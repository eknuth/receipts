"""Tests for evals/compare.py: paired-cell comparison of two result columns.

A tiny synthetic results tree in `tmp_path`, graded by the real grader
through the builders in `test_evals_grader.py`. Never the live results.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_evals_grader import CONTROL, PAYMENTS, control_report, grade_synthetic, hypothesis, report

from agent.report import Report
from evals.compare import (
    control_rows,
    load_column,
    main,
    moved_cells,
    paired_keys,
    render,
    scenario_rows,
    summary_line,
    validation_classes,
)
from evals.run import graded_run, run_dir, write_run


def place(results: Path, column: str, r: Report, repeat: int) -> None:
    graded = graded_run(r, grade_synthetic(r), config=column.split("/")[-1], repeat=repeat)
    write_run(run_dir(results, column, r.scenario_id, repeat), graded, r)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """`before/full` parked at two levels, `full` live; two payments cells and one control each.

    The after column fixes the payments/2 hedge and files a false incident on the control.
    """
    results = tmp_path / "results"
    place(results, "before/full", report(), 1)
    place(results, "before/full", report(hypotheses=[hypothesis(confidence="low")]), 2)
    place(results, "before/full", control_report(), 1)
    place(results, "full", report(), 1)
    place(results, "full", report(), 2)
    place(
        results,
        "full",
        control_report(
            incident_present=True,
            hypotheses=[hypothesis(confidence="medium")],
            validation_messages=["not_checked_false: customer.id was queried"],
        ),
        1,
    )
    place(results, "full", report(), 3)
    return results


def test_load_column_keys_by_scenario_and_repeat(tree: Path) -> None:
    before = load_column(tree, "before/full")
    after = load_column(tree, "full")
    assert set(before) == {(PAYMENTS.id, 1), (PAYMENTS.id, 2), (CONTROL.id, 1)}
    assert set(after) == set(before) | {(PAYMENTS.id, 3)}
    assert before[(PAYMENTS.id, 1)].report is not None


def test_paired_keys_are_the_intersection(tree: Path) -> None:
    before, after = load_column(tree, "before/full"), load_column(tree, "full")
    assert paired_keys(before, after) == sorted(before)


def test_moved_cells_reads_the_hedge_and_the_false_incident(tree: Path) -> None:
    before, after = load_column(tree, "before/full"), load_column(tree, "full")
    moved = moved_cells(before, after)
    keys = [key for key, _, _ in moved]
    assert (PAYMENTS.id, 2) in keys and (CONTROL.id, 1) in keys
    assert (PAYMENTS.id, 1) not in keys
    payments = next(item for item in moved if item[0] == (PAYMENTS.id, 2))
    assert payments[2] > payments[1]
    control = next(item for item in moved if item[0] == (CONTROL.id, 1))
    assert control[2] < control[1]


def test_scenario_rows_show_counts_and_delta(tree: Path) -> None:
    before, after = load_column(tree, "before/full"), load_column(tree, "full")
    rows = {row[0]: row for row in scenario_rows(before, after)}
    assert rows[PAYMENTS.id][1].endswith("(2)")
    assert rows[PAYMENTS.id][2].endswith("(3)")
    assert rows[PAYMENTS.id][3].startswith("+")
    assert rows[CONTROL.id][3].startswith("-")


def test_summary_line_counts(tree: Path) -> None:
    after = load_column(tree, "full")
    line = summary_line(after)
    assert "n=4" in line and "val_failed=0" in line and "crashed=0" in line
    assert summary_line({}) == "n=0"


def test_validation_classes_and_controls(tree: Path) -> None:
    after = load_column(tree, "full")
    assert validation_classes(after) == {"not_checked_false": 1}
    rows = control_rows(after)
    assert rows == [[f"{CONTROL.id}/1", "True", "1", "medium", rows[0][4], "report"]]


def test_validation_classes_reads_every_code_out_of_the_wrapped_prose(tmp_path: Path) -> None:
    """EDW-1369: `rejection_message` wraps every rejected attempt's issues in
    intro and outro prose, one `code: message` line per issue. Splitting the
    whole message on its first colon (the old approach) reads the intro
    sentence as the code; this reads the indented per-issue lines instead, and
    does it for `rejections` (every attempt) as well as `validation_messages`
    (the last one)."""
    results = tmp_path / "results"
    wrapped = (
        "The report was rejected. Every claim in it is checked against the log of the tool "
        "calls you made in this session, and these did not hold up:\n\n"
        "  partially_checked_contradicted: partially_checked entry for 'cloud.region' claims "
        "reading per_value, but a query broke down on it.\n"
        "  not_checked_false: not_checked entry 'exception.type' names a column your own "
        "queries used.\n\n"
        "Fix the report and call submit_report again.\n\n"
        "Columns and values your queries used: cloud.region, exception.type"
    )
    place(results, "full", report(rejections=[wrapped, "negation: excludes []"]), 1)
    after = load_column(results, "full")
    assert validation_classes(after, field="rejections") == {
        "negation": 1,
        "not_checked_false": 1,
        "partially_checked_contradicted": 1,
    }
    assert validation_classes(after) == {}


def test_render_includes_the_rejections_table(tree: Path) -> None:
    before, after = load_column(tree, "before/full"), load_column(tree, "full")
    text = render("before/full", "full", before, after)
    assert "## Every rejection by code (fixed or not, EDW-1369)" in text
    assert "none recorded in either column" in text


def test_render_is_a_pure_function_of_the_tree(tree: Path) -> None:
    before, after = load_column(tree, "before/full"), load_column(tree, "full")
    text = render("before/full", "full", before, after)
    assert text == render("before/full", "full", before, after)
    assert "paired n=3" in text
    assert "## Paired cells that moved by 0.05 or more" in text
    assert f"| {CONTROL.id}/1 |" in text


def test_render_with_no_shared_cells(tmp_path: Path) -> None:
    results = tmp_path / "results"
    place(results, "a", report(), 1)
    place(results, "b", control_report(), 1)
    text = render("a", "b", load_column(results, "a"), load_column(results, "b"))
    assert "paired n=0" in text
    assert "none" in text


def test_main_prints_and_rejects_a_missing_column(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["before/full", "full", "--results-dir", str(tree)]) == 0
    assert "# before/full vs full" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["before/full", "nope", "--results-dir", str(tree)])
