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

from agent.handoff import Handoff
from agent.report import load_report
from evals.grader import Grade, grade_file
from evals.report import (
    OUTCOME_FAIL_BELOW,
    HandoffEntry,
    _signed,
    config_order,
    load_results,
    main,
    num,
    provider_order,
    read_handoffs,
    render,
    write_report,
)
from evals.run import GradedRun, crashed_run, graded_run, run_dir, write_handoff, write_run

FIXTURES = Path(__file__).parent / "fixtures" / "reports"
RUNS_DIR = FIXTURES / "runs"
PINNED = FIXTURES / "report.md"


def _run(
    config: str,
    scenario_id: str,
    total: float,
    outcome_score: float,
    repeat: int = 1,
    *,
    receipts_score: float = 0.0,
    dims: float = 0.0,
    provider: str = "fake",
    max_wall_s: float = 0.0,
    malformed_calls: int = 0,
) -> GradedRun:
    """A minimal `GradedRun` for exercising the renderer directly. `render` reads the
    flat fields and `top_right`, which needs a grade to be present and `dims` at or over
    the grader's line, so the grade is a bare `Grade.model_construct` carrying nothing
    the renderer reads. Enough to drive the ablation delta section without the grader or
    the disk layout."""
    return GradedRun(
        config=config,
        scenario_id=scenario_id,
        run_id=f"run-{config}-{provider}-{repeat}",
        repeat=repeat,
        provider=provider,
        model="m",
        total=total,
        outcome_score=outcome_score,
        receipts_score=receipts_score,
        dims=dims,
        top_wrong=dims < 0.5,
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
        max_wall_s=max_wall_s,
        malformed_calls=malformed_calls,
        honeycomb_process_score=None,
        honeycomb_process_passed=None,
        grade=Grade.model_construct(run_id=f"run-{config}-{repeat}", scenario_id=scenario_id),
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
    # `contrast` sits before the two process columns this test doesn't cover
    # (`wall cap s`, `malformed calls`), so it is no longer the row's last cell.
    assert process.split("|")[-4].strip() == "3 of 8"
    assert f"total < {num(OUTCOME_FAIL_BELOW, 2)}" in text


def test_the_coerced_column_is_blank_when_no_run_needed_it_and_a_count_when_one_did(
    live_results: Path,
) -> None:
    """None of the eight live reports needed a coercion, so the column reads
    blank; adding one run that did makes it the total field count across the
    config, not the count of runs."""
    text = render(load_results(live_results))
    process = next(line for line in text.splitlines() if line.startswith("| full |"))
    # `coerced` sits before `contrast`, `wall cap s`, and `malformed calls`.
    assert process.split("|")[-5].strip() == ""

    report_path = FIXTURES / "run-ebc9c1e4be3d" / "report.json"
    report = load_report(report_path)
    result = grade_file(report_path, runs_dir=RUNS_DIR)
    coerced = graded_run(report, result, config="full", repeat=99)
    coerced.coerced_fields = ["hypotheses", "dims"]
    write_run(run_dir(live_results, "full", report.scenario_id, 99), coerced, report)

    text = render(load_results(live_results))
    process = next(line for line in text.splitlines() if line.startswith("| full |"))
    assert process.split("|")[-5].strip() == "2"


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
    assert process.split("|")[-5].strip() == "2"


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
        # Same provider as the live fixtures: this test is about crash
        # formatting, not the provider column, and a bare "" provider would
        # otherwise read as a second provider and add the "(provider)" suffix.
        provider="anthropic",
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
    # `contrast` sits before `wall cap s` and `malformed calls`, both blank: this
    # crash carries no `max_wall_s` or `malformed_calls` of its own.
    assert process.split("|")[-4].strip() == "0 of 1"
    assert process.endswith("|  |  |")


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


def test_ablation_delta_decomposes_the_total_move_and_signs_every_delta() -> None:
    runs = [
        _run("full", "s1", 0.750, 0.500, receipts_score=0.250, dims=1.0),
        _run("no-negation", "s1", 0.461, 0.500, receipts_score=0.211, dims=1.0),
    ]
    text = render(runs)
    section = text.split("## Ablation delta", 1)[1].split("## Process by config", 1)[0]
    full_row = next(line for line in section.splitlines() if line.startswith("| full |"))
    ablation_row = next(line for line in section.splitlines() if line.startswith("| no-negation |"))
    assert full_row == "| full | 0.750 |  | 0.500 |  | 0.250 |  |  | 1 of 1 |  |"
    cells = [cell.strip() for cell in ablation_row.strip("|").split("|")]
    assert cells[1:8] == ["0.461", "-0.289", "0.500", "0.000", "0.211", "-0.039", "-0.250"]
    assert cells[8] == "1 of 1"
    assert cells[9] == "0 / 0 of 1"


def test_a_delta_that_rounds_to_nothing_carries_no_sign() -> None:
    assert _signed(-0.0004, 3) == "0.000"
    assert _signed(0.0004, 3) == "0.000"
    assert _signed(-0.0395, 3) == "-0.040"
    assert _signed(0.05, 3) == "+0.050"


def test_ablation_sentence_names_outcome_receipts_and_penalties_and_reads_outcome() -> None:
    """A total move inside the rule's weight says nothing about which part moved, so the
    sentence is built from the decomposition and the verdict comes from outcome."""
    runs = [
        _run("full", "s1", 0.750, 0.500, receipts_score=0.250, dims=1.0),
        _run("no-negation", "s1", 0.650, 0.400, receipts_score=0.250, dims=1.0),
    ]
    text = render(runs)
    assert (
        "Removing the negation rule moved mean total by -0.100: outcome -0.100, receipts "
        "0.000, penalties 0.000. 0 of 1 scenarios have fewer right top hypotheses than `full` "
        "and 0 have more. Outcome fell, so some answers changed; the scenario table says "
        "which. The receipts score did not fall, so the model kept doing what the removed "
        "rule asked without being asked."
    ) in text


def test_ablation_sentence_with_outcome_held_puts_the_loss_in_receipts() -> None:
    runs = [
        _run("full", "s1", 0.750, 0.500, receipts_score=0.250, dims=1.0),
        _run("no-notchecked", "s1", 0.650, 0.500, receipts_score=0.150, dims=1.0),
    ]
    text = render(runs)
    assert (
        "Removing the not-checked rule moved mean total by -0.100: outcome 0.000, receipts "
        "-0.100, penalties 0.000. 0 of 1 scenarios have fewer right top hypotheses than `full` "
        "and 0 have more. The answers held; the loss is in receipts and penalties."
    ) in text


def test_ablation_sentence_for_a_higher_total_still_reads_outcome_first() -> None:
    """A higher total with a lower outcome is not "does not reduce the score"."""
    runs = [
        _run("full", "s1", 0.800, 0.700, receipts_score=0.100, dims=1.0),
        _run("no-notchecked", "s1", 0.850, 0.550, receipts_score=0.300, dims=1.0),
    ]
    text = render(runs)
    assert "Outcome fell, so some answers changed" in text
    assert "It does not reduce the score." not in text
    up = [
        _run("full", "s1", 0.700, 0.500, receipts_score=0.200, dims=1.0),
        _run("no-notchecked", "s1", 0.750, 0.500, receipts_score=0.250, dims=1.0),
    ]
    assert "It does not reduce the score." in render(up)


def test_unknown_ablation_config_gets_a_generic_subject_and_no_rule_talk() -> None:
    runs = [
        _run("full", "s1", 0.600, 0.500, receipts_score=0.100, dims=1.0),
        _run("mystery", "s1", 0.590, 0.490, receipts_score=0.100, dims=1.0),
    ]
    text = render(runs)
    assert "Config mystery moved mean total by -0.010: outcome -0.010" in text
    assert "removed rule" not in text.split("## Ablation delta", 1)[1].split("## Process", 1)[0]


def test_ablation_counts_scenarios_with_fewer_and_more_right_top_hypotheses() -> None:
    runs = [
        _run("full", "s1", 1.0, 0.75, receipts_score=0.25, dims=1.0),
        _run("full", "s2", 0.0, 0.0, receipts_score=0.0, dims=0.0),
        _run("no-negation", "s1", 0.0, 0.0, receipts_score=0.0, dims=0.0),
        _run("no-negation", "s2", 1.0, 0.75, receipts_score=0.25, dims=1.0),
    ]
    text = render(runs)
    assert "| 1 / 1 of 2 |" in text
    assert "1 of 2 scenarios have fewer right top hypotheses than `full` and 1 have more" in text


def test_ablation_deltas_are_over_shared_scenarios_and_say_what_was_left_out() -> None:
    runs = [
        _run("full", "s1", 1.0, 0.75, receipts_score=0.25, dims=1.0),
        _run("no-negation", "s1", 1.0, 0.75, receipts_score=0.25, dims=1.0),
        _run("no-negation", "s2", 0.0, 0.0, receipts_score=0.0, dims=0.0),
    ]
    text = render(runs)
    section = text.split("## Ablation delta", 1)[1].split("## Process by config", 1)[0]
    row = next(line for line in section.splitlines() if line.startswith("| no-negation |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[1:3] == ["1.000", "0.000"]
    assert "`no-negation` is compared with `full` over 1 shared scenarios; left out: s2." in text


def test_ablation_section_names_scenarios_emitted_more_than_once() -> None:
    a = _run("full", "trig", 1.0, 0.75, receipts_score=0.25, dims=1.0)
    b = _run("no-negation", "trig", 1.0, 0.75, receipts_score=0.25, dims=1.0)
    b.run_id = "run-other"
    text = render([a, b])
    assert (
        "Emitted more than once, so its cells differ in data as well as in config: `trig`." in text
    )


# --------------------------------------------------------------------------
# Providers (R15, EDW-1337)
# --------------------------------------------------------------------------


def test_provider_order_puts_anthropic_first_then_the_rest_alphabetically() -> None:
    names = ["ollama", "bedrock", "anthropic"]
    assert sorted(names, key=provider_order) == ["anthropic", "bedrock", "ollama"]


def test_a_single_provider_directory_gets_no_provider_suffix() -> None:
    """Every run shares one provider, so the column key stays the config alone,
    the same as before providers existed."""
    runs = [
        _run("full", "s1", 0.90, 0.70, dims=1.0, provider="ollama"),
        _run("no-negation", "s1", 0.50, 0.40, dims=0.0, provider="ollama"),
    ]
    text = render(runs)
    header = next(line for line in text.splitlines() if line.startswith("| scenario |"))
    assert header == (
        "| scenario | full total | full outcome | full top right | "
        "no-negation total | no-negation outcome | no-negation top right |"
    )


def test_multi_provider_columns_are_keyed_by_config_and_provider_in_order() -> None:
    """Configs in `config_order`, and within a config `anthropic` first then the
    rest alphabetically. `full` here has both providers, `no-negation` only
    `anthropic`, so its column carries no `ollama` counterpart at all."""
    runs = [
        _run("full", "s1", 0.80, 0.60, dims=1.0, provider="ollama"),
        _run("full", "s1", 0.90, 0.70, dims=1.0, provider="anthropic"),
        _run("no-negation", "s1", 0.50, 0.40, dims=0.0, provider="anthropic"),
    ]
    text = render(runs)
    header = next(line for line in text.splitlines() if line.startswith("| scenario |"))
    assert header == (
        "| scenario | full (anthropic) total | full (anthropic) outcome | "
        "full (anthropic) top right | full (ollama) total | full (ollama) outcome | "
        "full (ollama) top right | no-negation (anthropic) total | "
        "no-negation (anthropic) outcome | no-negation (anthropic) top right |"
    )
    row = next(line for line in text.splitlines() if line.startswith("| s1 |"))
    assert row == (
        "| s1 | 0.90 (0.90 to 0.90) | 0.70 (0.70 to 0.70) | 1 of 1 | "
        "0.80 (0.80 to 0.80) | 0.60 (0.60 to 0.60) | 1 of 1 | "
        "0.50 (0.50 to 0.50) | 0.40 (0.40 to 0.40) | 0 of 1 |"
    )
    process_section = text.split("## Process by config", 1)[1].split("## Runs", 1)[0]
    labels = [
        line.split("|")[1].strip()
        for line in process_section.splitlines()
        if line.startswith("| full") or line.startswith("| no-negation")
    ]
    assert labels == ["full (anthropic)", "full (ollama)", "no-negation (anthropic)"]


def test_runs_table_gets_a_provider_column_only_when_more_than_one_provider_is_present() -> None:
    single = [_run("full", "s1", 0.5, 0.4, dims=1.0, provider="ollama")]
    text = render(single)
    runs_section = text.split("## Runs", 1)[1]
    header = next(line for line in runs_section.splitlines() if line.startswith("| scenario |"))
    assert "| run id | model |" in header
    row = next(line for line in runs_section.splitlines() if line.startswith("| s1 |"))
    assert "| ollama |" not in row

    multi = [
        _run("full", "s1", 0.5, 0.4, dims=1.0, provider="anthropic"),
        _run("full", "s1", 0.6, 0.4, dims=1.0, provider="ollama"),
    ]
    text = render(multi)
    runs_section = text.split("## Runs", 1)[1]
    header = next(line for line in runs_section.splitlines() if line.startswith("| scenario |"))
    assert "| run id | provider | model |" in header
    rows = [line for line in runs_section.splitlines() if line.startswith("| s1 |")]
    assert any("| ollama | m |" in row for row in rows)
    assert any("| anthropic | m |" in row for row in rows)


def test_wall_cap_and_malformed_calls_columns_render_and_are_blank_at_zero() -> None:
    runs = [
        _run("full", "s1", 0.5, 0.4, dims=1.0, max_wall_s=480.0, malformed_calls=2),
        _run("full", "s1", 0.6, 0.4, dims=1.0, repeat=2, max_wall_s=480.0, malformed_calls=0),
    ]
    text = render(runs)
    assert "`wall cap s`" in text
    assert "`malformed calls`" in text
    process_section = text.split("## Process by config", 1)[1].split("## Runs", 1)[0]
    process = next(line for line in process_section.splitlines() if line.startswith("| full |"))
    cells = [cell.strip() for cell in process.strip("|").split("|")]
    assert cells[-2] == "480"
    assert cells[-1] == "2"

    zero_runs = [_run("full", "s1", 0.5, 0.4, dims=1.0)]
    text = render(zero_runs)
    process_section = text.split("## Process by config", 1)[1].split("## Runs", 1)[0]
    process = next(line for line in process_section.splitlines() if line.startswith("| full |"))
    cells = [cell.strip() for cell in process.strip("|").split("|")]
    assert cells[-2] == ""
    assert cells[-1] == ""


def test_ablation_section_is_rendered_once_per_provider_that_has_full_and_an_ablation() -> None:
    runs = [
        _run("full", "s1", 0.80, 0.60, receipts_score=0.20, dims=1.0, provider="anthropic"),
        _run("no-negation", "s1", 0.70, 0.60, receipts_score=0.10, dims=1.0, provider="anthropic"),
        _run("full", "s1", 0.50, 0.40, receipts_score=0.10, dims=1.0, provider="ollama"),
        _run("no-negation", "s1", 0.40, 0.40, receipts_score=0.00, dims=1.0, provider="ollama"),
    ]
    text = render(runs)
    assert "## Ablation delta (anthropic)" in text
    assert "## Ablation delta (ollama)" in text
    assert text.index("## Ablation delta (anthropic)") < text.index("## Ablation delta (ollama)")
    anthropic_section = text.split("## Ablation delta (anthropic)", 1)[1].split(
        "## Ablation delta (ollama)", 1
    )[0]
    ollama_section = text.split("## Ablation delta (ollama)", 1)[1].split(
        "## Process by config", 1
    )[0]
    # Each section's `no-negation` row is the mean over that provider's own runs
    # only: 0.700 for anthropic, 0.400 for ollama, never mixed.
    assert "| no-negation | 0.700 |" in anthropic_section
    assert "| no-negation | 0.400 |" in ollama_section


def test_ablation_section_skips_a_provider_with_only_full() -> None:
    runs = [
        _run("full", "s1", 0.80, 0.60, receipts_score=0.20, dims=1.0, provider="anthropic"),
        _run("no-negation", "s1", 0.70, 0.60, receipts_score=0.10, dims=1.0, provider="anthropic"),
        _run("full", "s1", 0.50, 0.40, receipts_score=0.10, dims=1.0, provider="ollama"),
    ]
    text = render(runs)
    assert "## Ablation delta (anthropic)" in text
    assert "## Ablation delta (ollama)" not in text


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


# --------------------------------------------------------------------------
# Canvas handoffs (R12, EDW-1334)
# --------------------------------------------------------------------------


def _handoff(**overrides: object) -> Handoff:
    base: dict[str, object] = {
        "run_id": "run-abc123456789",
        "prompt": "Top hypothesis...",
        "status": "completed",
        "classification": "agree",
        "raw_text": "I agree with this.",
        "board_id": "brd-1",
        "board_url": "https://ui.honeycomb.io/team/boards/brd-1",
    }
    base.update(overrides)
    return Handoff(**base)


def test_no_handoff_data_renders_no_section_and_does_not_perturb_the_pinned_bytes(
    live_results: Path,
) -> None:
    assert read_handoffs(live_results) == []
    text = render(load_results(live_results))
    assert "## Canvas handoffs" not in text
    assert text == PINNED.read_text()


def test_the_handoff_section_counts_classifications_per_scenario_and_config() -> None:
    handoffs: list[HandoffEntry] = [
        ("full", "s1", _handoff(classification="agree")),
        ("full", "s1", _handoff(classification="agree")),
        ("full", "s1", _handoff(classification="disagree")),
        ("full", "s1", _handoff(classification="extend")),
        ("full", "s1", _handoff(classification="no_response", status="timeout", board_url=None)),
        ("no-negation", "s1", _handoff(classification="extend")),
    ]
    text = render([], handoffs=handoffs)
    section = text.split("## Canvas handoffs", 1)[1]
    header = next(line for line in section.splitlines() if line.startswith("| scenario |"))
    assert header == "| scenario | config | agree | disagree | extend | no response | board |"
    full_row = next(line for line in section.splitlines() if line.startswith("| s1 | full |"))
    assert full_row == (
        "| s1 | full | 2 | 1 | 1 | 1 | [board](https://ui.honeycomb.io/team/boards/brd-1) |"
    )
    ablation_row = next(
        line for line in section.splitlines() if line.startswith("| s1 | no-negation |")
    )
    assert ablation_row == (
        "| s1 | no-negation | 0 | 0 | 1 | 0 | [board](https://ui.honeycomb.io/team/boards/brd-1) |"
    )


def test_the_handoff_section_is_absent_when_no_run_carries_a_board_link() -> None:
    handoffs: list[HandoffEntry] = [("full", "s1", _handoff(board_id=None, board_url=None))]
    text = render([], handoffs=handoffs)
    row = next(line for line in text.splitlines() if line.startswith("| s1 |"))
    assert row.endswith("|  |")  # the board cell is blank, not a broken link


def test_handoff_rows_are_ordered_by_scenario_then_config_order() -> None:
    handoffs: list[HandoffEntry] = [
        ("no-negation", "s2", _handoff()),
        ("full", "s2", _handoff()),
        ("full", "s1", _handoff()),
    ]
    text = render([], handoffs=handoffs)
    section = text.split("## Canvas handoffs", 1)[1]
    rows = [
        line
        for line in section.splitlines()
        if line.startswith("| s") and not line.startswith("| scenario |")
    ]
    assert [line.split("|")[1:3] for line in rows] == [
        [" s1 ", " full "],
        [" s2 ", " full "],
        [" s2 ", " no-negation "],
    ]


def test_read_handoffs_reads_config_and_scenario_from_the_path(tmp_path: Path) -> None:
    handoff = _handoff()
    write_handoff(run_dir(tmp_path, "full", "s1", 1), handoff)
    write_handoff(run_dir(tmp_path, "no-negation", "s2", 3), handoff)

    entries = read_handoffs(tmp_path)

    assert sorted((config, scenario) for config, scenario, _ in entries) == [
        ("full", "s1"),
        ("no-negation", "s2"),
    ]


def test_write_report_renders_the_handoff_section_from_disk(
    live_results: Path, tmp_path: Path
) -> None:
    write_handoff(run_dir(live_results, "full", "control-quiet", 1), _handoff())
    path = write_report(live_results, tmp_path / "report.md")
    text = path.read_text()
    assert "## Canvas handoffs" in text
    assert "| control-quiet | full | 1 | 0 | 0 | 0 |" in text


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
