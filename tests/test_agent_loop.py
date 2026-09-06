"""Tests for agent/loop.py: the budget, the tool log, and the two rules end to end.

No network. A fake provider returns scripted tool_use blocks and a fake MCP
returns canned results, so the whole loop runs in memory and every stop
condition can be reached on purpose.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from agent.loop import (
    SUBMIT_REPORT,
    AgentConfig,
    ScenarioRun,
    build_tools,
    investigate,
    opening_message,
    render_prompt,
)
from agent.mcp_client import ToolResult, ToolSpec
from agent.providers.base import Completion, ToolSchema, ToolUse, Turn, Usage
from agent.report import Report
from receipts.settings import Settings

RUN = ScenarioRun(
    run_id="run-abc123",
    scenario_id="payments-stripe-v251-uswest",
    dataset="receipts-shop",
    environment="receipts-demo",
    window_start="2026-09-03T02:37:20Z",
    window_end="2026-09-03T02:57:20Z",
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeMCP:
    """Enough of HoneycombMCP for the loop: list_tools and call."""

    def __init__(self, *, query_ids: list[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._query_ids = list(query_ids or ["Q1", "Q2", "Q3", "Q4", "Q5"])
        self.list_tools_error: Exception | None = None

    async def list_tools(self) -> list[ToolSpec]:
        if self.list_tools_error is not None:
            raise self.list_tools_error
        return [
            ToolSpec(name="get_workspace_context", description="orient", input_schema={}),
            ToolSpec(name="run_query", description="query", input_schema={"type": "object"}),
            ToolSpec(name="semconv", description="not an investigation tool", input_schema={}),
        ]

    async def call(
        self, name: str, args: dict[str, Any] | None = None, **kwargs: Any
    ) -> ToolResult:
        self.calls.append((name, dict(args or {})))
        if name != "run_query":
            return ToolResult(
                raw="ok", text=f"{name} ok", is_error=False, query_id=None, permalink=None
            )
        index = sum(1 for call in self.calls if call[0] == "run_query") - 1
        query_id = self._query_ids[index % len(self._query_ids)]
        return ToolResult(
            raw="rows",
            text=f"# Results\nquery_id: {query_id}",
            is_error=False,
            query_id=query_id,
            permalink=f"https://ui.honeycomb.io/x/result/{query_id}",
        )


class FakeProvider:
    """Replays scripted completions and records everything it was sent."""

    name = "fake"

    def __init__(self, script: list[Completion], model: str = "claude-sonnet-4-5") -> None:
        self.model = model
        self._script = list(script)
        self.seen: list[tuple[str, list[Turn], list[ToolSchema]]] = []

    async def complete(
        self,
        system: str,
        turns: list[Turn],
        tools: list[ToolSchema],
        *,
        max_tokens: int,
    ) -> Completion:
        self.seen.append((system, [Turn(**vars(t)) for t in turns], tools))
        if len(self._script) > 1:
            return self._script.pop(0)
        return self._script[0]


def use(name: str, args: dict[str, Any] | None = None, ident: str = "tu") -> ToolUse:
    return ToolUse(id=ident, name=name, args=args or {})


def completion(*tool_uses: ToolUse, text: str = "") -> Completion:
    return Completion(
        text=text,
        tool_uses=list(tool_uses),
        usage=Usage(input_tokens=1000, output_tokens=200, cache_read_tokens=50),
        stop_reason="tool_use" if tool_uses else "end_turn",
    )


def query_use(ident: str = "q") -> ToolUse:
    return use(
        "run_query",
        {
            "dataset_slug": "receipts-shop",
            "query_spec": {
                "calculations": [{"op": "P99", "column": "duration_ms"}],
                "filters": [{"column": "scenario.run_id", "op": "=", "value": RUN.run_id}],
                "breakdowns": ["deployment.version"],
            },
        },
        ident,
    )


def baseline_use(ident: str = "b0") -> ToolUse:
    """A plain measurement over the window, which is what a baseline cites."""
    return use(
        "run_query",
        {
            "dataset_slug": "receipts-shop",
            "query_spec": {
                "calculations": [{"op": "P99", "column": "duration_ms"}],
                "filters": [{"column": "scenario.run_id", "op": "=", "value": RUN.run_id}],
            },
        },
        ident,
    )


def negation_use(ident: str = "n", column: str = "deployment.version") -> ToolUse:
    """A real WHERE NOT: the same measurement with the population cut out."""
    return use(
        "run_query",
        {
            "dataset_slug": "receipts-shop",
            "query_spec": {
                "calculations": [{"op": "P99", "column": "duration_ms"}],
                "filters": [
                    {"column": "scenario.run_id", "op": "=", "value": RUN.run_id},
                    {"column": column, "op": "!=", "value": "9.9.9"},
                ],
            },
        },
        ident,
    )


def report_args(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "incident_present": True,
        "hypotheses": [
            {
                "claim": "one population got slow",
                "dims": {"deployment.version": "9.9.9"},
                "slow_or_failing_span": "some.span",
                "confidence": "high",
                "evidence": [{"query_id": "Q1", "summary": "P99 180ms to 1100ms"}],
                "negation": {"query_id": "Q2", "summary": "flat outside"},
            }
        ],
        "affected_population": "12%",
        "onset_estimate": "2026-09-03T02:47:20Z",
        "not_checked": ["customer.id", "cart.size"],
        "baseline_evidence": [{"query_id": "Q3", "summary": "P99 flat before onset"}],
    }
    base.update(overrides)
    return base


async def run_loop(
    provider: FakeProvider,
    mcp: FakeMCP | None = None,
    config: AgentConfig | None = None,
    *,
    settings: Settings,
) -> Report:
    return await investigate(
        RUN,
        config or AgentConfig(),
        settings=settings,
        mcp=mcp or FakeMCP(),
        provider=provider,
    )


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------


def test_the_prompt_carries_the_scope_and_cites_the_method() -> None:
    prompt = render_prompt(RUN, AgentConfig())
    assert RUN.run_id in prompt
    assert RUN.dataset in prompt
    assert RUN.window_start in prompt and RUN.window_end in prompt
    assert "github.com/honeycombio/agent-skill" in prompt
    assert "$" not in prompt


def test_the_prompt_has_no_em_dashes() -> None:
    assert "—" not in render_prompt(RUN, AgentConfig())


def test_the_prompt_names_no_scenario_and_no_ground_truth() -> None:
    """The method is general. Naming a dimension here would be tuning to the data."""
    prompt = render_prompt(RUN, AgentConfig()).lower()
    for word in (
        "payments",
        "stripe",
        "adyen",
        "paypal",
        "us-west",
        "eu-west",
        "2.5.1",
        "2.6.0",
        "checkout",
        "inventory",
        "scenario.id",
    ):
        assert word not in prompt, word


def test_the_prompt_makes_the_split_in_time_a_precondition() -> None:
    """A standing difference is not an incident, and the method has to say so.

    Two control runs reported a difference that held for the whole window as an
    incident, so these phrases are pinned: an edit that drops them drops the rule.
    """
    prompt = render_prompt(RUN, AgentConfig())
    assert "No candidate goes into `hypotheses` until this split has been run" in prompt
    assert "still there, or still coming back, at the end" in prompt
    assert "the same size on both sides of the split is a property of the system" in prompt
    assert "the end of the last part of the window that still read normal" in prompt


def test_the_prompt_adds_a_population_selection_step() -> None:
    """EDW-1364: dims is decided by a step, not left to whichever candidate BubbleUp
    ranked first. The step is in the fixed part of the prompt, so neither ablation
    should touch it, and it has to come before Traces since Traces filters on `dims`.
    """
    for config in (
        AgentConfig(),
        AgentConfig(require_negation=False),
        AgentConfig(require_not_checked=False),
    ):
        prompt = render_prompt(RUN, config)
        assert "**Select the population.**" in prompt
        assert "nothing that does not narrow them" in prompt
        assert prompt.index("**Select the population.**") < prompt.index("**Traces.**")


def test_the_ablations_remove_whole_rules_from_the_prompt() -> None:
    full = render_prompt(RUN, AgentConfig())
    assert "negation" in full.lower()
    assert "not_checked" in full

    no_negation = render_prompt(RUN, AgentConfig(require_negation=False))
    assert "WHERE NOT" not in no_negation
    assert "not_checked" in no_negation

    no_list = render_prompt(RUN, AgentConfig(require_not_checked=False))
    assert "Rule two" not in no_list
    assert "WHERE NOT" in no_list


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


async def test_the_tool_list_is_the_servers_schemas_plus_submit_report() -> None:
    tools = await build_tools(FakeMCP(), AgentConfig())
    names = [tool.name for tool in tools]
    assert names[-1] == SUBMIT_REPORT
    assert "run_query" in names
    # `semconv` is advertised by the server and is not an investigation tool.
    assert "semconv" not in names


# --------------------------------------------------------------------------
# Stopping
# --------------------------------------------------------------------------


async def test_the_loop_stops_when_a_valid_report_is_submitted(settings: Settings) -> None:
    provider = FakeProvider(
        [
            completion(use("get_workspace_context", ident="a")),
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, report_args(), ident="d")),
        ]
    )
    mcp = FakeMCP()
    report = await run_loop(provider, mcp, settings=settings)

    assert report.stop_reason == "report"
    assert report.validation_failed is False
    assert report.incident_present is True
    assert report.hypotheses[0].dims == {"deployment.version": "9.9.9"}
    assert [call[0] for call in mcp.calls] == [
        "get_workspace_context",
        "run_query",
        "run_query",
        "run_query",
    ]


async def test_the_process_fields_are_counted_not_taken_from_the_model(
    settings: Settings,
) -> None:
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, report_args(), ident="d")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    assert report.tool_calls == 3
    assert report.model_turns == 2
    assert report.tokens_in == 2000
    assert report.tokens_out == 400
    assert report.cache_read_tokens == 100
    assert report.wall_s >= 0
    assert report.cost_usd == pytest.approx(2000 * 3.0 / 1e6 + 400 * 15.0 / 1e6 + 100 * 0.3 / 1e6)
    assert report.provider == "fake"
    assert report.run_id == RUN.run_id
    assert report.scenario_id == RUN.scenario_id


async def test_the_tool_log_records_every_call_with_its_query_id(settings: Settings) -> None:
    provider = FakeProvider(
        [
            completion(query_use("b")),
            completion(use(SUBMIT_REPORT, report_args(), ident="d")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    entry = report.tool_log[0]
    assert entry.name == "run_query"
    assert entry.query_id == "Q1"
    assert entry.permalink == "https://ui.honeycomb.io/x/result/Q1"
    assert entry.args["query_spec"]["breakdowns"] == ["deployment.version"]
    assert entry.is_error is False
    assert entry.t >= 0


async def test_a_malformed_tool_call_is_counted_and_still_logged(settings: Settings) -> None:
    """A provider (agent/providers/ollama.py) that flags a call malformed still

    has it go out and get logged like any other call; the loop's own count
    is on top of that, not instead of it.
    """
    malformed = ToolUse(id="m1", name="run_query", args={}, malformed=True)
    provider = FakeProvider(
        [
            completion(malformed),
            completion(use(SUBMIT_REPORT, report_args(), ident="d")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    assert report.malformed_calls == 1
    assert report.tool_calls == 1
    assert report.tool_log[0].name == "run_query"
    assert report.tool_log[0].args == {}


async def test_a_well_formed_tool_call_does_not_count_as_malformed(settings: Settings) -> None:
    provider = FakeProvider(
        [
            completion(query_use("b")),
            completion(use(SUBMIT_REPORT, report_args(), ident="d")),
        ]
    )
    report = await run_loop(provider, settings=settings)
    assert report.malformed_calls == 0


async def test_the_report_records_the_wall_budget_it_was_given(settings: Settings) -> None:
    provider = FakeProvider([completion(use(SUBMIT_REPORT, report_args(), ident="d"))])
    report = await run_loop(provider, config=AgentConfig(max_wall_s=1200.0), settings=settings)
    assert report.max_wall_s == 1200.0


async def test_the_loop_stops_at_the_call_cap(settings: Settings) -> None:
    """The model keeps querying; the cap ends the run and no extra calls land."""
    provider = FakeProvider([completion(query_use("q"))])
    mcp = FakeMCP()
    report = await run_loop(provider, mcp, AgentConfig(max_calls=2), settings=settings)

    assert report.stop_reason == "call_cap"
    assert len(mcp.calls) == 2
    assert report.tool_calls == 2
    assert report.hypotheses == []


async def test_the_loop_stops_at_the_wall_cap(settings: Settings) -> None:
    """A clock that jumps past the limit after the first query ends the run."""
    ticks = iter([0.0, 0.0, 0.0, 999.0])

    def clock() -> float:
        return next(ticks, 999.0)

    provider = FakeProvider([completion(query_use("q"))])
    mcp = FakeMCP()
    report = await investigate(
        RUN,
        AgentConfig(max_wall_s=60.0),
        settings=settings,
        mcp=mcp,
        provider=provider,
        clock=clock,
    )

    assert report.stop_reason == "wall_cap"
    assert len(mcp.calls) == 1
    assert report.tool_calls == 1


async def test_a_spent_budget_is_told_to_the_model_before_the_run_ends(
    settings: Settings,
) -> None:
    provider = FakeProvider([completion(query_use("q"))])
    await run_loop(provider, config=AgentConfig(max_calls=1), settings=settings)

    said = "\n".join(
        turn.text or "" for _, turns, _ in provider.seen for turn in turns if turn.role == "user"
    )
    assert "submit_report now" in said


async def test_a_model_that_stops_calling_tools_is_nudged_then_the_run_ends(
    settings: Settings,
) -> None:
    provider = FakeProvider([completion(text="I think it was the database.")])
    report = await run_loop(provider, settings=settings)

    assert report.stop_reason == "model_stopped"
    assert len(provider.seen) == 3  # the first turn plus two nudges


async def test_a_provider_failure_is_recorded_not_raised(settings: Settings) -> None:
    class Broken(FakeProvider):
        async def complete(self, *args: Any, **kwargs: Any) -> Completion:
            raise RuntimeError("no credit")

    report = await run_loop(Broken([completion()]), settings=settings)
    assert report.stop_reason == "error"
    assert "no credit" in (report.error or "")


async def test_a_successful_query_result_carries_the_queried_so_far_footer(
    settings: Settings,
) -> None:
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, report_args(), ident="d")),
        ]
    )
    await run_loop(provider, settings=settings)

    turns = provider.seen[-1][1]
    footers = [
        result.content
        for turn in turns
        for result in turn.tool_results
        if "Queried so far:" in (result.content or "")
    ]
    assert footers
    assert footers[0].endswith("Queried so far: deployment.version, duration_ms")


def region_breakdown_use(ident: str = "e") -> ToolUse:
    """A fourth query, over a column no other call in the test names, so a
    footer taken after it has grown over the ones before it."""
    return use(
        "run_query",
        {
            "dataset_slug": "receipts-shop",
            "query_spec": {
                "calculations": [{"op": "COUNT"}],
                "filters": [{"column": "scenario.run_id", "op": "=", "value": RUN.run_id}],
                "breakdowns": ["cloud.region"],
            },
        },
        ident,
    )


async def test_the_footer_grows_as_more_queries_run(settings: Settings) -> None:
    # The fourth call is not cited by report_args(), so it does not change what
    # the report needs to validate; it only grows the queried-terms set the
    # footer after it reports.
    provider = FakeProvider(
        [
            completion(
                query_use("b"), negation_use("c"), baseline_use("d"), region_breakdown_use("e")
            ),
            completion(use(SUBMIT_REPORT, report_args(), ident="f")),
        ]
    )
    await run_loop(provider, settings=settings)

    # Only the last snapshot has the full, non-overlapping turn history: each
    # earlier snapshot is a prefix of it, so summing footers across snapshots
    # would double-count the ones common to more than one.
    turns = provider.seen[-1][1]
    footers = [
        result.content
        for turn in turns
        for result in turn.tool_results
        if "Queried so far:" in (result.content or "")
    ]
    assert len(footers) == 4
    assert "cloud.region" not in footers[0]
    assert "cloud.region" in footers[-1]


async def test_an_error_result_carries_no_footer(settings: Settings) -> None:
    class Failing(FakeMCP):
        async def call(
            self, name: str, args: dict[str, Any] | None = None, **kwargs: Any
        ) -> ToolResult:
            self.calls.append((name, dict(args or {})))
            raise RuntimeError("dataset not found")

    provider = FakeProvider(
        [completion(query_use("q")), completion(use(SUBMIT_REPORT, report_args(), ident="d"))]
    )
    await run_loop(provider, Failing(), settings=settings)

    error_results = [
        result
        for _, turns, _ in provider.seen
        for turn in turns
        for result in turn.tool_results
        if result.is_error
    ]
    assert error_results
    assert all("Queried so far" not in (result.content or "") for result in error_results)


async def test_the_footer_is_not_stored_in_the_tool_log(settings: Settings) -> None:
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, report_args(), ident="d")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    assert not any("Queried so far" in str(call.model_dump()) for call in report.tool_log)


class BreakdownMCP(FakeMCP):
    """A FakeMCP whose run_query result carries a real `# Results` table, so
    `agent/format.py`'s breakdown_values has something to read."""

    async def call(
        self, name: str, args: dict[str, Any] | None = None, **kwargs: Any
    ) -> ToolResult:
        self.calls.append((name, dict(args or {})))
        if name != "run_query":
            return ToolResult(
                raw="ok", text=f"{name} ok", is_error=False, query_id=None, permalink=None
            )
        text = (
            "# Results\n\n| COUNT | deployment.version |\n| --- | --- |\n"
            "| 10 | 9.9.9 |\n| 5 | 9.9.8 |\n\n---\nMetadata:\n  query_run_pk: Q1\n"
        )
        return ToolResult(raw=text, text=text, is_error=False, query_id="Q1", permalink=None)


async def test_a_breakdown_result_is_recorded_into_result_values(settings: Settings) -> None:
    provider = FakeProvider(
        [completion(query_use("b")), completion(use(SUBMIT_REPORT, report_args(), ident="d"))]
    )
    report = await run_loop(provider, BreakdownMCP(), settings=settings)

    assert report.tool_log[0].result_values == {"deployment.version": ["9.9.9", "9.9.8"]}


async def test_recorded_result_values_feed_the_queried_so_far_footer(settings: Settings) -> None:
    provider = FakeProvider(
        [completion(query_use("b")), completion(use(SUBMIT_REPORT, report_args(), ident="d"))]
    )
    await run_loop(provider, BreakdownMCP(), settings=settings)

    turns = provider.seen[-1][1]
    footers = [
        result.content
        for turn in turns
        for result in turn.tool_results
        if "Queried so far:" in (result.content or "")
    ]
    assert footers
    assert "9.9.9" in footers[0]
    assert "9.9.8" in footers[0]


async def test_a_failing_mcp_call_becomes_a_tool_result_the_model_can_read(
    settings: Settings,
) -> None:
    class Failing(FakeMCP):
        async def call(
            self, name: str, args: dict[str, Any] | None = None, **kwargs: Any
        ) -> ToolResult:
            self.calls.append((name, dict(args or {})))
            raise RuntimeError("dataset not found")

    provider = FakeProvider(
        [completion(query_use("q")), completion(use(SUBMIT_REPORT, report_args(), ident="d"))]
    )
    report = await run_loop(provider, Failing(), settings=settings)

    assert report.tool_log[0].is_error is True
    assert report.stop_reason == "report"
    assert report.validation_failed is True  # Q1 was never returned, so the citation fails


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


async def test_a_rejected_report_is_handed_back_once_and_can_be_fixed(
    settings: Settings,
) -> None:
    bad = report_args(hypotheses=[dict(report_args()["hypotheses"][0], evidence=[])])
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, bad, ident="d")),
            completion(use(SUBMIT_REPORT, report_args(), ident="e")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    assert report.validation_failed is False
    assert report.stop_reason == "report"
    handed_back = "\n".join(
        result.content
        for _, turns, _ in provider.seen
        for turn in turns
        for result in turn.tool_results
    )
    assert "The report was rejected" in handed_back
    assert "carries no evidence" in handed_back


async def test_a_second_rejection_keeps_the_report_and_flags_it(settings: Settings) -> None:
    bad = report_args(hypotheses=[dict(report_args()["hypotheses"][0], evidence=[])])
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, bad, ident="d")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    assert report.validation_failed is True
    assert report.stop_reason == "report"
    assert report.hypotheses  # kept, so the grader can see what it did
    assert any("unsupported" in message for message in report.validation_messages)


async def test_a_not_checked_entry_that_was_queried_fails_validation(
    settings: Settings,
) -> None:
    lying = report_args(not_checked=["deployment.version was never broken down on"])
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, lying, ident="d")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    assert report.validation_failed is True
    assert any("not_checked_false" in message for message in report.validation_messages)


async def test_a_json_encoded_hypotheses_list_is_accepted_and_the_coercion_is_recorded(
    settings: Settings,
) -> None:
    """A live run once sent `hypotheses` as the JSON text of a list rather than
    a list. Before agent/report.py's coercion this burned the call budget on
    a validation error and filed nothing; now it is accepted and the
    coercion is recorded rather than silent."""
    encoded = report_args(hypotheses=json.dumps(report_args()["hypotheses"]))
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, encoded, ident="d")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    assert report.stop_reason == "report"
    assert report.validation_failed is False
    assert report.hypotheses[0].dims == {"deployment.version": "9.9.9"}
    assert report.coerced_fields == ["hypotheses"]
    assert any("coerced_fields" in message for message in report.validation_messages)
    assert any("hypotheses" in message for message in report.validation_messages)


async def test_coerced_then_validator_rejected_then_clean_still_records_the_coercion(
    settings: Settings,
) -> None:
    """The first attempt sends hypotheses as a JSON string (coerces) and has
    no evidence (the validator rejects it, not the schema). The second
    attempt is a plain, uncoerced, valid report. The run ends clean, and the
    coercion from the abandoned first attempt is still on the record."""
    first_hypothesis = dict(report_args()["hypotheses"][0], evidence=[])
    coerced_and_rejected = report_args(hypotheses=json.dumps([first_hypothesis]))
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, coerced_and_rejected, ident="d")),
            completion(use(SUBMIT_REPORT, report_args(), ident="e")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    assert report.stop_reason == "report"
    assert report.validation_failed is False
    assert report.coerced_fields == ["hypotheses"]
    assert any("coerced_fields" in message for message in report.validation_messages)


async def test_coerced_then_schema_rejected_twice_still_records_the_coercion(
    settings: Settings,
) -> None:
    """Both attempts send dims as a JSON string (coerces) inside a hypothesis
    an invalid confidence level makes schema-invalid regardless. A field
    validator for one field runs even when a sibling field fails, so the
    coercion happens on every attempt though neither ever validates."""

    def bad_schema() -> dict[str, Any]:
        return report_args(
            hypotheses=[
                {
                    "claim": "x",
                    "dims": json.dumps({"deployment.version": "9.9.9"}),
                    "confidence": "certain",  # not one of high/medium/low
                    "evidence": [{"query_id": "Q1", "summary": "rows"}],
                }
            ]
        )

    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, bad_schema(), ident="d")),
            completion(use(SUBMIT_REPORT, bad_schema(), ident="e")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    assert report.stop_reason == "schema"  # nothing was filed, and the record says so
    assert report.validation_failed is True
    assert report.hypotheses == []  # the schema-rejected draft was never kept
    # Two attempts each needed the coercion, so two entries: the count is
    # per attempt, not per distinct field.
    assert report.coerced_fields == ["dims", "dims"]
    assert any("coerced_fields" in message for message in report.validation_messages)


async def test_a_clean_report_records_no_coercion(settings: Settings) -> None:
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, report_args(), ident="d")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    assert report.stop_reason == "report"
    assert report.coerced_fields == []
    assert not any("coerced_fields" in message for message in report.validation_messages)


async def test_a_tool_argument_coercion_is_folded_into_report_coerced_fields(
    settings: Settings,
) -> None:
    """EDW-1362: a run_bubbleup group value the MCP client retyped from the
    column schema (agent/mcp_client.py) shows up in Report.coerced_fields as
    run_bubbleup:<path>, on the tool log entry, and alongside a submit-side
    coercion when one also happened in the same run."""

    class BubbleupCoercing(FakeMCP):
        async def call(
            self, name: str, args: dict[str, Any] | None = None, **kwargs: Any
        ) -> ToolResult:
            if name == "run_bubbleup":
                self.calls.append((name, dict(args or {})))
                return ToolResult(
                    raw="ok",
                    text="run_bubbleup ok",
                    is_error=False,
                    query_id="B1",
                    permalink=None,
                    coerced=("selection.group.error",),
                )
            return await super().call(name, args, **kwargs)

    encoded = report_args(hypotheses=json.dumps(report_args()["hypotheses"]))
    bubbleup_use = use(
        "run_bubbleup",
        {"query_pk": "Q1", "selection": {"type": "group", "group": {"error": "true"}}},
        ident="e",
    )
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d"), bubbleup_use),
            completion(use(SUBMIT_REPORT, encoded, ident="f")),
        ]
    )
    report = await run_loop(provider, BubbleupCoercing(), settings=settings)

    assert report.stop_reason == "report"
    assert "run_bubbleup:selection.group.error" in report.coerced_fields
    assert "hypotheses" in report.coerced_fields
    logged = next(call for call in report.tool_log if call.name == "run_bubbleup")
    assert logged.coerced == ["selection.group.error"]


async def test_a_report_that_does_not_match_the_schema_is_handed_back(
    settings: Settings,
) -> None:
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, {"incident_present": "maybe"}, ident="d")),
            completion(use(SUBMIT_REPORT, report_args(), ident="e")),
        ]
    )
    report = await run_loop(provider, settings=settings)

    assert report.validation_failed is False
    handed_back = "\n".join(
        result.content
        for _, turns, _ in provider.seen
        for turn in turns
        for result in turn.tool_results
    )
    assert "did not match the submit_report schema" in handed_back


# --------------------------------------------------------------------------
# The leak
# --------------------------------------------------------------------------


async def test_the_scenario_id_never_reaches_the_provider(settings: Settings) -> None:
    """The model is told the run id and the window. The scenario id is an answer."""
    provider = FakeProvider(
        [
            completion(use("get_workspace_context", ident="a")),
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, report_args(), ident="d")),
        ]
    )
    report = await run_loop(provider, settings=settings)
    assert report.scenario_id == RUN.scenario_id

    assert provider.seen
    for system, turns, tools in provider.seen:
        blob = json.dumps(
            {
                "system": system,
                "turns": [
                    {
                        "role": turn.role,
                        "text": turn.text,
                        "tool_uses": [vars(u) for u in turn.tool_uses],
                        "tool_results": [vars(r) for r in turn.tool_results],
                    }
                    for turn in turns
                ],
                "tools": [vars(tool) for tool in tools],
            }
        )
        assert RUN.scenario_id not in blob
        assert "scenario_id" not in blob
        assert "scenario.id" not in blob


def test_the_opening_message_names_the_run_and_not_the_scenario() -> None:
    message = opening_message(RUN)
    assert RUN.run_id in message
    assert RUN.scenario_id not in message


# --------------------------------------------------------------------------
# The file on disk
# --------------------------------------------------------------------------


async def test_the_report_writes_to_results_run_id_report_json(
    settings: Settings, tmp_path: Path
) -> None:
    provider = FakeProvider(
        [
            completion(query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, report_args(), ident="d")),
        ]
    )
    report = await run_loop(provider, settings=settings)
    path = report.write(tmp_path)

    assert path == tmp_path / "run-abc123" / "report.json"
    written = json.loads(path.read_text())
    assert written["scenario_id"] == RUN.scenario_id
    assert written["tool_log"][0]["query_id"] == "Q1"
    assert written["cost_usd"] > 0


# --------------------------------------------------------------------------
# The wall budget has to bind during a model call, not only between turns
# --------------------------------------------------------------------------


async def test_a_provider_that_hangs_is_cut_off_at_the_wall(settings: Settings) -> None:
    """Before the timeout the wall was advisory: it was read between turns, so
    a provider that hung ran as long as it liked against a stated budget."""

    class Hanging(FakeProvider):
        async def complete(self, *args: Any, **kwargs: Any) -> Completion:
            await asyncio.sleep(30)
            raise AssertionError("the wall should have cut this off")

    started = time.monotonic()
    report = await investigate(
        RUN,
        AgentConfig(max_wall_s=0.2),
        provider=Hanging([completion(query_use("q"))]),
        mcp=FakeMCP(),
        settings=settings,
    )
    assert report.stop_reason == "wall_cap"
    assert time.monotonic() - started < 5.0


async def test_the_wall_stops_the_run_before_another_model_call(settings: Settings) -> None:
    provider = FakeProvider([completion(query_use("q"))])
    report = await investigate(
        RUN,
        AgentConfig(max_wall_s=0.0),
        provider=provider,
        mcp=FakeMCP(),
        settings=settings,
    )
    assert report.stop_reason == "wall_cap"
    assert provider.seen == []


async def test_the_model_stop_reason_is_recorded_next_to_the_loop_reason(
    settings: Settings,
) -> None:
    """A refusal and a model that just stopped calling tools both end the loop
    the same way, so the record has to keep the provider's own word for it."""
    refusal = Completion(
        text="I will not do that.",
        tool_uses=[],
        usage=Usage(input_tokens=10, output_tokens=5),
        stop_reason="refusal",
    )
    report = await run_loop(FakeProvider([refusal]), settings=settings)
    assert report.stop_reason == "model_stopped"
    assert report.model_stop_reason == "refusal"


# --------------------------------------------------------------------------
# The provider factory (R15 / EDW-1337): _make_provider picks the right class
# --------------------------------------------------------------------------


def test_make_provider_returns_an_nvidia_provider_for_provider_nvidia(clean_env: None) -> None:
    """A dummy key, built the same way as the other Settings fixtures in this

    suite, so no real NVIDIA_API_KEY is ever needed to prove the wiring.
    """
    from agent.loop import _make_provider
    from agent.providers.nvidia import NvidiaProvider

    nvidia_settings = Settings(
        _env_file=None,
        honeycomb_ingest_key="fake-ingest-key",
        honeycomb_mcp_key="fake-key-id:fake-secret",
        anthropic_api_key="fake-anthropic-key",
        anthropic_workspace_id="fake-workspace-id",
        nvidia_api_key="fake-nvidia-key",
    )
    config = AgentConfig(provider="nvidia", model="nvidia/nemotron-3-super-120b-a12b")
    made = _make_provider(config, nvidia_settings)
    assert isinstance(made, NvidiaProvider)
    assert made.model == "nvidia/nemotron-3-super-120b-a12b"
