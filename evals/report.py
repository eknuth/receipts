"""Render `evals/report.md` from the `grade.json` files under `evals/results/`.

    uv run python -m evals.report

Every number in the report is read from a `grade.json` written by
`evals/run.py`. The cost column is `Report.cost_usd`, priced by the loop
from `evals/pricing.yml`, and comes through `Grade.process` unchanged.

The output is a pure function of the results directory. Files are read in
sorted order, no timestamps are written, and every number is rounded once
here by `num`, so rendering the same directory twice gives the same bytes.
A test renders the eight live fixture reports twice and compares the bytes.

Two columns need a word. `outcome` sits next to `total` in every table
because `total` folds the receipts components in, and an ablation that
removes a rule can lose that rule's weight without changing an answer.
Whether the rule changed the answer is a question about `outcome` alone. The contrast column
counts runs that pass Honeycomb's process evaluator while scoring under
`OUTCOME_FAIL_BELOW` on ours. The line is half the maximum and the number
the grader uses to call a top hypothesis wrong, and it was set before the
first render.

A `grade.json` the current schema cannot read is listed at the end of the
report by path instead of stopping the render, since repeats append across
schema changes and an old file next to a new one is the expected shape.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from statistics import fmean

from evals.run import CONFIGS, RESULTS_DIR, GradedRun, load_run

REPORT_PATH = Path(__file__).resolve().parent / "report.md"

# A run scoring under this on our grader "fails outcome" for the contrast
# column. Half the maximum, and the grader's own line for a wrong top
# hypothesis (`WRONG_BELOW` on the dims component).
OUTCOME_FAIL_BELOW = 0.5


def num(value: float, places: int) -> str:
    """The one rounding function. Thousands separated, fixed decimals.

    A value that rounds to zero prints as zero: `-0.0` is a reachable total
    (a small positive weight against an equal penalty) and would read as a
    negative score.
    """
    value = round(value, places) or 0.0
    return f"{value:,.{places}f}"


def _signed(value: float, places: int) -> str:
    """A delta with its sign, `+0.050` or `-0.039`; one that rounds to nothing is `0.000`."""
    rounded = round(value, places)
    if rounded == 0:
        return f"{0:.{places}f}"
    return f"{rounded:+,.{places}f}"


def read_results(results_dir: Path = RESULTS_DIR) -> tuple[list[GradedRun], list[str]]:
    """Every `grade.json` under `<config>/<scenario>/<n>/`, sorted, plus the unreadable ones.

    The second list holds one line per file the current schema rejects, as
    `relative path: reason`, sorted by path.
    """
    runs: list[GradedRun] = []
    unreadable: list[str] = []
    for path in sorted(results_dir.glob("*/*/*/grade.json")):
        try:
            runs.append(load_run(path))
        except (ValueError, OSError) as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            unreadable.append(f"{path.relative_to(results_dir)}: {reason}")
    runs.sort(key=lambda item: (item.scenario_id, config_order(item.config), item.repeat))
    return runs, unreadable


def load_results(results_dir: Path = RESULTS_DIR) -> list[GradedRun]:
    """The readable runs alone; see `read_results`."""
    return read_results(results_dir)[0]


def config_order(name: str) -> tuple[int, str]:
    """Known configs in `CONFIGS` order, `full` first; anything else after, alphabetically."""
    known = list(CONFIGS)
    return (known.index(name), name) if name in known else (len(known), name)


def render(runs: Sequence[GradedRun], unreadable: Sequence[str] = ()) -> str:
    """The whole report as Markdown."""
    lines: list[str] = [
        "# Eval report",
        "",
        "Every number here is read from a `grade.json` under `evals/results/`, written by "
        "`evals/run.py` and graded by `evals/grader.py` (weights and penalties are explained "
        "in `evals/grader.md`). Nothing is typed by hand. A run that crashed, or that the "
        "loop ended with an error, is a row with a total of 0 and the error text; it was not "
        "put through the grader.",
        "",
        "`total` is the grader's full score, penalties included, between -1 and 1. `outcome` "
        "is the weighted dims, span, incident, and onset components alone, at most 0.75, and "
        "sits next to `total` because an ablation that removes a rule can lose that rule's "
        "weight without changing an answer; whether it changed the answer is a question about "
        "`outcome`. `top right` counts runs whose top hypothesis scored at least 0.5 on the "
        "grader's dims component, which is the grader's own line for a wrong hypothesis. On "
        "a control a report with no hypothesis, or only low ones, scores 1 there and counts; "
        "on an incident scenario a report with no hypothesis scores 0 and does not; a crash "
        "never does. Ranges are the lowest and highest single run.",
        "",
    ]
    if not runs:
        lines += ["No runs found.", ""]
        lines += _unreadable_section(unreadable)
        return "\n".join(lines)

    configs = sorted({item.config for item in runs}, key=config_order)
    scenarios = sorted({item.scenario_id for item in runs})

    lines += ["## Scores by scenario", ""]
    header = ["scenario"]
    for config in configs:
        header += [f"{config} total", f"{config} outcome", f"{config} top right"]
    lines.append(_row(header))
    lines.append(_row(["---"] * len(header)))
    for scenario in scenarios + ["all scenarios"]:
        cells = [scenario]
        for config in configs:
            cell = [
                item
                for item in runs
                if item.config == config
                and (scenario == "all scenarios" or item.scenario_id == scenario)
            ]
            cells += _score_cells(cell)
        lines.append(_row(cells))
    lines.append("")

    lines += _ablation_section(runs, configs)

    lines += [
        "## Process by config",
        "",
        "Means over every run in the config, crashes included. `passes theirs, fails ours` "
        "counts runs that pass Honeycomb's process evaluator (a reimplementation of "
        f"`tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, pass at 0.6) and score "
        f"a `total` under {num(OUTCOME_FAIL_BELOW, 2)} on ours. A crash has no process "
        "score and is not counted as passing theirs. The line is on `total`, so under an "
        "ablation config the removed rule's weight (0.15 for the negation, 0.10 for the "
        "not-checked list, both scored whatever the config) can count against the run here; "
        "read `outcome` in the "
        "scenario table for whether the answer changed. `tokens in` is uncached input, as the "
        "grade records it; the prompt cache reads that make up most of what the model read "
        "are in each `report.json` and are already priced into the cost. `coerced` counts two "
        "kinds of fix, across every attempt and every tool call in the config: submit_report "
        "fields decoded from a JSON-encoded string or unwrapped from a stray wrapper key "
        "around the whole report, and BubbleUp group values the MCP client retyped from the "
        "column schema rather than sending on as the model wrote them; blank when none were. "
        "`total cost USD` sums the same cost column instead of averaging it, and the line "
        "under the table sums that column again across every config.",
        "",
    ]
    header = [
        "config",
        "runs",
        "crashed",
        "mean calls",
        "mean tokens in",
        "mean tokens out",
        "mean cost USD",
        "total cost USD",
        "mean wall s",
        "coerced",
        f"passes theirs, fails ours (total < {num(OUTCOME_FAIL_BELOW, 2)})",
    ]
    lines.append(_row(header))
    lines.append(_row(["---"] * len(header)))
    total_spend = 0.0
    for config in configs:
        cell = [item for item in runs if item.config == config]
        contrast = sum(
            1 for item in cell if item.honeycomb_process_passed and item.total < OUTCOME_FAIL_BELOW
        )
        coerced = sum(len(item.coerced_fields) for item in cell)
        config_spend = sum(item.cost_usd for item in cell)
        total_spend += config_spend
        lines.append(
            _row(
                [
                    config,
                    str(len(cell)),
                    str(sum(1 for item in cell if item.crashed)),
                    _mean(cell, lambda item: item.tool_calls, 1),
                    _mean(cell, lambda item: item.tokens_in, 0),
                    _mean(cell, lambda item: item.tokens_out, 0),
                    _mean(cell, lambda item: item.cost_usd, 2),
                    num(config_spend, 2),
                    _mean(cell, lambda item: item.wall_s, 0),
                    str(coerced) if coerced else "",
                    f"{contrast} of {len(cell)}",
                ]
            )
        )
    lines.append("")
    lines.append(
        f"Total Anthropic spend across every run in this results directory: ${num(total_spend, 2)}."
    )
    lines.append("")

    lines += [
        "## Runs",
        "",
        "One row per investigation. `query` links to the first evidence query of the top "
        "hypothesis, or to the first baseline query when the report names no hypothesis. "
        "`theirs` is Honeycomb's process score with its pass mark applied.",
        "",
    ]
    header = [
        "scenario",
        "config",
        "n",
        "run id",
        "model",
        "total",
        "outcome",
        "receipts",
        "top confidence",
        "stopped by",
        "calls",
        "cost USD",
        "wall s",
        "theirs",
        "query",
    ]
    lines.append(_row(header))
    lines.append(_row(["---"] * len(header)))
    for item in runs:
        if item.honeycomb_process_score is None:
            theirs = ""
        else:
            verdict = "pass" if item.honeycomb_process_passed else "fail"
            theirs = f"{num(item.honeycomb_process_score, 2)} {verdict}"
        stopped = item.stop_reason
        if item.error:
            stopped = f"{stopped}: {_escape(item.error)}"
        elif item.validation_failed:
            stopped = f"{stopped} (validation failed)"
        lines.append(
            _row(
                [
                    item.scenario_id,
                    item.config,
                    str(item.repeat),
                    item.run_id,
                    item.model,
                    num(item.total, 2),
                    num(item.outcome_score, 2),
                    num(item.receipts_score, 2),
                    item.top_confidence or "",
                    stopped,
                    str(item.tool_calls),
                    num(item.cost_usd, 2),
                    num(item.wall_s, 0),
                    theirs,
                    f"[query]({item.permalink})" if item.permalink else "",
                ]
            )
        )
    lines.append("")
    lines += _unreadable_section(unreadable)
    return "\n".join(lines)


def _unreadable_section(unreadable: Sequence[str]) -> list[str]:
    if not unreadable:
        return []
    lines = [
        "## Unreadable",
        "",
        "These `grade.json` files did not match the current schema and are left out above.",
        "",
    ]
    lines += [f"- `{_escape(entry)}`" for entry in sorted(unreadable)]
    lines.append("")
    return lines


def _score_cells(cell: Sequence[GradedRun]) -> list[str]:
    """`total`, `outcome`, and `top right` for one group of runs, or three empty cells."""
    if not cell:
        return ["", "", ""]
    totals = [item.total for item in cell]
    outcomes = [item.outcome_score for item in cell]
    right = sum(1 for item in cell if item.top_right)
    return [
        f"{num(fmean(totals), 2)} ({num(min(totals), 2)} to {num(max(totals), 2)})",
        f"{num(fmean(outcomes), 2)} ({num(min(outcomes), 2)} to {num(max(outcomes), 2)})",
        f"{right} of {len(cell)}",
    ]


def _mean(cell: Sequence[GradedRun], pick: Callable[[GradedRun], float], places: int) -> str:
    return num(fmean(pick(item) for item in cell), places) if cell else ""


def _ablation_sentence(
    config: str,
    *,
    delta_total: float,
    delta_outcome: float,
    delta_receipts: float,
    delta_penalties: float,
    fewer: int,
    more: int,
    scenarios: int,
) -> str:
    """One generated sentence per ablation config, built from the identity
    `total = outcome + receipts + penalties`, so each part of the move is
    named and nothing is inferred from the total alone."""
    if config == "no-negation":
        subject = "Removing the negation rule"
    elif config == "no-notchecked":
        subject = "Removing the not-checked rule"
    else:
        subject = f"Config {config}"
    head = (
        f"{subject} moved mean total by {_signed(delta_total, 3)}: outcome "
        f"{_signed(delta_outcome, 3)}, receipts {_signed(delta_receipts, 3)}, penalties "
        f"{_signed(delta_penalties, 3)}. {fewer} of {scenarios} scenarios have fewer right top "
        f"hypotheses than `full` and {more} have more."
    )
    parts: list[str] = []
    if delta_outcome < 0:
        parts.append("Outcome fell, so some answers changed; the scenario table says which.")
    elif delta_total < 0:
        parts.append("The answers held; the loss is in receipts and penalties.")
    else:
        parts.append("It does not reduce the score.")
    if config in ("no-negation", "no-notchecked") and delta_receipts >= 0:
        parts.append(
            "The receipts score did not fall, so the model kept doing what the removed rule "
            "asked without being asked."
        )
    return head + " " + " ".join(parts)


def _ablation_section(runs: Sequence[GradedRun], configs: Sequence[str]) -> list[str]:
    """The `## Ablation delta` section, or nothing when there is no ablation to show.

    `configs` is already sorted `full` first (`config_order`). The section
    is rendered only when `full` is among the configs and at least one other
    config is too; an empty results directory and a single-config directory
    both render as they did before this section existed. Each config's means
    and deltas are taken over the scenarios it shares with `full`, so a
    scenario present in one config only cannot move a delta; the paragraph
    names any such scenario, and any scenario whose cells carry more than one
    run id, since those cells differ in data as well as in config.
    """
    if "full" not in configs or len(configs) < 2:
        return []

    def cell(config: str, scenarios: set[str] | None = None) -> list[GradedRun]:
        return [
            item
            for item in runs
            if item.config == config and (scenarios is None or item.scenario_id in scenarios)
        ]

    def scenarios_of(config: str) -> set[str]:
        return {item.scenario_id for item in runs if item.config == config}

    def top_right_by_scenario(config: str, scenarios: set[str]) -> dict[str, int]:
        return {
            scenario: sum(
                1
                for item in runs
                if item.config == config and item.scenario_id == scenario and item.top_right
            )
            for scenario in scenarios
        }

    def means(group: Sequence[GradedRun]) -> tuple[float, float, float, float]:
        total = fmean(item.total for item in group)
        outcome = fmean(item.outcome_score for item in group)
        receipts = fmean(item.receipts_score for item in group)
        return total, outcome, receipts, total - outcome - receipts

    full_scenarios = scenarios_of("full")
    lines = [
        "## Ablation delta",
        "",
        "Each row is one config's mean `total`, `outcome`, and `receipts` over the scenarios "
        "it shares with `full`, the difference from `full` on each, and the difference in "
        "penalties, which is what is left of the total move once outcome and receipts are "
        "taken out. `top right` counts runs whose top hypothesis the grader called right; the "
        "last column counts scenarios where the config has fewer such runs than `full`, and "
        "scenarios where it has more. The `full` row has no deltas, because it is what the "
        "others are measured against.",
        "",
    ]
    header = [
        "config",
        "mean total",
        "delta total",
        "mean outcome",
        "delta outcome",
        "mean receipts",
        "delta receipts",
        "delta penalties",
        "top right",
        "scenarios fewer / more top right than full",
    ]
    lines.append(_row(header))
    lines.append(_row(["---"] * len(header)))

    sentences: list[str] = []
    notes: list[str] = []
    for config in configs:
        if config == "full":
            group = cell("full")
            total, outcome, receipts, _ = means(group)
            lines.append(
                _row(
                    [
                        config,
                        num(total, 3),
                        "",
                        num(outcome, 3),
                        "",
                        num(receipts, 3),
                        "",
                        "",
                        f"{sum(1 for item in group if item.top_right)} of {len(group)}",
                        "",
                    ]
                )
            )
            continue
        shared = scenarios_of(config) & full_scenarios
        only_here = sorted(scenarios_of(config) - full_scenarios)
        only_full = sorted(full_scenarios - scenarios_of(config))
        if only_here or only_full:
            notes.append(
                f"`{config}` is compared with `full` over {len(shared)} shared scenarios; "
                f"left out: {', '.join(only_here + only_full) or 'none'}."
            )
        group = cell(config, shared)
        full_group = cell("full", shared)
        if not shared:
            lines.append(_row([config, "", "", "", "", "", "", "", "0 of 0", ""]))
            continue
        total, outcome, receipts, penalties = means(group)
        f_total, f_outcome, f_receipts, f_penalties = means(full_group)
        config_top = top_right_by_scenario(config, shared)
        full_top = top_right_by_scenario("full", shared)
        fewer = sum(1 for s in shared if config_top[s] < full_top[s])
        more = sum(1 for s in shared if config_top[s] > full_top[s])
        lines.append(
            _row(
                [
                    config,
                    num(total, 3),
                    _signed(total - f_total, 3),
                    num(outcome, 3),
                    _signed(outcome - f_outcome, 3),
                    num(receipts, 3),
                    _signed(receipts - f_receipts, 3),
                    _signed(penalties - f_penalties, 3),
                    f"{sum(1 for item in group if item.top_right)} of {len(group)}",
                    f"{fewer} / {more} of {len(shared)}",
                ]
            )
        )
        sentences.append(
            _ablation_sentence(
                config,
                delta_total=total - f_total,
                delta_outcome=outcome - f_outcome,
                delta_receipts=receipts - f_receipts,
                delta_penalties=penalties - f_penalties,
                fewer=fewer,
                more=more,
                scenarios=len(shared),
            )
        )
    lines.append("")

    per_cell = sorted(
        scenario
        for scenario in {item.scenario_id for item in runs}
        if len({item.run_id for item in runs if item.scenario_id == scenario}) > 1
    )
    if per_cell:
        notes.append(
            "Emitted more than once, so its cells differ in data as well as in config: "
            + ", ".join(f"`{scenario}`" for scenario in per_cell)
            + "."
        )
    for note in notes:
        lines.append(note)
        lines.append("")
    for sentence in sentences:
        lines.append(sentence)
        lines.append("")
    return lines


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _escape(text: str) -> str:
    """Keep an error message on one table row and out of the column separators."""
    return " ".join(text.split()).replace("|", "\\|")


def write_report(results_dir: Path = RESULTS_DIR, path: Path = REPORT_PATH) -> Path:
    runs, unreadable = read_results(results_dir)
    path.write_text(render(runs, unreadable))
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render evals/report.md from evals/results/.")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    path = write_report(args.results_dir, args.out)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
