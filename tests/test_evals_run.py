"""Tests for evals/run.py: the matrix, the results layout, crashes, caps, and the run index.

No network and no model. The loop runs against the fakes from
`test_agent_loop.py`, and the runner is handed a fake session opener and a
provider factory, so every stop condition is reached on purpose.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_agent_loop import (
    FakeMCP,
    FakeProvider,
    baseline_use,
    completion,
    negation_use,
    query_use,
    report_args,
    use,
)

from agent.loop import SUBMIT_REPORT, AgentConfig
from agent.mcp_client import TokenBucket
from agent.providers.base import Completion, ToolSchema, Turn
from agent.report import Evidence, Hypothesis, Report, load_report
from agent.telemetry import Telemetry
from evals.grader import grade_file
from evals.run import (
    CONFIGS,
    DEFAULT_MAX_WALL_S,
    OLLAMA_DEFAULT_MAX_WALL_S,
    PROVIDERS,
    GradedRun,
    RunIndex,
    agent_config,
    crashed_run,
    graded_run,
    load_run,
    main,
    next_repeat,
    regrade,
    resolve_run,
    resolved_max_wall_s,
    run_dir,
    run_matrix,
    top_permalink,
    write_run,
)
from gen.emit import EmitResult
from gen.scenario import Scenario
from receipts.settings import REPO_ROOT, Settings

PAYMENTS = "payments-stripe-v251-uswest"
CONTROL = "control-quiet"
DEPLOY = "deploy-regression-v260"

FIXTURES = Path(__file__).parent / "fixtures" / "reports"
FIXTURE_RUNS_DIR = FIXTURES / "runs"


# --------------------------------------------------------------------------
# Fakes and fixtures
# --------------------------------------------------------------------------


def manifest(run_id: str, scenario_id: str, emitted_at: str, mode: str = "backdate") -> EmitResult:
    return EmitResult(
        run_id=run_id,
        scenario_id=scenario_id,
        dataset="receipts-shop",
        environment="receipts-demo",
        mode=mode,
        seed=0,
        rps=15.0,
        minutes=20.0,
        window_start_s=1788403040.0,
        window_end_s=1788404240.0,
        window_start="2026-09-03T02:37:20Z",
        window_end="2026-09-03T02:57:20Z",
        onset_s=1788403640.0,
        onset="2026-09-03T02:47:20Z",
        requests=18000,
        spans=90000,
        exported=90000,
        failed_batches=0,
        fault_population=2221,
        faulted_requests=1114,
        errored_requests=92,
        emitted_at=emitted_at,
    )


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "runs"
    for run_id, scenario_id, when in (
        ("run-pay01", PAYMENTS, "2026-09-03T01:00:00Z"),
        ("run-pay02", PAYMENTS, "2026-09-03T02:00:00Z"),
        ("run-ctl01", CONTROL, "2026-09-03T02:00:00Z"),
        ("run-dep01", DEPLOY, "2026-09-03T02:00:00Z"),
    ):
        manifest(run_id, scenario_id, when).write(directory)
    manifest("run-dry01", PAYMENTS, "2026-09-03T03:00:00Z", mode="backdate (dry run)").write(
        directory
    )
    return directory


@pytest.fixture
def results_dir(tmp_path: Path) -> Path:
    return tmp_path / "results"


class FakeSession:
    """`async with open_mcp(settings) as session`, yielding a FakeMCP."""

    def __init__(self) -> None:
        self.mcp = FakeMCP()

    async def __aenter__(self) -> FakeMCP:
        return self.mcp

    async def __aexit__(self, *exc: object) -> None:
        return None


class RaisingProvider:
    name = "fake"
    model = "claude-sonnet-4-5"

    async def complete(
        self, system: str, turns: list[Turn], tools: list[ToolSchema], *, max_tokens: int
    ) -> Completion:
        raise RuntimeError("the provider fell over")


def good_script() -> list[Completion]:
    """A run that files a valid (if wrong) report after three queries."""
    return [
        completion(use("get_workspace_context", ident="a")),
        completion(query_use("b"), negation_use("c"), baseline_use("d")),
        completion(use(SUBMIT_REPORT, report_args(), ident="e")),
    ]


def factory(
    per_call: list[Callable[[], Any]],
) -> Callable[[AgentConfig, Settings], Any]:
    """A provider factory that answers each call from the next entry."""
    calls: list[AgentConfig] = []

    def make(config: AgentConfig, settings: Settings) -> Any:
        calls.append(config)
        return per_call[len(calls) - 1]()

    make.calls = calls  # type: ignore[attr-defined]
    return make


async def run(
    scenarios: list[str],
    configs: list[str],
    repeats: int,
    *,
    settings: Settings,
    results_dir: Path,
    runs_dir: Path,
    provider_factory: Any,
    **kwargs: Any,
) -> list[GradedRun]:
    from io import StringIO

    from rich.console import Console

    return await run_matrix(
        scenarios,
        configs,
        repeats,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        open_mcp=lambda settings: FakeSession(),
        provider_factory=provider_factory,
        console=Console(file=StringIO(), force_terminal=False),
        **kwargs,
    )


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


async def test_each_run_writes_a_report_and_a_grade_by_config_scenario_and_repeat(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    results = await run(
        [PAYMENTS],
        ["full"],
        2,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())] * 2),
    )
    assert [item.repeat for item in results] == [1, 2]
    for repeat in (1, 2):
        directory = run_dir(results_dir, "full", PAYMENTS, repeat)
        assert (directory / "report.json").exists()
        graded = load_run(directory / "grade.json")
        assert graded.run_id == "run-pay02"
        assert graded.grade is not None
        assert graded.total == graded.grade.total
        assert graded.outcome_score == graded.grade.outcome_score
        assert graded.error is None
        assert graded.stop_reason == "report"
        assert graded.tool_calls == 4
        assert graded.cost_usd == graded.grade.process.cost_usd
        assert graded.provider == "fake"
        assert graded.model == "claude-sonnet-4-5"
        assert graded.dims == graded.grade.components.dims
        assert graded.notes == []
    # The report the runner writes is the loop's own, unchanged.
    report = Report.model_validate_json(
        (run_dir(results_dir, "full", PAYMENTS, 1) / "report.json").read_text()
    )
    assert report.run_id == "run-pay02"
    assert report.scenario_id == PAYMENTS


async def test_the_root_span_carries_the_same_total_as_grade_json(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from agent.telemetry import Telemetry

    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        telemetry=telemetry,
    )
    graded = load_run(run_dir(results_dir, "full", PAYMENTS, 1) / "grade.json")

    root = next(
        span
        for span in exporter.get_finished_spans()
        if span.name == "invoke_agent receipts-investigator"
    )
    assert root.attributes["gen_ai.evaluation.result"] == graded.total == results[0].total
    assert root.attributes["receipts.grade.dims"] == graded.grade.components.dims
    assert root.attributes["gen_ai.conversation.id"] == f"{graded.run_id}.full.1"
    assert root.attributes["receipts.run_id"] == graded.run_id
    assert root.attributes["scenario.id"] == PAYMENTS
    assert root.attributes["receipts.config"] == "full"
    assert root.attributes["receipts.stop_reason"] == "report"
    assert root.attributes["receipts.tool_calls"] == graded.tool_calls
    assert root.attributes["receipts.tool_errors"] == 0


async def test_running_the_matrix_again_appends_repeats(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    for _ in range(2):
        await run(
            [CONTROL],
            ["full"],
            1,
            settings=settings,
            results_dir=results_dir,
            runs_dir=runs_dir,
            provider_factory=factory([lambda: FakeProvider(good_script())]),
        )
    assert next_repeat(results_dir, "full", CONTROL) == 3
    assert sorted(p.name for p in (results_dir / "full" / CONTROL).iterdir()) == ["1", "2"]


async def test_one_run_id_serves_every_config_and_repeat(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    make = factory([lambda: FakeProvider(good_script())] * 4)
    results = await run(
        [PAYMENTS],
        ["full", "no-negation"],
        2,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=make,
    )
    assert {item.run_id for item in results} == {"run-pay02"}
    assert [(item.config, item.repeat) for item in results] == [
        ("full", 1),
        ("full", 2),
        ("no-negation", 1),
        ("no-negation", 2),
    ]
    # The config reached the loop as AgentConfig knobs and nothing else.
    assert [config.require_negation for config in make.calls] == [True, True, False, False]
    assert all(config.require_not_checked for config in make.calls)


async def test_resume_fills_the_missing_repeat_and_skips_the_rest(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    from io import StringIO

    from rich.console import Console

    await run(
        [PAYMENTS],
        ["full"],
        2,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())] * 2),
    )
    assert sorted(p.name for p in (results_dir / "full" / PAYMENTS).iterdir()) == ["1", "2"]

    buffer = StringIO()
    console = Console(file=buffer, force_terminal=False)
    results = await run_matrix(
        [PAYMENTS],
        ["full"],
        3,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        open_mcp=lambda settings: FakeSession(),
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        console=console,
        resume=True,
    )
    assert [item.repeat for item in results] == [3]
    output = buffer.getvalue()
    assert "payments-stripe-v251-uswest full 1: done, skipped" in output
    assert "payments-stripe-v251-uswest full 2: done, skipped" in output
    assert sorted(p.name for p in (results_dir / "full" / PAYMENTS).iterdir()) == ["1", "2", "3"]


async def test_without_resume_a_second_invocation_appends_rather_than_fills(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    await run(
        [PAYMENTS],
        ["full"],
        2,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())] * 2),
    )
    results = await run(
        [PAYMENTS],
        ["full"],
        3,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())] * 3),
    )
    assert [item.repeat for item in results] == [3, 4, 5]
    assert sorted(p.name for p in (results_dir / "full" / PAYMENTS).iterdir()) == [
        "1",
        "2",
        "3",
        "4",
        "5",
    ]


async def test_resume_grades_a_stored_report_instead_of_running_it_again(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """A cell with a report and no grade was paid for. Resume grades what is there."""
    from io import StringIO

    from rich.console import Console

    first = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
    )
    directory = run_dir(results_dir, "full", PAYMENTS, 1)
    (directory / "grade.json").unlink()
    stored = (directory / "report.json").read_text()

    def no_provider() -> Any:
        raise AssertionError("resume must not run the investigation again")

    buffer = StringIO()
    console = Console(file=buffer, force_terminal=False)
    results = await run_matrix(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        open_mcp=lambda settings: FakeSession(),
        provider_factory=factory([no_provider]),
        console=console,
        resume=True,
    )
    assert [item.repeat for item in results] == [1]
    assert results[0].total == first[0].total
    assert (directory / "grade.json").exists()
    assert (directory / "report.json").read_text() == stored
    assert "payments-stripe-v251-uswest full 1: graded from the stored report" in buffer.getvalue()


async def test_resume_makes_a_stored_error_report_a_crash_row_not_a_grade(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """An error report on a control would grade as a right "no incident". Resume must
    make of it what run_one made of it: a crash row."""
    await run(
        [CONTROL],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([RaisingProvider]),
    )
    directory = run_dir(results_dir, "full", CONTROL, 1)
    (directory / "grade.json").unlink()
    results = await run(
        [CONTROL],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([RaisingProvider]),
        resume=True,
    )
    assert len(results) == 1
    assert results[0].crashed
    assert results[0].total == 0.0
    assert results[0].stop_reason == "error"
    assert (directory / "grade.json").exists()


async def test_resume_runs_a_cell_again_when_its_stored_report_is_unreadable(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    directory = run_dir(results_dir, "full", PAYMENTS, 1)
    directory.mkdir(parents=True)
    (directory / "report.json").write_text("{not json")
    from io import StringIO

    from rich.console import Console

    buffer = StringIO()
    console = Console(file=buffer, force_terminal=False)
    results = await run_matrix(
        [PAYMENTS],
        ["full"],
        2,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        open_mcp=lambda settings: FakeSession(),
        provider_factory=factory([lambda: FakeProvider(good_script())] * 2),
        console=console,
        resume=True,
    )
    assert [item.repeat for item in results] == [1, 2]
    assert "payments-stripe-v251-uswest full 1: report.json unreadable" in buffer.getvalue()
    assert (directory / "grade.json").exists()


async def test_resume_skips_a_crash_row_and_says_how_to_retry_it(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    await run(
        [CONTROL],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([RaisingProvider]),
    )
    from io import StringIO

    from rich.console import Console

    buffer = StringIO()
    console = Console(file=buffer, force_terminal=False)
    results = await run_matrix(
        [CONTROL],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        open_mcp=lambda settings: FakeSession(),
        provider_factory=factory([RaisingProvider]),
        console=console,
        resume=True,
    )
    assert results == []
    assert "control-quiet full 1: crashed, skipped (delete the directory to retry)" in (
        buffer.getvalue()
    )


def test_configs_map_to_agent_config_knobs_only() -> None:
    assert set(CONFIGS) == {"full", "no-negation", "no-notchecked"}
    assert agent_config("full") == AgentConfig()
    assert agent_config("no-negation") == AgentConfig(require_negation=False)
    assert agent_config("no-notchecked") == AgentConfig(require_not_checked=False)
    assert agent_config("full", model="claude-sonnet-5", max_calls=10).max_calls == 10
    with pytest.raises(KeyError):
        agent_config("no-such-config")


# --------------------------------------------------------------------------
# The ollama provider (R15 / EDW-1337): choices, config, and the wall budget
# --------------------------------------------------------------------------


def test_ollama_is_an_accepted_provider_choice() -> None:
    assert "ollama" in PROVIDERS


def test_agent_config_wires_the_ollama_provider_through() -> None:
    config = agent_config("full", provider="ollama", model="qwen3.8:27b")
    assert config.provider == "ollama"
    assert config.model == "qwen3.8:27b"


def test_an_explicit_max_wall_s_always_wins() -> None:
    assert resolved_max_wall_s("anthropic", 300.0) == 300.0
    assert resolved_max_wall_s("ollama", 300.0) == 300.0


def test_ollama_defaults_to_a_twenty_minute_wall_budget_when_unset() -> None:
    assert resolved_max_wall_s("ollama", None) == OLLAMA_DEFAULT_MAX_WALL_S
    assert OLLAMA_DEFAULT_MAX_WALL_S == 1200.0


def test_every_other_provider_keeps_the_eight_minute_default_when_unset() -> None:
    assert resolved_max_wall_s("anthropic", None) == DEFAULT_MAX_WALL_S
    assert resolved_max_wall_s("bedrock", None) == DEFAULT_MAX_WALL_S


def test_provider_ollama_is_accepted_by_argument_parsing() -> None:
    """`--provider ollama` parses; `--provider nonsense` does not.

    Argument parsing only: this never reaches Settings() or a real run, since
    both would need a real .env and would spend the MCP rate limit or the
    Anthropic API this test suite must not touch.
    """
    import evals.run as module

    args = module._parse_args(["--scenarios", "control-quiet", "--provider", "ollama"])
    assert args.provider == "ollama"
    with pytest.raises(SystemExit):
        module._parse_args(["--scenarios", "control-quiet", "--provider", "nonsense"])


# --------------------------------------------------------------------------
# The nvidia provider (R15 / EDW-1337): choices, config, and the wall budget
# --------------------------------------------------------------------------


def test_nvidia_is_an_accepted_provider_choice() -> None:
    assert "nvidia" in PROVIDERS


def test_agent_config_wires_the_nvidia_provider_through() -> None:
    config = agent_config("full", provider="nvidia", model="nvidia/nemotron-3-super-120b-a12b")
    assert config.provider == "nvidia"
    assert config.model == "nvidia/nemotron-3-super-120b-a12b"


def test_nvidia_keeps_the_eight_minute_default_when_unset() -> None:
    """Only ollama gets a longer default; nvidia is a hosted endpoint, so it

    gets the same eight minute budget every other provider gets.
    """
    assert resolved_max_wall_s("nvidia", None) == DEFAULT_MAX_WALL_S


def test_an_explicit_max_wall_s_always_wins_for_nvidia_too() -> None:
    assert resolved_max_wall_s("nvidia", 300.0) == 300.0


def test_provider_nvidia_is_accepted_by_argument_parsing() -> None:
    """`--provider nvidia` parses, same as `--provider ollama` above."""
    import evals.run as module

    args = module._parse_args(["--scenarios", "control-quiet", "--provider", "nvidia"])
    assert args.provider == "nvidia"


# --------------------------------------------------------------------------
# Crashes and caps
# --------------------------------------------------------------------------


async def test_a_run_that_raises_before_any_call_is_a_zero_row_and_the_matrix_continues(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """The second scenario's provider cannot even be built. The third still runs."""

    def explode() -> Any:
        raise RuntimeError("killed mid-run")

    results = await run(
        [PAYMENTS, CONTROL, DEPLOY],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory(
            [lambda: FakeProvider(good_script()), explode, lambda: FakeProvider(good_script())]
        ),
    )
    assert [item.scenario_id for item in results] == [PAYMENTS, CONTROL, DEPLOY]

    crashed = load_run(run_dir(results_dir, "full", CONTROL, 1) / "grade.json")
    assert crashed.crashed
    assert crashed.total == 0.0
    assert crashed.outcome_score == 0.0
    assert crashed.error == "RuntimeError: killed mid-run"
    assert crashed.stop_reason == "crash"
    assert crashed.top_wrong is True
    assert crashed.grade is None
    assert crashed.honeycomb_process_passed is None
    assert not (run_dir(results_dir, "full", CONTROL, 1) / "report.json").exists()

    third = load_run(run_dir(results_dir, "full", DEPLOY, 1) / "grade.json")
    assert third.grade is not None
    assert third.error is None


class ClosingFailsSession(FakeSession):
    """A session whose close raises after the investigation finished."""

    def __init__(self, calls_before: list[int]) -> None:
        super().__init__()
        self._calls_before = calls_before

    async def __aexit__(self, *exc: object) -> None:
        self._calls_before.append(len(self.mcp.calls))
        raise RuntimeError("cancel scope exited in a different task")


async def test_a_session_that_fails_to_close_after_the_report_is_still_graded(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """The report was filed and paid for. A close error is a note, not a crash."""
    from io import StringIO

    from rich.console import Console

    seen: list[int] = []
    results = await run_matrix(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        open_mcp=lambda settings: ClosingFailsSession(seen),
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        console=Console(file=StringIO(), force_terminal=False),
    )
    graded = results[0]
    assert seen == [4]
    assert not graded.crashed
    assert graded.stop_reason == "report"
    assert graded.error is None
    assert graded.total == graded.grade.total  # type: ignore[union-attr]
    assert graded.notes == [
        "session close failed after the report: RuntimeError: "
        "cancel scope exited in a different task"
    ]
    assert load_run(run_dir(results_dir, "full", PAYMENTS, 1) / "grade.json").notes == graded.notes


async def test_a_crash_after_calls_were_made_records_the_calls_and_the_wall(
    settings: Settings, results_dir: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exception escapes from inside the run after two MCP calls.

    The loop guards the provider and every MCP call, so the escape here is
    simulated at `investigate` itself, which is where a bug in the loop or a
    cancelled task would surface.
    """
    import evals.run as module

    async def dies_mid_run(*args: Any, **kwargs: Any) -> Any:
        mcp = kwargs["mcp"]
        await mcp.call("get_workspace_context", {})
        await mcp.call("run_query", {})
        raise RuntimeError("killed mid-run")

    monkeypatch.setattr(module, "investigate", dies_mid_run)
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
    )
    graded = results[0]
    assert graded.crashed
    assert graded.error == "RuntimeError: killed mid-run"
    assert graded.tool_calls == 2
    assert graded.wall_s >= 0
    assert graded.stop_reason == "crash"
    assert graded.tokens_in == 0 and graded.cost_usd == 0.0
    assert not (run_dir(results_dir, "full", PAYMENTS, 1) / "report.json").exists()


async def test_a_loop_that_ends_in_error_is_a_zero_row_with_the_loops_process_fields(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """The loop catches a provider failure and files an error report. On a
    control that empty report would grade as a correct "no incident", so the
    runner does not grade it."""
    results = await run(
        [CONTROL],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([RaisingProvider]),
    )
    graded = results[0]
    assert graded.crashed
    assert graded.total == 0.0
    assert graded.stop_reason == "error"
    assert graded.error == "RuntimeError: the provider fell over"
    assert graded.tool_calls == 0
    assert graded.wall_s >= 0
    # The error report itself is kept on disk for inspection.
    assert (run_dir(results_dir, "full", CONTROL, 1) / "report.json").exists()


async def test_the_call_cap_ends_a_run_that_is_graded_normally(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider([completion(query_use("q"))])]),
        max_calls=2,
    )
    graded = results[0]
    assert not graded.crashed
    assert graded.stop_reason == "call_cap"
    assert graded.error is None
    assert graded.grade is not None
    assert graded.tool_calls == 2
    assert graded.total == graded.grade.total


async def test_a_cap_ended_run_on_a_control_grades_as_no_answer_and_says_how_it_stopped(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """A model that never files anything on a control scores 0 on the grader:
    the default `incident_present` is not restraint the model showed.

    The runner does not change that: the grader is not touched here, and the
    `stop_reason` in the row is what tells the reader nothing was filed.
    """
    results = await run(
        [CONTROL],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider([completion(query_use("q"))])]),
        max_calls=2,
    )
    graded = results[0]
    assert graded.stop_reason == "call_cap"
    assert not graded.crashed
    assert graded.grade is not None
    assert graded.total == pytest.approx(0.0)
    assert graded.dims == 0.0
    assert graded.top_right is False
    assert graded.top_confidence is None


async def test_the_wall_cap_ends_a_run_that_is_graded_normally(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    ticks = iter(range(0, 100_000, 100))
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider([completion(query_use("q"))])]),
        max_wall_s=250.0,
        clock=lambda: float(next(ticks)),
    )
    graded = results[0]
    assert not graded.crashed
    assert graded.stop_reason == "wall_cap"
    assert graded.error is None
    assert graded.grade is not None


async def test_a_grading_failure_is_a_row_too(
    settings: Settings, results_dir: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evals.run as module

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("the grader disagreed")

    monkeypatch.setattr(module, "grade", broken)
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
    )
    assert results[0].crashed
    assert results[0].error == "ValueError: the grader disagreed"
    # The report was filed, so its process fields are the loop's.
    assert results[0].tool_calls == 4
    assert results[0].stop_reason == "report"


# --------------------------------------------------------------------------
# The run index
# --------------------------------------------------------------------------


async def test_with_emit_each_fresh_run_is_saved_before_the_next_emit(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """A failing second emit must not orphan the spans the first one paid for."""

    def fake_emit(scenario: Scenario, settings: Settings) -> EmitResult:
        if scenario.id == CONTROL:
            raise RuntimeError("ingest refused")
        return manifest("run-new-pay", scenario.id, "2026-09-03T09:00:00Z")

    with pytest.raises(RuntimeError, match="ingest refused"):
        await run(
            [PAYMENTS, CONTROL],
            ["full"],
            1,
            settings=settings,
            results_dir=results_dir,
            runs_dir=runs_dir,
            provider_factory=factory([]),
            emit_first=True,
            emitter=fake_emit,
        )
    saved = RunIndex.load(results_dir / "runs.json")
    assert saved.latest(PAYMENTS).run_id == "run-new-pay"  # type: ignore[union-attr]


def test_the_index_prefers_the_latest_entry_by_emitted_at(runs_dir: Path) -> None:
    index = RunIndex()
    index.add(manifest("run-pay01", PAYMENTS, "2026-09-03T01:00:00Z"))
    index.add(manifest("run-pay02", PAYMENTS, "2026-09-03T02:00:00Z"))
    assert resolve_run(PAYMENTS, index, runs_dir).run_id == "run-pay02"
    assert index.latest(CONTROL) is None


def test_without_an_entry_the_newest_real_manifest_is_used_and_recorded(runs_dir: Path) -> None:
    """The dry run is newer and is skipped: nothing was sent for it."""
    index = RunIndex()
    assert resolve_run(PAYMENTS, index, runs_dir).run_id == "run-pay02"
    assert index.latest(PAYMENTS).run_id == "run-pay02"  # type: ignore[union-attr]


def test_an_index_entry_without_a_manifest_stops_before_anything_runs(
    runs_dir: Path, tmp_path: Path
) -> None:
    index = RunIndex()
    index.add(manifest("run-gone", CONTROL, "2026-09-03T09:00:00Z"))
    with pytest.raises(FileNotFoundError, match="run-gone"):
        resolve_run(CONTROL, index, runs_dir)
    with pytest.raises(FileNotFoundError, match="Emit one"):
        resolve_run("checkout-error-surge-adyen", RunIndex(), runs_dir)


def test_an_index_entry_that_disagrees_with_its_manifest_is_an_error(runs_dir: Path) -> None:
    index = RunIndex()
    index.add(manifest("run-ctl01", PAYMENTS, "2026-09-03T09:00:00Z"))
    with pytest.raises(ValueError, match="manifest says"):
        resolve_run(PAYMENTS, index, runs_dir)


async def test_without_emit_the_matrix_reuses_runs_json(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    index_path = results_dir / "runs.json"
    index = RunIndex()
    index.add(manifest("run-pay01", PAYMENTS, "2026-09-03T05:00:00Z"))
    index.save(index_path)

    make = factory([lambda: FakeProvider(good_script())])
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=make,
        index_path=index_path,
    )
    # runs.json says run-pay01 is the latest, even though a newer manifest exists.
    assert results[0].run_id == "run-pay01"
    saved = RunIndex.load(index_path)
    assert [entry.run_id for entry in saved.runs] == ["run-pay01"]


async def test_a_missing_manifest_stops_the_matrix_before_any_provider_is_built(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    index_path = results_dir / "runs.json"
    index = RunIndex()
    index.add(manifest("run-gone", CONTROL, "2026-09-03T09:00:00Z"))
    index.save(index_path)
    make = factory([lambda: FakeProvider(good_script())] * 2)
    with pytest.raises(FileNotFoundError):
        await run(
            [PAYMENTS, CONTROL],
            ["full"],
            1,
            settings=settings,
            results_dir=results_dir,
            runs_dir=runs_dir,
            provider_factory=make,
            index_path=index_path,
        )
    assert make.calls == []
    assert not results_dir.exists() or not list(results_dir.glob("*/*/*/grade.json"))


async def test_with_emit_a_fresh_run_is_recorded_and_used(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    emitted: list[str] = []

    def fake_emit(scenario: Scenario, settings: Settings) -> EmitResult:
        emitted.append(scenario.id)
        return manifest(f"run-new-{scenario.id[:3]}", scenario.id, "2026-09-03T09:00:00Z")

    results = await run(
        [PAYMENTS, CONTROL],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())] * 2),
        emit_first=True,
        emitter=fake_emit,
    )
    assert emitted == [PAYMENTS, CONTROL]
    assert [item.run_id for item in results] == ["run-new-pay", "run-new-con"]
    assert (runs_dir / "run-new-pay.json").exists()
    saved = RunIndex.load(results_dir / "runs.json")
    assert saved.latest(PAYMENTS).run_id == "run-new-pay"  # type: ignore[union-attr]
    assert saved.latest(CONTROL).run_id == "run-new-con"  # type: ignore[union-attr]


def test_the_index_round_trips_and_sorts(tmp_path: Path) -> None:
    index = RunIndex()
    index.add(manifest("run-b", CONTROL, "2026-09-03T02:00:00Z"))
    index.add(manifest("run-a", PAYMENTS, "2026-09-03T01:00:00Z"))
    index.add(manifest("run-b", CONTROL, "2026-09-03T02:30:00Z"))  # replaced, not duplicated
    path = index.save(tmp_path / "runs.json")
    loaded = RunIndex.load(path)
    assert [(e.run_id, e.emitted_at) for e in loaded.runs] == [
        ("run-b", "2026-09-03T02:30:00Z"),
        ("run-a", "2026-09-03T01:00:00Z"),
    ]
    assert json.loads(path.read_text())["runs"][0]["window_start"] == "2026-09-03T02:37:20Z"


# --------------------------------------------------------------------------
# Permalinks
# --------------------------------------------------------------------------


def _report(**fields: Any) -> Report:
    base: dict[str, Any] = {
        "run_id": "run-x",
        "scenario_id": PAYMENTS,
        "provider": "fake",
        "model": "m",
    }
    base.update(fields)
    return Report(**base)


def test_top_right_is_the_dims_component_against_the_graders_line() -> None:
    def record(dims: float, top_wrong: bool, grade: Any = "x") -> GradedRun:
        return GradedRun(
            config="full",
            scenario_id=PAYMENTS,
            run_id="run-x",
            repeat=1,
            provider="fake",
            model="m",
            total=0.0,
            outcome_score=0.0,
            receipts_score=0.0,
            dims=dims,
            top_wrong=top_wrong,
            top_confidence=None,
            stop_reason="report",
            error=None,
            validation_failed=False,
            permalink=None,
            tool_calls=0,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            wall_s=0.0,
            honeycomb_process_score=None,
            honeycomb_process_passed=None,
            grade=None,
        )

    # No hypothesis on an incident: the grader's flag says not wrong, dims says 0.
    assert record(0.0, top_wrong=False).top_right is False
    assert record(0.5, top_wrong=False).top_right is False  # crashed: grade is None
    crash = crashed_run(config="full", scenario_id=PAYMENTS, run_id="run-x", repeat=1, error="boom")
    assert crash.top_right is False and crash.dims == 0.0


def test_the_permalink_is_the_top_hypothesis_first_evidence_then_the_baseline() -> None:
    hypothesis = Hypothesis(
        claim="c",
        dims={},
        confidence="low",
        evidence=[
            Evidence(query_id="Q1", summary="no link", permalink=None),
            Evidence(query_id="Q2", summary="linked", permalink="https://x/Q2"),
        ],
    )
    baseline = [Evidence(query_id="B1", summary="b", permalink="https://x/B1")]
    assert top_permalink(_report(hypotheses=[hypothesis], baseline_evidence=baseline)) == (
        "https://x/Q2"
    )
    assert top_permalink(_report(baseline_evidence=baseline)) == "https://x/B1"
    assert top_permalink(_report()) is None
    assert (
        top_permalink(_report(hypotheses=[Hypothesis(claim="c", dims={}, confidence="low")]))
        is None
    )


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------


def test_results_are_ignored_except_runs_json() -> None:
    def ignored(relative: str) -> bool:
        # --no-index, or a tracked file is never reported and the runs.json
        # assertion would pass with the negation line deleted.
        proc = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", relative],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        return proc.returncode == 0

    assert ignored("evals/results/full/control-quiet/1/grade.json")
    assert ignored("evals/results/full/control-quiet/1/report.json")
    assert ignored("evals/results/run-4155490e2a44/report.json")
    assert not ignored("evals/results/runs.json")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_usage_errors_exit_2_before_anything_runs(tmp_path: Path) -> None:
    # Pointed at empty directories so a guard that moves after Settings()
    # fails on a missing manifest instead of starting a paid run.
    safe = ["--results-dir", str(tmp_path / "results"), "--runs-dir", str(tmp_path / "runs")]
    assert main(["--scenarios", "not-a-scenario", *safe]) == 2
    assert main(["--scenarios", CONTROL, "--configs", "not-a-config", *safe]) == 2
    assert main(["--scenarios", CONTROL, "--repeats", "0", *safe]) == 2
    assert main(["--scenarios", CONTROL, "--provider", "bedrock", *safe]) == 2
    assert main(["--scenarios", CONTROL, "--resume", "--emit", *safe]) == 2


def test_scenarios_is_required_unless_regrade_is_given(tmp_path: Path) -> None:
    safe = ["--results-dir", str(tmp_path / "results"), "--runs-dir", str(tmp_path / "runs")]
    assert main(safe) == 2


# --------------------------------------------------------------------------
# --regrade
# --------------------------------------------------------------------------


def _seeded_cell(results_dir: Path, config: str = "full", repeat: int = 1) -> tuple[Path, float]:
    """One real graded cell, filed the way the runner files it. Returns the
    grade.json path and the total a fresh grade actually computes."""
    report_path = FIXTURES / "run-4155490e2a44" / "report.json"
    report = load_report(report_path)
    result = grade_file(report_path, runs_dir=FIXTURE_RUNS_DIR)
    graded = graded_run(report, result, config=config, repeat=repeat)
    directory = run_dir(results_dir, config, report.scenario_id, repeat)
    write_run(directory, graded, report)
    return directory / "grade.json", result.total


def _make_stale(grade_path: Path, stale_total: float = -9.0) -> None:
    """Overwrite a grade.json's total, standing in for one written before a
    grader change, without touching its report.json."""
    data = json.loads(grade_path.read_text())
    data["total"] = stale_total
    data["grade"]["total"] = stale_total
    grade_path.write_text(json.dumps(data))


def test_regrade_rebuilds_a_stale_grade_and_reports_the_change(tmp_path: Path) -> None:
    grade_path, real_total = _seeded_cell(tmp_path)
    _make_stale(grade_path)

    changed = regrade(tmp_path, FIXTURE_RUNS_DIR)

    assert changed == [(grade_path, -9.0, real_total)]
    assert load_run(grade_path).total == pytest.approx(real_total)


def test_regrade_leaves_an_unchanged_grade_out_of_the_report_and_rewrites_it_anyway(
    tmp_path: Path,
) -> None:
    grade_path, real_total = _seeded_cell(tmp_path)
    before = grade_path.read_text()

    changed = regrade(tmp_path, FIXTURE_RUNS_DIR)

    assert changed == []
    assert load_run(grade_path).total == pytest.approx(real_total)
    assert grade_path.read_text() == before  # a pure function of the same report and scenario


def test_regrade_leaves_a_crashed_cell_alone(tmp_path: Path) -> None:
    crash = crashed_run(config="full", scenario_id=CONTROL, run_id="run-x", repeat=1, error="boom")
    directory = run_dir(tmp_path, "full", CONTROL, 1)
    write_run(directory, crash, None)  # no report.json, the way a bare crash writes
    before = (directory / "grade.json").read_text()

    changed = regrade(tmp_path, FIXTURE_RUNS_DIR)

    assert changed == []
    assert (directory / "grade.json").read_text() == before


def test_regrade_via_the_cli_needs_no_scenarios_and_prints_the_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    grade_path, real_total = _seeded_cell(tmp_path)
    _make_stale(grade_path)

    assert (
        main(["--regrade", "--results-dir", str(tmp_path), "--runs-dir", str(FIXTURE_RUNS_DIR)])
        == 0
    )
    out = capsys.readouterr().out
    assert "-9.000 -> " in out
    assert f"{real_total:.3f}" in out


def test_regrade_via_the_cli_says_so_when_nothing_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seeded_cell(tmp_path)
    assert (
        main(["--regrade", "--results-dir", str(tmp_path), "--runs-dir", str(FIXTURE_RUNS_DIR)])
        == 0
    )
    assert "no grades changed" in capsys.readouterr().out


# --------------------------------------------------------------------------
# --handoff (R12, EDW-1334)
# --------------------------------------------------------------------------


@dataclass
class _WriteResult:
    raw: Any
    text: str = ""
    is_error: bool = False


def _fake_boards_markdown(boards: list[dict[str, Any]]) -> str:
    """A `list_boards`-shaped page: real header, one row per board, a
    `Metadata:` block with `total_pages: 1` (every board fits on one page in
    these tests). Matches the live shape in `tests/fixtures/mcp/list_boards.json`."""
    lines = ["# Boards", ""]
    if boards:
        lines.append(
            "| ID | Name | Description | Private | QueryCount | SLOCount | TextCount "
            "| UpdatedAt | Tags |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for b in boards:
            lines.append(
                f"| {b['id']} | {b['name']} |  | false | 0 | 0 | 1 | 2026-09-07T00:00:00Z |  |"
            )
    lines += [
        "",
        "---",
        "Metadata:",
        "  environment: receipts-demo",
        "  page: 1",
        f"  total_items: {len(boards)}",
        "  total_pages: 1",
    ]
    return "\n".join(lines)


def _fake_created_board_markdown(board_id: str, board_url: str, name: str) -> str:
    """A `create_board`-shaped success reply, matching the live shape in
    `tests/fixtures/mcp/create_board.json`."""
    return (
        "Board created successfully.\n\n---\nMetadata:\n"
        f"  board_id: {board_id}\n"
        f"  board_name: {name}\n"
        f'  board_url: "{board_url}"\n'
        "  environment: receipts-demo\n"
        "  text_count: 1\n"
    )


@dataclass
class FakeWriteMCP:
    """Enough of a write-capable HoneycombMCP for `agent/board.py` and
    `agent/handoff.py`: `list_boards`, `create_board`, `canvas_agent_invoke`,
    and `canvas_agent_poll_response`, answered from fixed, successful
    responses. `boards` starts empty so the first `ensure_board` call always
    creates one.

    `list_boards` and `create_board` answer with real-shaped Markdown text
    (see `agent/board.py`'s module docstring: both are Markdown, not JSON,
    confirmed live 2026-09-07); `canvas_agent_invoke` and
    `canvas_agent_poll_response` still answer with a JSON `raw` dict, since
    those two are JSON on the wire.
    """

    boards: list[dict[str, Any]] = field(default_factory=list)
    reply: str = "I agree with this."
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    kwargs_seen: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    created_count: int = 0

    async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls.append((name, dict(args or {})))
        self.kwargs_seen.append((name, dict(kwargs)))
        if name == "list_boards":
            return _WriteResult(raw=None, text=_fake_boards_markdown(self.boards))
        if name == "create_board":
            self.created_count += 1
            board_id = f"brd-{self.created_count}"
            board_url = f"https://ui.honeycomb.io/team/boards/{board_id}"
            name_arg = (args or {})["name"]
            self.boards.append({"id": board_id, "name": name_arg})
            return _WriteResult(
                raw=None, text=_fake_created_board_markdown(board_id, board_url, name_arg)
            )
        if name == "canvas_agent_invoke":
            return _WriteResult(
                raw={
                    "status": "running",
                    "session_id": "sess-1",
                    "investigation_id": "hcciv_1",
                    "investigation_url": "https://ui.honeycomb.io/team/canvas/1",
                    "investigation_created": True,
                }
            )
        if name == "canvas_agent_poll_response":
            return _WriteResult(raw={"status": "completed", "chat": self.reply})
        raise AssertionError(f"unexpected call to {name!r}")


class FakeWriteSession:
    """`async with open_write_mcp(settings) as session`, yielding a FakeWriteMCP."""

    def __init__(self, mcp: FakeWriteMCP | None = None) -> None:
        self.mcp = mcp or FakeWriteMCP()

    async def __aenter__(self) -> FakeWriteMCP:
        return self.mcp

    async def __aexit__(self, *exc: object) -> None:
        return None


def _grade_json_ignoring_wall_time(directory: Path) -> dict[str, Any]:
    """`grade.json`'s content with every real-elapsed-time field zeroed out,
    so two separate runs of the same deterministic script can be compared
    for equality without flaking on how long each one actually took."""
    data = json.loads((directory / "grade.json").read_text())
    data["wall_s"] = None
    if data.get("grade") is not None:
        data["grade"]["process"]["wall_s"] = None
    return data


async def test_handoff_writes_a_handoff_json_next_to_grade_json(
    settings: Settings, results_dir: Path, runs_dir: Path, tmp_path: Path
) -> None:
    write_session = FakeWriteSession()
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        handoff=True,
        open_write_mcp=lambda settings: write_session,
    )

    # A handoff must never move the grade it is attached to. The old
    # assertion here was `results[0].total == results[0].total`, which
    # holds no matter what `hand_off` does; rerun the identical script with
    # handoff off and diff the two grade.json files instead, so a handoff
    # that somehow perturbed grading would actually be caught.
    baseline_dir = tmp_path / "baseline-results"
    baseline_results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=baseline_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
    )
    assert results[0].total == baseline_results[0].total
    assert _grade_json_ignoring_wall_time(
        run_dir(results_dir, "full", PAYMENTS, 1)
    ) == _grade_json_ignoring_wall_time(run_dir(baseline_dir, "full", PAYMENTS, 1))

    handoff_path = run_dir(results_dir, "full", PAYMENTS, 1) / "handoff.json"
    assert handoff_path.exists()
    data = json.loads(handoff_path.read_text())
    assert data["classification"] == "agree"
    assert data["raw_text"] == "I agree with this."
    assert data["status"] == "completed"
    assert data["board_id"] == "brd-1"
    assert data["board_url"] == "https://ui.honeycomb.io/team/boards/brd-1"
    assert data["prompt"]  # the message that was sent is kept too


async def test_without_the_handoff_flag_no_handoff_json_is_written(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
    )
    assert not (run_dir(results_dir, "full", PAYMENTS, 1) / "handoff.json").exists()


async def test_handoff_does_not_create_a_second_board_for_a_second_repeat_of_the_same_run(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """Repeats of one scenario share a run id (one emit serves every repeat),
    so the board they hand off to should be the one board, not one per repeat."""
    write_session = FakeWriteSession()
    await run(
        [PAYMENTS],
        ["full"],
        2,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())] * 2),
        handoff=True,
        open_write_mcp=lambda settings: write_session,
    )
    assert write_session.mcp.created_count == 1
    first = json.loads((run_dir(results_dir, "full", PAYMENTS, 1) / "handoff.json").read_text())
    second = json.loads((run_dir(results_dir, "full", PAYMENTS, 2) / "handoff.json").read_text())
    assert first["board_id"] == second["board_id"] == "brd-1"


async def test_a_crashed_run_gets_no_handoff_json(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    write_session = FakeWriteSession()
    await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([RaisingProvider]),
        handoff=True,
        open_write_mcp=lambda settings: write_session,
    )
    assert not (run_dir(results_dir, "full", PAYMENTS, 1) / "handoff.json").exists()
    assert write_session.mcp.calls == []  # never even opened for a crash


async def test_a_failing_write_session_still_records_a_handoff_and_never_raises(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    class RaisingWriteSession:
        async def __aenter__(self) -> Any:
            raise RuntimeError("write session could not open")

        async def __aexit__(self, *exc: object) -> None:
            return None

    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        handoff=True,
        open_write_mcp=lambda settings: RaisingWriteSession(),
    )
    assert results[0].error is None  # the run itself is unaffected
    data = json.loads((run_dir(results_dir, "full", PAYMENTS, 1) / "handoff.json").read_text())
    assert data["status"] == "error"
    assert data["classification"] == "no_response"
    assert "write session could not open" in data["error"]


# --------------------------------------------------------------------------
# Handoff telemetry (R23, EDW-1370)
# --------------------------------------------------------------------------


async def test_handoff_tools_get_execute_tool_spans_in_the_investigations_conversation(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """`canvas_agent_invoke`, `canvas_agent_poll_response`, `create_board`,
    and `list_boards` each get an `execute_tool` span (`FakeWriteMCP`'s
    `boards` starts empty, so `ensure_board` makes exactly one of each),
    and every one of them, plus the handoff's own root, shares
    `gen_ai.conversation.id` with the investigation that produced the
    report."""
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    write_session = FakeWriteSession()
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        handoff=True,
        open_write_mcp=lambda settings: write_session,
        telemetry=telemetry,
    )
    telemetry.flush()

    conversation_id = f"{results[0].run_id}.full.1"
    spans = exporter.get_finished_spans()

    investigation_root = next(s for s in spans if s.name == "invoke_agent receipts-investigator")
    assert investigation_root.attributes["gen_ai.conversation.id"] == conversation_id

    handoff_root = next(s for s in spans if s.name == "invoke_agent canvas")
    assert handoff_root.attributes["gen_ai.conversation.id"] == conversation_id
    assert handoff_root.attributes["receipts.config"] == "full"
    assert handoff_root.attributes["gen_ai.provider.name"] == "anthropic"

    expected_tools = {
        "canvas_agent_invoke",
        "canvas_agent_poll_response",
        "create_board",
        "list_boards",
    }
    tool_spans = {
        span.attributes["gen_ai.tool.name"]: span
        for span in spans
        if span.name.startswith("execute_tool ")
        and span.attributes["gen_ai.tool.name"] in expected_tools
    }
    assert set(tool_spans) == expected_tools
    for span in tool_spans.values():
        assert span.attributes["gen_ai.conversation.id"] == conversation_id
        assert span.attributes["gen_ai.tool.call.arguments"]


async def test_handoff_calls_carry_the_tool_spans_traceparent_and_tracestate(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """`agent/telemetry.py`'s `traced_call` must pass `traceparent`/
    `tracestate` to `mcp.call` the same way `agent/loop.py` does for the
    investigation's own calls (CLAUDE.md: every call carries it), for each
    of the four handoff and board tools. Telemetry must be a real (in-memory)
    one, not the disabled default: a disabled trace's spans have no valid
    context to propagate, so `traceparent` would be `None` regardless of
    whether `traced_call` passes it through, and this test would prove
    nothing."""
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    write_session = FakeWriteSession()
    await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        handoff=True,
        open_write_mcp=lambda settings: write_session,
        telemetry=telemetry,
    )
    telemetry.flush()

    seen = write_session.mcp.kwargs_seen
    expected_tools = {
        "canvas_agent_invoke",
        "canvas_agent_poll_response",
        "create_board",
        "list_boards",
    }
    called_tools = {name for name, _ in seen}
    assert expected_tools <= called_tools
    for name, kwargs in seen:
        if name in expected_tools:
            # tracestate is commonly empty (W3C leaves it optional), so only
            # presence is checked; traceparent is always non-empty, the
            # same as `test_the_traceparent_reaches_the_mcps_call_kwargs`
            # checks for the investigation's own calls.
            assert "traceparent" in kwargs and kwargs["traceparent"], f"{name} missing traceparent"
            assert "tracestate" in kwargs, f"{name} missing a tracestate kwarg"


async def test_handoff_root_span_carries_classification_reply_and_board_link(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    write_session = FakeWriteSession(FakeWriteMCP(reply="I agree with this."))
    await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        handoff=True,
        open_write_mcp=lambda settings: write_session,
        telemetry=telemetry,
    )
    telemetry.flush()

    spans = exporter.get_finished_spans()
    handoff_root = next(s for s in spans if s.name == "invoke_agent canvas")
    assert handoff_root.attributes["receipts.handoff.status"] == "completed"
    assert handoff_root.attributes["receipts.handoff.classification"] == "agree"
    assert "I agree with this." in handoff_root.attributes["receipts.handoff.reply"]
    assert handoff_root.attributes["receipts.board.id"] == "brd-1"
    assert handoff_root.attributes["receipts.board.url"] == (
        "https://ui.honeycomb.io/team/boards/brd-1"
    )


async def test_handoff_with_telemetry_disabled_still_hands_off_and_writes_json(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """`Telemetry()` with no settings and no exporter is disabled: the
    handoff must still run to completion and `handoff.json` must still be
    written, the same as with no `telemetry` argument at all."""
    write_session = FakeWriteSession()
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        handoff=True,
        open_write_mcp=lambda settings: write_session,
        telemetry=Telemetry(),
    )
    assert results[0].error is None
    handoff_path = run_dir(results_dir, "full", PAYMENTS, 1) / "handoff.json"
    assert handoff_path.exists()
    data = json.loads(handoff_path.read_text())
    assert data["status"] == "completed"
    assert data["board_id"] == "brd-1"


async def test_a_canvas_error_leaves_grade_json_byte_identical_to_no_handoff(
    settings: Settings, results_dir: Path, runs_dir: Path, tmp_path: Path
) -> None:
    """A Canvas error (here: `canvas_agent_invoke` raises) must not perturb
    the grade it is attached to, pinned the same way
    `test_handoff_writes_a_handoff_json_next_to_grade_json` pins a
    successful handoff: rerun the identical script with handoff off and
    diff the two `grade.json` files."""

    class ErroringWriteMCP(FakeWriteMCP):
        async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            if name == "canvas_agent_invoke":
                raise RuntimeError("canvas is down")
            return await super().call(name, args, **kwargs)

    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    write_session = FakeWriteSession(ErroringWriteMCP())
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        handoff=True,
        open_write_mcp=lambda settings: write_session,
        telemetry=telemetry,
    )
    telemetry.flush()

    baseline_dir = tmp_path / "baseline-results"
    baseline_results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=baseline_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
    )
    assert results[0].total == baseline_results[0].total
    assert _grade_json_ignoring_wall_time(
        run_dir(results_dir, "full", PAYMENTS, 1)
    ) == _grade_json_ignoring_wall_time(run_dir(baseline_dir, "full", PAYMENTS, 1))

    handoff_data = json.loads(
        (run_dir(results_dir, "full", PAYMENTS, 1) / "handoff.json").read_text()
    )
    assert handoff_data["status"] == "error"
    assert handoff_data["classification"] == "no_response"

    spans = exporter.get_finished_spans()
    handoff_root = next(s for s in spans if s.name == "invoke_agent canvas")
    assert handoff_root.status.status_code.name == "ERROR"
    assert handoff_root.attributes["receipts.handoff.status"] == "error"


async def test_a_canvas_timeout_leaves_grade_json_byte_identical_to_no_handoff(
    settings: Settings,
    results_dir: Path,
    runs_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same pin as the error-path test above, for the deadline-expiring
    path instead of a raised exception: `hand_off`'s real deadline is
    forced to 0 (`FakeWriteMCP`'s `canvas_agent_invoke` answers `running`,
    so the poll loop is entered, and by the time it checks the deadline
    real time has already passed it, no faked clock needed), which must
    still leave the grade untouched."""
    import evals.run as run_module
    from agent.handoff import hand_off as real_hand_off

    async def zero_deadline_hand_off(*args: Any, **kwargs: Any) -> Any:
        kwargs["deadline_s"] = 0.0
        return await real_hand_off(*args, **kwargs)

    monkeypatch.setattr(run_module, "hand_off", zero_deadline_hand_off)

    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    write_session = FakeWriteSession()
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        handoff=True,
        open_write_mcp=lambda settings: write_session,
        telemetry=telemetry,
    )
    telemetry.flush()

    baseline_dir = tmp_path / "baseline-results"
    baseline_results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=baseline_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
    )
    assert results[0].total == baseline_results[0].total
    assert _grade_json_ignoring_wall_time(
        run_dir(results_dir, "full", PAYMENTS, 1)
    ) == _grade_json_ignoring_wall_time(run_dir(baseline_dir, "full", PAYMENTS, 1))

    handoff_data = json.loads(
        (run_dir(results_dir, "full", PAYMENTS, 1) / "handoff.json").read_text()
    )
    assert handoff_data["status"] == "timeout"
    assert handoff_data["classification"] == "no_response"

    spans = exporter.get_finished_spans()
    handoff_root = next(s for s in spans if s.name == "invoke_agent canvas")
    assert handoff_root.status.status_code.name == "ERROR"
    assert handoff_root.attributes["receipts.handoff.status"] == "timeout"


async def test_no_span_in_the_handoff_trace_carries_anything_oauth_shaped(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """The handoff runs over OAuth, a different identity from the
    investigation's own key-based sessions (CLAUDE.md's Honeycomb facts);
    nothing identity-shaped may land on any span this trace produces. The
    scenario id is not checked here: the investigation's own root already
    carries `scenario.id` on purpose (`RunTrace.end`), closed before this
    trace opens, so a handoff span cannot expose anything the conversation
    does not already show (see `Telemetry.start_handoff`)."""
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    write_session = FakeWriteSession()
    results = await run(
        [PAYMENTS],
        ["full"],
        1,
        settings=settings,
        results_dir=results_dir,
        runs_dir=runs_dir,
        provider_factory=factory([lambda: FakeProvider(good_script())]),
        handoff=True,
        open_write_mcp=lambda settings: write_session,
        telemetry=telemetry,
    )
    telemetry.flush()

    assert results[0].scenario_id == PAYMENTS

    spans = exporter.get_finished_spans()
    handoff_root = next(s for s in spans if s.name == "invoke_agent canvas")
    handoff_trace_id = handoff_root.context.trace_id
    handoff_spans = [s for s in spans if s.context.trace_id == handoff_trace_id]
    assert len(handoff_spans) >= 4  # the root plus at least the four tool spans

    forbidden = [
        "Bearer",
        "access_token",
        "refresh_token",
        settings.honeycomb_mcp_key.get_secret_value(),
    ]
    for span in handoff_spans:
        for key, value in span.attributes.items():
            text = str(value)
            for needle in forbidden:
                assert needle not in text, f"{needle!r} found in {key}={text!r}"


async def test_hand_off_cells_default_write_session_uses_the_given_bucket(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_hand_off_cell`'s own fallback session (built only when a caller
    invokes it, or `run_one`, directly without an `open_write_mcp`) used to
    construct a plain `HoneycombMCP()` with no bucket at all: a fresh,
    disconnected `TokenBucket` on every call, able to exceed the team-wide
    rate limit on its own no matter how the matrix's read sessions were
    already paced. Threading `write_bucket` through must make that
    fallback share one bucket across cells instead of minting a new one
    every time."""
    import evals.run as run_module

    captured: list[Any] = []

    class FakeHoneycombMCP:
        def __init__(
            self,
            *,
            settings: Any,
            allow_write: bool = False,
            bucket: Any = None,
            http_client: Any = None,
        ) -> None:
            captured.append(bucket)
            self._mcp = FakeWriteMCP()

        async def __aenter__(self) -> Any:
            return self._mcp

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(run_module, "HoneycombMCP", FakeHoneycombMCP)
    shared_bucket = TokenBucket()

    await run_module._hand_off_cell(tmp_path, _report(), settings, None, shared_bucket)
    await run_module._hand_off_cell(tmp_path, _report(), settings, None, shared_bucket)

    assert captured == [shared_bucket, shared_bucket]


def test_handoff_flag_is_off_by_default_in_the_cli() -> None:
    from evals.run import _parse_args

    args = _parse_args(["--scenarios", "all"])
    assert args.handoff is False


def test_handoff_with_no_oauth_token_fails_fast_and_names_the_login_command(
    tmp_path: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--handoff` needs Canvas, which needs a usable Honeycomb OAuth token
    on file (a management key has no user actor); with none, the CLI must
    say so and exit before opening any session, not run the whole matrix
    first and record an OAuthNotAuthorized crash on every cell. The
    precondition is a token on file, not `HONEYCOMB_AUTH=oauth` in
    settings: `_SharedSessions.write` forces OAuth on the write session it
    opens regardless, so the read sessions the investigation itself uses
    never need to move off the key."""
    monkeypatch.setenv("HONEYCOMB_MCP_KEY", "fake-key-id:fake-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "fake-workspace-id")
    monkeypatch.setenv("HONEYCOMB_OAUTH_TOKEN_PATH", str(tmp_path / "no-such-token.json"))
    safe = ["--results-dir", str(tmp_path / "results"), "--runs-dir", str(tmp_path / "runs")]

    assert main(["--scenarios", CONTROL, "--handoff", *safe]) == 2

    err = capsys.readouterr().err
    assert "agent.auth login" in err
    assert not (tmp_path / "results").exists()  # the matrix never started


def test_handoff_with_a_malformed_token_file_fails_the_same_clear_way_as_missing(
    tmp_path: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A token file whose `tokens` block fails pydantic validation (an
    `access_token` that is a number, say) used to raise `ValidationError`
    out of `main`: exit 1, a stack trace, no login command named. It must
    fail the same way a missing token file does: exit 2, no matrix run, the
    login command named."""
    token_path = tmp_path / "malformed-token.json"
    token_path.write_text(json.dumps({"tokens": {"access_token": 5, "token_type": []}}))
    monkeypatch.setenv("HONEYCOMB_MCP_KEY", "fake-key-id:fake-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "fake-workspace-id")
    monkeypatch.setenv("HONEYCOMB_OAUTH_TOKEN_PATH", str(token_path))
    safe = ["--results-dir", str(tmp_path / "results"), "--runs-dir", str(tmp_path / "runs")]

    assert main(["--scenarios", CONTROL, "--handoff", *safe]) == 2

    err = capsys.readouterr().err
    assert "agent.auth login" in err
    assert not (tmp_path / "results").exists()  # the matrix never started


async def test_handoff_with_the_default_key_setting_still_reads_off_the_key(
    settings: Settings, results_dir: Path, runs_dir: Path
) -> None:
    """The precondition check above only asks whether a token is on file;
    it must not be satisfied by flipping `HONEYCOMB_AUTH` to `"oauth"`,
    since that used to also flip every read session in the matrix onto
    OAuth. `_SharedSessions.write` is what actually forces OAuth, and only
    for the write session it opens for the board and Canvas calls."""
    from evals.run import _SharedSessions

    assert settings.honeycomb_auth == "key"
    shared = _SharedSessions()

    read_session = shared(settings)
    write_session = shared.write(settings)

    assert read_session._settings.honeycomb_auth == "key"
    assert write_session._settings.honeycomb_auth == "oauth"
    assert read_session._bucket is shared.bucket
    assert write_session._bucket is shared.bucket
