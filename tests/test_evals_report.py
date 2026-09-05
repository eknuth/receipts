"""Tests for evals/report.py: the markdown report, pinned to the eight live reports.

The fixture `tests/fixtures/reports/report.md` is what the renderer produces
from the eight live reports in `tests/fixtures/reports/`, each graded by the
grader and filed under config `full` by repeat number. Regenerate it with

    uv run python -m tests.test_evals_report

after a deliberate change to the renderer, and read the diff before
committing it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from agent.report import load_report
from evals.grader import grade_file
from evals.report import (
    OUTCOME_FAIL_BELOW,
    config_order,
    load_results,
    main,
    num,
    render,
    write_report,
)
from evals.run import GradedRun, crashed_run, graded_run, run_dir, write_run

FIXTURES = Path(__file__).parent / "fixtures" / "reports"
RUNS_DIR = FIXTURES / "runs"
PINNED = FIXTURES / "report.md"


def _run(
    config: str, scenario_id: str, total: float, outcome_score: float, repeat: int = 1
) -> GradedRun:
    """A minimal `GradedRun` for exercising the renderer directly, with no report or grade
    behind it. `render` only reads the flat fields, so a run built this way is enough to
    drive the ablation delta section without going through the grader or the disk layout."""
    return GradedRun(
        config=config,
        scenario_id=scenario_id,
        run_id=f"run-{config}-{repeat}",
        repeat=repeat,
        provider="fake",
        model="m",
        total=total,
        outcome_score=outcome_score,
        receipts_score=0.0,
        dims=0.0,
        top_wrong=True,
        top_confidence=None,
        stop_reason="report",
        error=None,
        validation_failed=False,
        permalink=None,
        tool_calls=1,
        tokens_in=1,
        tokens_out=1,
        cost_usd=0.1,
        wall_s=1.0,
        honeycomb_process_score=None,
        honeycomb_process_passed=None,
        grade=None,
    )


def _repeat_of(path: Path) -> int:
    match = re.fullmatch(r"report(?:-(\d+))?\.json", path.name)
    assert match is not None, path
    return int(match.group(1) or 1)


def build_live_results(results_dir: Path, config: str = "full") -> list[GradedRun]:
    """The eight live reports, graded and filed the way the runner files them."""
    graded: list[GradedRun] = []
    for path in sorted(FIXTURES.glob("run-*/report*.json")):
        report = load_report(path)
        result = grade_file(path, runs_dir=RUNS_DIR)
        item = graded_run(report, result, config=config, repeat=_repeat_of(path))
        write_run(run_dir(results_dir, config, report.scenario_id, item.repeat), item, report)
        graded.append(item)
    return graded


@pytest.fixture
def live_results(tmp_path: Path) -> Path:
    results_dir = tmp_path / "results"
    build_live_results(results_dir)
    return results_dir


def test_the_live_reports_render_to_the_pinned_bytes(live_results: Path) -> None:
    rendered = render(load_results(live_results))
    assert rendered == PINNED.read_text()


def test_rendering_twice_from_the_same_directory_is_byte_identical(
    live_results: Path, tmp_path: Path
) -> None:
    first = write_report(live_results, tmp_path / "one.md")
    second = write_report(live_results, tmp_path / "two.md")
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() == PINNED.read_bytes()


def test_the_report_carries_no_timestamps_and_no_em_dashes(live_results: Path) -> None:
    text = render(load_results(live_results))
    assert "—" not in text
    # Window timestamps live in the manifests and the reports, not in this file.
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)


def test_the_scenario_table_shows_total_and_outcome_side_by_side(live_results: Path) -> None:
    text = render(load_results(live_results))
    header = next(line for line in text.splitlines() if line.startswith("| scenario |"))
    assert header == "| scenario | full total | full outcome | full top right |"
    # The live payments runs: 0.90, 0.63, 0.27, 0.25. Mean 0.51, and one of
    # the four is marked top_wrong (report-3, Jaccard 1/3).
    payments = next(line for line in text.splitlines() if line.startswith("| payments-"))
    assert payments == (
        "| payments-stripe-v251-uswest | 0.51 (0.25 to 0.90) | 0.60 (0.52 to 0.65) | 3 of 4 |"
    )
    control = next(line for line in text.splitlines() if line.startswith("| control-quiet"))
    assert control == "| control-quiet | 0.69 (-0.25 to 1.00) | 0.56 (0.00 to 0.75) | 3 of 4 |"
    everything = next(line for line in text.splitlines() if line.startswith("| all scenarios"))
    assert everything.startswith("| all scenarios | 0.60 (-0.25 to 1.00) |")


def test_the_contrast_column_counts_runs_that_pass_theirs_and_fail_ours(
    live_results: Path,
) -> None:
    """All eight live reports pass Honeycomb's evaluator. Three score under the line."""
    runs = load_results(live_results)
    assert all(item.honeycomb_process_passed for item in runs)
    under = [item for item in runs if item.total < OUTCOME_FAIL_BELOW]
    assert len(under) == 3
    text = render(runs)
    process = next(line for line in text.splitlines() if line.startswith("| full |"))
    assert process.endswith("| 3 of 8 |")
    assert f"total < {num(OUTCOME_FAIL_BELOW, 2)}" in text


def test_the_coerced_column_is_blank_when_no_run_needed_it_and_a_count_when_one_did(
    live_results: Path,
) -> None:
    """None of the eight live reports needed a coercion, so the column reads
    blank; adding one run that did makes it the total field count across the
    config, not the count of runs."""
    text = render(load_results(live_results))
    process = next(line for line in text.splitlines() if line.startswith("| full |"))
    assert process.split("|")[-3].strip() == ""

    report_path = FIXTURES / "run-ebc9c1e4be3d" / "report.json"
    report = load_report(report_path)
    result = grade_file(report_path, runs_dir=RUNS_DIR)
    coerced = graded_run(report, result, config="full", repeat=99)
    coerced.coerced_fields = ["hypotheses", "dims"]
    write_run(run_dir(live_results, "full", report.scenario_id, 99), coerced, report)

    text = render(load_results(live_results))
    process = next(line for line in text.splitlines() if line.startswith("| full |"))
    assert process.split("|")[-3].strip() == "2"


def test_the_coerced_column_counts_a_wrapper_and_a_tool_argument_coercion_as_two(
    live_results: Path,
) -> None:
    """EDW-1362: coerced_fields now also carries wrapper:<key> (a stray
    submit_report wrapper unwrapped) and <tool>:<path> (a BubbleUp group
    value the MCP client retyped from the column schema). Both kinds count
    toward the same total the column already sums."""
    report_path = FIXTURES / "run-ebc9c1e4be3d" / "report.json"
    report = load_report(report_path)
    result = grade_file(report_path, runs_dir=RUNS_DIR)
    coerced = graded_run(report, result, config="full", repeat=99)
    coerced.coerced_fields = ["wrapper:permalink", "run_bubbleup:selection.group.error"]
    write_run(run_dir(live_results, "full", report.scenario_id, 99), coerced, report)

    text = render(load_results(live_results))
    process = next(line for line in text.splitlines() if line.startswith("| full |"))
    assert process.split("|")[-3].strip() == "2"


def test_every_run_row_links_its_top_evidence_query(live_results: Path) -> None:
    text = render(load_results(live_results))
    rows = [line for line in text.splitlines() if line.startswith("| payments-") and "run-" in line]
    assert len(rows) == 4
    for row in rows:
        assert "[query](https://ui.honeycomb.io/team/environments/receipts-demo/" in row


def test_a_crash_is_a_zero_row_with_its_error_and_no_link(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    build_live_results(results_dir)
    crash = crashed_run(
        config="no-negation",
        scenario_id="control-quiet",
        run_id="run-ebc9c1e4be3d",
        repeat=1,
        error="RuntimeError: killed | mid-run",
        tool_calls=7,
        wall_s=12.3,
    )
    write_run(run_dir(results_dir, "no-negation", "control-quiet", 1), crash, None)

    text = render(load_results(results_dir))
    header = next(line for line in text.splitlines() if line.startswith("| scenario |"))
    assert header == (
        "| scenario | full total | full outcome | full top right | "
        "no-negation total | no-negation outcome | no-negation top right |"
    )
    control = next(line for line in text.splitlines() if line.startswith("| control-quiet |"))
    assert control.endswith("| 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0 of 1 |")
    # No no-negation runs for payments: empty cells, not zeros.
    payments = next(line for line in text.splitlines() if line.startswith("| payments-"))
    assert payments.endswith("| 3 of 4 |  |  |  |")
    row = next(
        line for line in text.splitlines() if line.startswith("| control-quiet | no-negation | 1 |")
    )
    assert "crash: RuntimeError: killed \\| mid-run" in row
    assert row.endswith("| 7 | 0.00 | 12 |  |  |")
    process_section = text.split("## Process by config", 1)[1]
    process = next(
        line for line in process_section.splitlines() if line.startswith("| no-negation |")
    )
    assert process.startswith("| no-negation | 1 | 1 | 7.0 |")
    assert process.endswith("| 0 of 1 |")


def test_an_unreadable_grade_is_listed_and_the_rest_still_render(tmp_path: Path) -> None:
    from evals.report import read_results

    results_dir = tmp_path / "results"
    build_live_results(results_dir)
    stale = run_dir(results_dir, "full", "control-quiet", 9)
    stale.mkdir(parents=True)
    (stale / "grade.json").write_text('{"config": "full", "total": 0.5}\n')
    runs, unreadable = read_results(results_dir)
    assert len(runs) == 8
    assert len(unreadable) == 1 and unreadable[0].startswith("full/control-quiet/9/grade.json: ")
    text = render(runs, unreadable)
    assert "## Unreadable" in text
    assert "- `full/control-quiet/9/grade.json: " in text
    # The table numbers are the eight live runs, untouched.
    assert "| control-quiet | 0.69 (-0.25 to 1.00) |" in text


def test_an_empty_results_directory_renders_a_report_that_says_so(tmp_path: Path) -> None:
    text = render(load_results(tmp_path))
    assert "No runs found." in text
    assert "|" not in text


def test_the_single_run_layout_is_not_mistaken_for_a_cell(tmp_path: Path) -> None:
    """`agent/__main__.py` writes `<run_id>/report.json` into the same directory."""
    results_dir = tmp_path / "results"
    build_live_results(results_dir)
    single = results_dir / "run-4155490e2a44"
    single.mkdir()
    (single / "report.json").write_text((FIXTURES / "run-4155490e2a44/report.json").read_text())
    assert len(load_results(results_dir)) == 8


def test_configs_are_ordered_full_first_then_the_ablations_then_the_rest() -> None:
    names = ["zeta", "no-notchecked", "full", "alpha", "no-negation"]
    assert sorted(names, key=config_order) == [
        "full",
        "no-negation",
        "no-notchecked",
        "alpha",
        "zeta",
    ]


def test_num_rounds_and_separates_thousands() -> None:
    assert num(0.2375, 2) == "0.24"
    assert num(-0.25, 2) == "-0.25"
    # 0.05 receipts against a 0.05 hedge penalty: a reachable -0.0 total.
    assert num(-0.0, 2) == "0.00"
    assert num(-0.004, 2) == "0.00"
    assert num(1149836.5, 0) == "1,149,836"
    assert num(1149836.5, 1) == "1,149,836.5"


def test_the_cli_writes_the_report(live_results: Path, tmp_path: Path, capsys: object) -> None:
    out = tmp_path / "report.md"
    assert main(["--results-dir", str(live_results), "--out", str(out)]) == 0
    assert out.read_bytes() == PINNED.read_bytes()


# --------------------------------------------------------------------------
# Ablation delta
# --------------------------------------------------------------------------


def test_ablation_section_appears_with_full_and_an_ablation_and_is_absent_with_full_alone() -> None:
    full_only = [_run("full", "payments-stripe-v251-uswest", 0.5, 0.4)]
    assert "## Ablation delta" not in render(full_only)

    with_ablation = full_only + [_run("no-negation", "payments-stripe-v251-uswest", 0.4, 0.4)]
    assert "## Ablation delta" in render(with_ablation)


def test_an_empty_results_directory_and_a_single_config_directory_carry_no_ablation_section(
    live_results: Path,
) -> None:
    assert "## Ablation delta" not in render(load_results(live_results))
    assert "## Ablation delta" not in render([])


def test_ablation_delta_uses_signed_three_decimal_formatting() -> None:
    runs = [_run("full", "s1", 0.500, 0.500), _run("no-negation", "s1", 0.461, 0.500)]
    text = render(runs)
    section = text.split("## Ablation delta", 1)[1].split("## Process by config", 1)[0]
    full_row = next(line for line in section.splitlines() if line.startswith("| full |"))
    ablation_row = next(line for line in section.splitlines() if line.startswith("| no-negation |"))
    assert full_row == "| full | 0.500 |  | 0.500 |  | 0 of 1 |  |"
    cells = [cell.strip() for cell in ablation_row.strip("|").split("|")]
    assert cells[1] == "0.461"  # mean total
    assert cells[2] == "-0.039"  # delta total, against full's 0.500
    assert cells[3] == "0.500"  # mean outcome
    assert cells[4] == "+0.000"  # delta outcome: both score 0.500


def test_ablation_sentence_inside_the_weight_reads_as_lost_checkability_not_correctness() -> None:
    runs = [_run("full", "s1", 0.60, 0.50), _run("no-negation", "s1", 0.50, 0.50)]
    text = render(runs)
    assert (
        "Removing the negation rule changed mean total by -0.100 and mean outcome by "
        "+0.000; 0 of 1 scenarios have fewer right top hypotheses than `full`. The total "
        "delta is inside the rule's own weight of 0.15, the part of the score that pays "
        "for checkability; the outcome delta is what the answers lost."
    ) in text


def test_ablation_sentence_for_a_non_negative_delta_says_it_does_not_reduce_the_score() -> None:
    runs = [_run("full", "s1", 0.50, 0.50), _run("no-notchecked", "s1", 0.55, 0.50)]
    text = render(runs)
    assert (
        "Removing the not-checked rule changed mean total by +0.050 and mean outcome by "
        "+0.000; 0 of 1 scenarios have fewer right top hypotheses than `full`. It does "
        "not reduce the score."
    ) in text


def test_ablation_sentence_beyond_the_weight_points_at_the_scenario_table() -> None:
    runs = [_run("full", "s1", 0.60, 0.50), _run("no-negation", "s1", 0.40, 0.50)]
    text = render(runs)
    assert (
        "Removing the negation rule changed mean total by -0.200 and mean outcome by "
        "+0.000; 0 of 1 scenarios have fewer right top hypotheses than `full`. The total "
        "delta is more than the rule's own weight of 0.15, so the loss is not "
        "checkability alone; read the scenario table for which answers changed."
    ) in text


def test_unknown_ablation_config_is_generic_and_never_reads_as_inside_the_weight() -> None:
    """An unknown config carries weight 0.0, so a negative delta can never be `<= 0.0`
    and the "inside the weight" branch is unreachable for it."""
    runs = [_run("full", "s1", 0.60, 0.50), _run("mystery", "s1", 0.59, 0.50)]
    text = render(runs)
    assert (
        "Config mystery changed mean total by -0.010 and mean outcome by +0.000; 0 of 1 "
        "scenarios have fewer right top hypotheses than `full`. The total delta is more "
        "than the rule's own weight of 0.00, so the loss is not checkability alone; read "
        "the scenario table for which answers changed."
    ) in text


# --------------------------------------------------------------------------
# Total spend
# --------------------------------------------------------------------------


def test_the_total_spend_line_equals_the_sum_of_the_cost_column(live_results: Path) -> None:
    runs = load_results(live_results)
    text = render(runs)
    total = sum(item.cost_usd for item in runs)
    assert (
        f"Total Anthropic spend across every run in this results directory: ${num(total, 2)}."
        in text
    )


def regenerate(path: Path = PINNED) -> Path:
    """Rebuild the pinned fixture from the live reports."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        results_dir = Path(tmp) / "results"
        build_live_results(results_dir)
        path.write_text(render(load_results(results_dir)))
    return path


if __name__ == "__main__":
    print(regenerate())
    sys.exit(0)
