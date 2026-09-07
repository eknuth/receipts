"""Tests for evals/cell.py: reading one result cell.

A synthetic cell in `tmp_path`, graded by the real grader through the
builders in `test_evals_grader.py`. Never the live results.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_evals_grader import RUN_ID, control_report, grade_synthetic, query_call, report

from agent.report import PartialCheck, RejectedCandidate, Report, ToolCall
from evals.cell import call_line, effective_spec, main, query_line, read_cell, render
from evals.run import crashed_run, graded_run, run_dir, write_run


def place(results: Path, column: str, r: Report, repeat: int) -> None:
    graded = graded_run(r, grade_synthetic(r), config=column, repeat=repeat)
    write_run(run_dir(results, column, r.scenario_id, repeat), graded, r)


def test_effective_spec_parses_a_json_string() -> None:
    spec = {"breakdowns": ["cloud.region"]}
    assert effective_spec({"query_spec": spec}) == spec
    assert effective_spec({"query_spec": json.dumps(spec)}) == spec
    assert effective_spec({"query_spec": "{not json"}) == {"query_spec": "{not json"}
    assert effective_spec({"breakdowns": ["x"]}) == {"breakdowns": ["x"]}


def test_query_line_drops_the_run_id_and_shows_the_shape() -> None:
    call = query_call(
        "Q9",
        breakdowns=["cloud.region"],
        filters=[{"column": "deployment.version", "op": "=", "value": "2.5.1"}],
        calculations=[{"op": "COUNT"}, {"op": "P99", "column": "duration_ms"}],
    )
    call.args["query_spec"]["granularity"] = 60
    call.args["query_spec"]["from"] = "2026-09-03T02:00:00Z"
    call.args["query_spec"]["to"] = "2026-09-03T02:20:00Z"
    line = query_line(call, run_id=RUN_ID)
    assert line.startswith("[Q9] run_query CALC COUNT, P99(duration_ms)")
    assert 'WHERE deployment.version = "2.5.1"' in line
    assert "scenario.run_id" not in line
    assert "BY cloud.region" in line
    assert "GRAN 60" in line
    assert "2026-09-03T02:00:00Z -> 2026-09-03T02:20:00Z" in line


def test_query_line_marks_an_error_and_a_nested_filter() -> None:
    call = query_call(
        "Q1",
        calculations=[
            {"op": "COUNT", "column": "error", "filters": [{"column": "error", "op": "exists"}]}
        ],
        is_error=True,
    )
    line = query_line(call, run_id=RUN_ID)
    assert "run_query ERROR" in line
    assert "COUNT(error) WHERE[error exists]" in line


def test_call_line_drops_slugs_and_clips() -> None:
    call = ToolCall(
        name="run_bubbleup",
        args={"dataset_slug": "receipts-shop", "query_pk": "abc", "selection": {"x": "y" * 300}},
        query_id="abc",
    )
    line = call_line(call)
    assert line.startswith("[abc] run_bubbleup {")
    assert "dataset_slug" not in line
    assert line.endswith("...")


def test_render_covers_every_section(tmp_path: Path) -> None:
    results = tmp_path / "results"
    r = report(
        rejected_candidates=[
            RejectedCandidate(
                claim="eu-west-1 db.query is slow",
                dims={"cloud.region": "eu-west-1"},
                reason="pre-existing, flat across the window",
            )
        ],
        partially_checked=[
            PartialCheck(
                subject="cart.size",
                queried_as="broken down over the window",
                reading="over_time",
                not_run="per value",
            )
        ],
        validation_messages=["baseline_window: a note"],
        rejections=["negation: excludes []"],
    )
    place(results, "full", r, 1)
    text = read_cell(results, "full", r.scenario_id, 1)
    for heading in (
        "## Grade",
        "## Report",
        "## Hypotheses",
        "## Baseline evidence",
        "## Rejected candidates",
        "## Partially checked",
        "## Not checked",
        "## Tool log (4 calls)",
    ):
        assert heading in text
    assert "- dims: 1.000 (weighted 0.350)" in text
    assert "1. [high] dims=" in text
    assert "evidence [Q1] P99 180ms to 1100ms" in text
    assert "negation [Q2] P99 flat outside" in text
    assert "- validation: baseline_window: a note" in text
    assert "- rejection 1: negation: excludes []" in text
    assert "eu-west-1 db.query is slow" in text
    assert "cart.size (over_time)" in text
    assert "customer.id, http.route" in text
    assert "[-] get_workspace_context {}" in text
    assert text.count("run_query CALC") == 3


def test_render_shows_rejections_not_recorded_pre_edw_1369(tmp_path: Path) -> None:
    results = tmp_path / "results"
    r = report(rejections=None)
    place(results, "full", r, 1)
    text = read_cell(results, "full", r.scenario_id, 1)
    assert "- rejections: not recorded (run predates EDW-1369)" in text


def test_render_a_control_without_hypotheses(tmp_path: Path) -> None:
    results = tmp_path / "results"
    r = control_report()
    place(results, "full", r, 2)
    text = read_cell(results, "full", r.scenario_id, 2)
    assert "incident_present=False" in text
    assert "## Hypotheses\n\nnone" in text


def test_render_a_crash_without_a_report(tmp_path: Path) -> None:
    graded = crashed_run(config="full", scenario_id="s", run_id="run-x", repeat=1, error="boom")
    directory = tmp_path / "full" / "s" / "1"
    write_run(directory, graded, None)
    text = read_cell(tmp_path, "full", "s", 1)
    assert "error: boom" in text
    assert "no report.json in this cell" in text
    assert render(graded, None) == text


def test_main_prints_and_rejects_a_missing_cell(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results"
    r = report()
    place(results, "full", r, 1)
    assert main(["full", r.scenario_id, "1", "--results-dir", str(results)]) == 0
    assert capsys.readouterr().out.startswith(f"# full/{r.scenario_id}/1 run={RUN_ID}")
    with pytest.raises(SystemExit):
        main(["full", r.scenario_id, "7", "--results-dir", str(results)])
