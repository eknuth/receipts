"""The investigation loop: one run, one report.

    report = await investigate(scenario_run, AgentConfig())

The phases (orient, characterize, BubbleUp, traces, verify by negation,
record) are in the prompt, not in this file. The model picks the queries. What
this file owns is everything that has to be true no matter what the model
does: the budget, the tool log, the counters, and the two rules, which are
enforced by `agent/validate.py` against the log rather than taken on trust.

The model is told the dataset, the environment, the run id to scope every
query to, and the time window. It is never told the scenario id. Scenario ids
read as answers, so `ScenarioRun.scenario_id` is carried for bookkeeping and
never reaches a message; a test asserts that.

Stopping. The loop ends when `submit_report` produces a report that passes
validation, when the MCP call budget is spent, or when the wall clock budget
is spent. The two budgets are not hard stops on their own: when either is
reached the model is told so and gets a few turns to submit what it has, which
is the difference between a thin report and no report at all.

Validation. A rejected report is handed back once with the reasons, in the
"hold until the flagged claims are cleared" shape from
`charles/api/src/services/fact-checker.ts`. A second rejection keeps the report
and sets `validation_failed`, because a report that broke its own rules is a
result, and the grader should see it and mark it down.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template

from pydantic import ValidationError

from agent import validate
from agent.mcp_client import HoneycombMCP, ToolNotAllowed
from agent.providers.base import (
    Completion,
    Provider,
    ToolResultBlock,
    ToolSchema,
    ToolUse,
    Turn,
)
from agent.report import Report, ReportDraft, ToolCall, submit_report_schema
from agent.telemetry import RunTrace, disabled_run_trace
from evals.pricing import cost_usd
from receipts.settings import Settings

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "investigator.md"

SUBMIT_REPORT = "submit_report"
SUBMIT_REPORT_DESCRIPTION = (
    "File the investigation report. Call this once, when you are done. Every query_id in it "
    "is checked against the log of the tool calls you made in this session."
)

# The read tools an investigation uses. The schemas come from the server, so
# this only decides which of the advertised tools are offered; anything the
# server does not serve is silently absent. Tools outside the investigation,
# such as the semantic-convention lookups and the AI conversation browser, are
# left out to keep the schema block from crowding the window.
INVESTIGATION_TOOLS: tuple[str, ...] = (
    "get_workspace_context",
    "get_environment",
    "get_dataset",
    "get_dataset_columns",
    "find_columns",
    "find_queries",
    "run_query",
    "get_query_results",
    "run_bubbleup",
    "get_trace",
    "list_spans",
    "get_span_details",
    "get_slos",
    "get_triggers",
)

DEFAULT_MAX_CALLS = 40
DEFAULT_MAX_WALL_S = 8 * 60.0
DEFAULT_MAX_TOKENS = 8192

# How many more model turns to allow once a budget is spent, so the model can
# file what it has instead of the run ending with nothing.
GRACE_TURNS = 3
# How many times a model that answers with plain text is asked to use a tool.
MAX_NUDGES = 2

_OPTIONAL_BLOCK = re.compile(
    r"<!-- optional: (?P<name>[a-z_]+) -->\n(?P<body>.*?)<!-- end -->\n", re.DOTALL
)


@dataclass(frozen=True)
class ScenarioRun:
    """One emitted run, as the agent is allowed to know it.

    `scenario_id` is bookkeeping. It names the ground truth file for the
    grader and fills `Report.scenario_id`; it never goes into a prompt.
    """

    run_id: str
    scenario_id: str
    dataset: str
    environment: str
    window_start: str
    window_end: str

    @classmethod
    def from_manifest(cls, manifest: object) -> ScenarioRun:
        """Build from a `gen.emit.EmitResult`."""
        return cls(
            run_id=manifest.run_id,  # type: ignore[attr-defined]
            scenario_id=manifest.scenario_id,  # type: ignore[attr-defined]
            dataset=manifest.dataset,  # type: ignore[attr-defined]
            environment=manifest.environment,  # type: ignore[attr-defined]
            window_start=manifest.window_start,  # type: ignore[attr-defined]
            window_end=manifest.window_end,  # type: ignore[attr-defined]
        )


@dataclass(frozen=True)
class AgentConfig:
    """The knobs. `require_negation` and `require_not_checked` are R10's ablations."""

    provider: str = "anthropic"
    model: str | None = None
    require_negation: bool = True
    require_not_checked: bool = True
    max_calls: int = DEFAULT_MAX_CALLS
    max_wall_s: float = DEFAULT_MAX_WALL_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    tools: tuple[str, ...] = INVESTIGATION_TOOLS


def config_label(config: AgentConfig) -> str:
    """A short name for this config, for the trace's `agent.config` attribute.

    Mirrors `evals/run.py`'s config names (`full`, `no-negation`,
    `no-notchecked`) without importing from `evals`, which `agent/` does not
    depend on; the eval matrix passes its own config name instead of calling
    this, since that name is already authoritative there.
    """
    parts = []
    if not config.require_negation:
        parts.append("no-negation")
    if not config.require_not_checked:
        parts.append("no-notchecked")
    return "+".join(parts) if parts else "full"


@dataclass
class _Budget:
    """What is left of the two limits, and what the model has been told.

    `clock` is injectable so a test can spend the wall budget partway through
    a run without waiting for it.
    """

    max_calls: int
    max_wall_s: float
    started: float
    clock: Callable[[], float] = time.monotonic
    calls: int = 0
    warned: bool = False
    grace_left: int = GRACE_TURNS

    @property
    def wall_s(self) -> float:
        return self.clock() - self.started

    @property
    def calls_spent(self) -> bool:
        return self.calls >= self.max_calls

    @property
    def wall_spent(self) -> bool:
        return self.wall_s >= self.max_wall_s

    @property
    def spent(self) -> bool:
        return self.calls_spent or self.wall_spent

    def wall_left(self) -> float:
        """Seconds of wall budget left, floored at zero."""
        return max(0.0, self.max_wall_s - self.wall_s)

    def reason(self) -> str:
        return "call_cap" if self.calls_spent else "wall_cap"


def render_prompt(
    run: ScenarioRun,
    config: AgentConfig,
    *,
    path: Path = PROMPT_PATH,
) -> str:
    """The system prompt for this run, with the ablated sections removed.

    All the prose is in `agent/prompts/investigator.md`. The optional blocks
    are marked in that file, and the switches here drop them whole rather than
    rewording anything, so an ablation removes a rule instead of softening it.
    """
    keep = {"negation": config.require_negation, "not_checked": config.require_not_checked}

    def resolve(match: re.Match[str]) -> str:
        return match.group("body") if keep.get(match.group("name"), True) else ""

    text = _OPTIONAL_BLOCK.sub(resolve, path.read_text())
    return Template(text).substitute(
        environment=run.environment,
        dataset=run.dataset,
        run_id=run.run_id,
        window_start=run.window_start,
        window_end=run.window_end,
        max_calls=config.max_calls,
        max_wall_minutes=round(config.max_wall_s / 60.0),
    )


def opening_message(run: ScenarioRun) -> str:
    """The one user message that starts the investigation."""
    return (
        f"Investigate the traffic in `{run.dataset}` where `scenario.run_id = {run.run_id}`, "
        f"between {run.window_start} and {run.window_end}. Work out whether anything went "
        "wrong, and if it did, what and for whom. Follow the method, then file the report."
    )


async def build_tools(mcp: HoneycombMCP, config: AgentConfig) -> list[ToolSchema]:
    """The tools the model sees: the server's own schemas, plus `submit_report`."""
    wanted = set(config.tools)
    tools = [
        ToolSchema(
            name=spec.name,
            description=spec.description or "",
            input_schema=spec.input_schema,
        )
        for spec in await mcp.list_tools()
        if spec.name in wanted
    ]
    tools.append(
        ToolSchema(
            name=SUBMIT_REPORT,
            description=SUBMIT_REPORT_DESCRIPTION,
            input_schema=submit_report_schema(),
        )
    )
    return tools


def _make_provider(config: AgentConfig, settings: Settings) -> Provider:
    if config.provider != "anthropic":
        raise ValueError(f"unknown provider {config.provider!r}; only 'anthropic' exists in R6")
    from agent.providers.anthropic import AnthropicProvider

    return AnthropicProvider(settings, model=config.model)


async def investigate(
    run: ScenarioRun,
    config: AgentConfig | None = None,
    *,
    settings: Settings | None = None,
    mcp: HoneycombMCP | None = None,
    provider: Provider | None = None,
    clock: Callable[[], float] = time.monotonic,
    trace: RunTrace | None = None,
) -> Report:
    """Investigate one run and return its report.

    `mcp`, `provider`, and `clock` are injectable so the tests can drive the
    whole loop with no network and no waiting. When `mcp` is None a session is
    opened and closed here.

    `trace` is the root span this run's `chat` and `execute_tool` spans
    attach to, opened by the caller (see `agent/telemetry.py`) so grading,
    which happens after this returns, can still write the evaluation result
    onto it. When `trace` is None, as in every existing caller that does not
    care about telemetry, a disabled trace is used and nothing is emitted.
    """
    config = config or AgentConfig()
    settings = settings or Settings()
    trace = trace or disabled_run_trace(run.run_id)
    if mcp is not None:
        return await _investigate(run, config, settings, mcp, provider, clock, trace)
    async with HoneycombMCP(settings=settings) as session:
        return await _investigate(run, config, settings, session, provider, clock, trace)


async def _investigate(
    run: ScenarioRun,
    config: AgentConfig,
    settings: Settings,
    mcp: HoneycombMCP,
    provider: Provider | None,
    clock: Callable[[], float],
    trace: RunTrace,
) -> Report:
    provider = provider or _make_provider(config, settings)
    budget = _Budget(
        max_calls=config.max_calls,
        max_wall_s=config.max_wall_s,
        started=clock(),
        clock=clock,
    )

    state = _RunState(run=run, config=config, provider=provider, budget=budget, trace=trace)

    try:
        tools = await build_tools(mcp, config)
    except Exception as exc:  # a session that cannot list tools cannot investigate
        logger.exception("could not list tools")
        return state.finish(stop_reason="error", error=f"{type(exc).__name__}: {exc}")

    system = render_prompt(run, config)
    turns: list[Turn] = [Turn(role="user", text=opening_message(run))]
    nudges = 0

    while True:
        # The wall is checked around the model call as well as after it. Before
        # this the budget was advisory: it was only read between turns, so a
        # provider that hung or retried could run for an hour against a stated
        # eight minute budget.
        remaining = budget.wall_left()
        if remaining <= 0:
            return state.finish(stop_reason=budget.reason())
        try:
            async with asyncio.timeout(remaining):
                with trace.chat_span(
                    provider.model, provider_name=config.provider, max_tokens=config.max_tokens
                ) as chat:
                    completion = await provider.complete(
                        system, turns, tools, max_tokens=config.max_tokens
                    )
                    chat.record(completion, system=system, turns=turns)
        except TimeoutError:
            logger.warning("provider call ran past the wall budget")
            return state.finish(stop_reason="wall_cap")
        except Exception as exc:
            logger.exception("provider call failed")
            return state.finish(stop_reason="error", error=f"{type(exc).__name__}: {exc}")

        state.record_usage(completion)
        turns.append(
            Turn(
                role="assistant",
                text=completion.text,
                tool_uses=completion.tool_uses,
                raw=completion.raw_content,
            )
        )

        if not completion.tool_uses:
            nudges += 1
            if nudges > MAX_NUDGES:
                return state.finish(stop_reason="model_stopped")
            turns.append(Turn(role="user", text=_nudge_text(budget)))
            continue

        results: list[ToolResultBlock] = []
        for use in completion.tool_uses:
            if use.name == SUBMIT_REPORT:
                report = state.submit(use)
                if report is not None:
                    return report
                results.append(
                    ToolResultBlock(
                        tool_use_id=use.id,
                        content=state.last_rejection,
                        is_error=True,
                    )
                )
                continue
            results.append(await state.call_tool(mcp, use))

        turns.append(Turn(role="user", text=None, tool_results=results))

        if budget.spent:
            if budget.grace_left <= 0:
                return state.finish(stop_reason=budget.reason())
            budget.grace_left -= 1
            if not budget.warned:
                budget.warned = True
                turns.append(Turn(role="user", text=_budget_text(budget)))


def _nudge_text(budget: _Budget) -> str:
    return (
        "That turn made no tool call. Either run the next query or call submit_report with "
        f"what you have. {budget.max_calls - budget.calls} MCP calls remain."
    )


def _budget_text(budget: _Budget) -> str:
    limit = f"the {budget.max_calls} MCP call budget" if budget.calls_spent else "the time budget"
    return (
        f"You have spent {limit}. No further MCP calls will run. Call submit_report now with "
        "the evidence you already have, and put everything you did not get to into not_checked."
    )


class _RunState:
    """The counters, the tool log, and the one report the run is allowed to file."""

    def __init__(
        self,
        run: ScenarioRun,
        config: AgentConfig,
        provider: Provider,
        budget: _Budget,
        trace: RunTrace,
    ) -> None:
        self.run = run
        self.config = config
        self.provider = provider
        self.budget = budget
        self.trace = trace
        self.tool_log: list[ToolCall] = []
        self.tokens_in = 0
        self.tokens_out = 0
        self.cache_read = 0
        self.cache_write = 0
        self.model_turns = 0
        self.model_stop_reason: str | None = None
        self.rejections = 0
        self.last_rejection = ""
        self.issues: list[validate.Issue] = []

    # -- counters ---------------------------------------------------------

    def record_usage(self, completion: Completion) -> None:
        self.model_turns += 1
        self.model_stop_reason = completion.stop_reason
        self.tokens_in += completion.usage.input_tokens
        self.tokens_out += completion.usage.output_tokens
        self.cache_read += completion.usage.cache_read_tokens
        self.cache_write += completion.usage.cache_write_tokens

    # -- tools ------------------------------------------------------------

    async def call_tool(self, mcp: HoneycombMCP, use: ToolUse) -> ToolResultBlock:
        """Run one MCP call, log it, and return what the model sees."""
        if self.budget.spent:
            return ToolResultBlock(
                tool_use_id=use.id,
                content=_budget_text(self.budget),
                is_error=True,
            )

        self.budget.calls += 1
        elapsed = self.budget.wall_s
        with self.trace.tool_span(use.name, use.id, use.args) as tool_span:
            try:
                result = await mcp.call(
                    use.name,
                    use.args,
                    traceparent=tool_span.traceparent,
                    tracestate=tool_span.tracestate,
                )
            except ToolNotAllowed as exc:
                tool_span.record_exception(exc)
                self._log(use, elapsed, is_error=True)
                return ToolResultBlock(tool_use_id=use.id, content=str(exc), is_error=True)
            except Exception as exc:
                logger.warning("mcp call %s failed: %s", use.name, exc)
                tool_span.record_exception(exc)
                self._log(use, elapsed, is_error=True)
                return ToolResultBlock(
                    tool_use_id=use.id,
                    content=f"{use.name} failed: {type(exc).__name__}: {exc}",
                    is_error=True,
                )

            tool_span.record_result(result.text, is_error=result.is_error)
            self._log(
                use,
                elapsed,
                is_error=result.is_error,
                query_id=result.query_id,
                permalink=result.permalink,
            )
            return ToolResultBlock(
                tool_use_id=use.id, content=result.text, is_error=result.is_error
            )

    def _log(
        self,
        use: ToolUse,
        elapsed: float,
        *,
        is_error: bool,
        query_id: str | None = None,
        permalink: str | None = None,
    ) -> None:
        self.tool_log.append(
            ToolCall(
                name=use.name,
                args=use.args,
                query_id=query_id,
                permalink=permalink,
                is_error=is_error,
                t=round(elapsed, 3),
            )
        )

    # -- the report -------------------------------------------------------

    def submit(self, use: ToolUse) -> Report | None:
        """Validate a submitted report. Returns the report, or None to re-prompt."""
        try:
            draft = ReportDraft.model_validate(use.args)
        except ValidationError as exc:
            self.rejections += 1
            self.last_rejection = (
                "The report did not match the submit_report schema and was not filed:\n"
                f"{exc}\nFix the fields and call submit_report again."
            )
            if self.rejections > 1:
                return self.finish(
                    stop_reason="report",
                    validation_failed=True,
                    messages=[self.last_rejection],
                )
            return None

        issues = validate.validate_draft(
            draft,
            self.tool_log,
            run_id=self.run.run_id,
            require_negation=self.config.require_negation,
            require_not_checked=self.config.require_not_checked,
        )
        if not issues:
            return self.finish(stop_reason="report", draft=draft)

        self.rejections += 1
        self.issues = issues
        self.last_rejection = validate.rejection_message(issues)
        if self.rejections > 1:
            # Kept and flagged rather than discarded. The grader punishes it.
            return self.finish(
                stop_reason="report",
                draft=draft,
                validation_failed=True,
                messages=[str(issue) for issue in issues],
            )
        return None

    def finish(
        self,
        *,
        stop_reason: str,
        draft: ReportDraft | None = None,
        validation_failed: bool = False,
        messages: Sequence[str] | None = None,
        error: str | None = None,
    ) -> Report:
        """Assemble the report with the process fields the loop measured."""
        cost = cost_usd(
            self.provider.model,
            self.tokens_in,
            self.tokens_out,
            cache_read_tokens=self.cache_read,
            cache_write_tokens=self.cache_write,
        )
        if cost is None:
            logger.warning(
                "no price for %r in evals/pricing.yml; cost recorded as 0", self.provider.model
            )
        process = {
            "run_id": self.run.run_id,
            "scenario_id": self.run.scenario_id,
            "provider": self.provider.name,
            "model": self.provider.model,
            "tool_calls": len(self.tool_log),
            "model_turns": self.model_turns,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cache_read_tokens": self.cache_read,
            "cache_write_tokens": self.cache_write,
            "wall_s": round(self.budget.wall_s, 2),
            "cost_usd": round(cost or 0.0, 6),
            "tool_log": list(self.tool_log),
            "stop_reason": stop_reason,
            "model_stop_reason": self.model_stop_reason,
            "validation_failed": validation_failed,
            "validation_messages": list(messages or []),
            "error": error,
        }
        if draft is None:
            return Report(**process)
        return Report.from_draft(draft, **process)
