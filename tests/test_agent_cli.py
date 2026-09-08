"""Tests for agent/__main__.py: the checks that run before anything costs money.

The live run itself is done by hand (`uv run python -m agent --scenario ...`)
and its output goes in the R6 report. What is worth a test is everything the
CLI refuses to do, because those are the mistakes that would otherwise be
found after a paid run.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from agent.__main__ import main, render
from agent.report import Evidence, Hypothesis, Report, ToolCall
from receipts.settings import Settings

MANIFEST = {
    "run_id": "run-test01",
    "scenario_id": "control-quiet",
    "dataset": "receipts-shop",
    "environment": "receipts-demo",
    "mode": "backdate",
    "seed": 0,
    "rps": 15.0,
    "minutes": 20.0,
    "window_start_s": 1.0,
    "window_end_s": 1201.0,
    "window_start": "2026-09-03T02:38:00Z",
    "window_end": "2026-09-03T02:58:00Z",
    "onset_s": None,
    "onset": None,
    "requests": 18000,
    "spans": 90000,
    "exported": 90000,
    "failed_batches": 0,
    "fault_population": 0,
    "faulted_requests": 0,
    "errored_requests": 77,
    "emitted_at": "2026-09-03T02:59:00Z",
}


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "runs"
    directory.mkdir()
    (directory / "run-test01.json").write_text(json.dumps(MANIFEST))
    return directory


def test_an_unknown_scenario_is_a_usage_error(runs_dir: Path) -> None:
    code = main(
        ["--scenario", "not-a-scenario", "--run-id", "run-test01", "--runs-dir", str(runs_dir)]
    )
    assert code == 2


def test_a_missing_manifest_is_a_usage_error(runs_dir: Path) -> None:
    code = main(
        ["--scenario", "control-quiet", "--run-id", "run-nope", "--runs-dir", str(runs_dir)]
    )
    assert code == 2


def test_a_run_from_a_different_scenario_is_refused(
    runs_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Grading a run against the wrong ground truth would score noise."""
    code = main(
        [
            "--scenario",
            "payments-stripe-v251-uswest",
            "--run-id",
            "run-test01",
            "--runs-dir",
            str(runs_dir),
        ]
    )
    assert code == 2
    assert "is a 'control-quiet' run" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("present", "missing"),
    [
        ({"HONEYCOMB_MCP_KEY": "fake-key-id:fake-secret"}, ["ANTHROPIC_API_KEY"]),
        ({"ANTHROPIC_API_KEY": "fake-anthropic-key"}, ["HONEYCOMB_MCP_KEY"]),
        ({}, ["ANTHROPIC_API_KEY", "HONEYCOMB_MCP_KEY"]),
    ],
)
def test_a_missing_key_is_reported_before_anything_is_spent(
    runs_dir: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    present: dict[str, str],
    missing: list[str],
) -> None:
    """No key is required on Settings, so the CLI runs the same pre-flight
    as evals/run.py: exit 2 with every missing key named, before telemetry
    starts or an MCP session opens. The CLI's `Settings()` reads the real
    .env at the repo root, so it is pinned to the shell alone here."""
    import agent.__main__ as module

    monkeypatch.setattr(module, "Settings", functools.partial(Settings, _env_file=None))
    for name, value in present.items():
        monkeypatch.setenv(name, value)

    code = main(
        ["--scenario", "control-quiet", "--run-id", "run-test01", "--runs-dir", str(runs_dir)]
    )

    assert code == 2
    err = capsys.readouterr().err
    for name in missing:
        assert name in err
    assert err.count("error: ") == len(missing)


def test_the_printed_report_shows_the_evidence_and_the_process_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from rich.console import Console

    report = Report(
        run_id="run-test01",
        scenario_id="control-quiet",
        provider="anthropic",
        model="claude-sonnet-4-5",
        incident_present=True,
        hypotheses=[
            Hypothesis(
                claim="one population got slow",
                dims={"deployment.version": "9.9.9"},
                slow_or_failing_span="some.span",
                confidence="high",
                evidence=[Evidence(query_id="Q1", summary="P99 180ms to 1100ms")],
                negation=Evidence(query_id="Q2", summary="flat outside"),
            )
        ],
        not_checked=["customer.id"],
        tool_calls=9,
        tokens_in=1000,
        tokens_out=200,
        cost_usd=0.0123,
        wall_s=42.0,
        tool_log=[ToolCall(name="run_query", query_id="Q1")],
        stop_reason="report",
    )
    render(report, Console(width=100))

    printed = capsys.readouterr().out
    assert "one population got slow" in printed
    assert "Q1" in printed and "Q2" in printed
    assert "customer.id" in printed
    assert "$0.0123" in printed
    assert "mcp calls" in printed
