"""Tests for evals/run.py: the matrix, the results layout, crashes, caps, and the run index.

No network and no model. The loop runs against the fakes from
`test_agent_loop.py`, and the runner is handed a fake session opener and a
provider factory, so every stop condition is reached on purpose.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
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
from agent.providers.base import Completion, ToolSchema, Turn
from agent.report import Evidence, Hypothesis, Report
from evals.run import (
    CONFIGS,
    GradedRun,
    RunIndex,
    agent_config,
    load_run,
    main,
    next_repeat,
    resolve_run,
    run_dir,
    run_matrix,
    top_permalink,
)
from gen.emit import EmitResult
from gen.scenario import Scenario
from receipts.settings import REPO_ROOT, Settings

PAYMENTS = "payments-stripe-v251-uswest"
CONTROL = "control-quiet"
DEPLOY = "deploy-regression-v260"


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
    # The report the runner writes is the loop's own, unchanged.
    report = Report.model_validate_json(
        (run_dir(results_dir, "full", PAYMENTS, 1) / "report.json").read_text()
    )
    assert report.run_id == "run-pay02"
    assert report.scenario_id == PAYMENTS


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


def test_configs_map_to_agent_config_knobs_only() -> None:
    assert set(CONFIGS) == {"full", "no-negation", "no-notchecked"}
    assert agent_config("full") == AgentConfig()
    assert agent_config("no-negation") == AgentConfig(require_negation=False)
    assert agent_config("no-notchecked") == AgentConfig(require_not_checked=False)
    assert agent_config("full", model="claude-sonnet-5", max_calls=10).max_calls == 10
    with pytest.raises(KeyError):
        agent_config("no-such-config")


# --------------------------------------------------------------------------
# Crashes and caps
# --------------------------------------------------------------------------


async def test_a_crash_is_a_zero_row_and_the_matrix_continues(
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
        proc = subprocess.run(
            ["git", "check-ignore", "-q", relative], cwd=REPO_ROOT, capture_output=True
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
    assert main(["--scenarios", "not-a-scenario"]) == 2
    assert main(["--scenarios", CONTROL, "--configs", "not-a-config"]) == 2
    assert main(["--scenarios", CONTROL, "--repeats", "0"]) == 2
    assert main(["--scenarios", CONTROL, "--provider", "bedrock"]) == 2
