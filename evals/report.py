"""Render `evals/report.md` from the `grade.json` files under `evals/results/`.

    uv run python -m evals.report

Every number in the report is read from a `grade.json` written by
`evals/run.py`; nothing is typed by hand and nothing is recomputed. The cost
column is `Report.cost_usd`, priced by the loop from `evals/pricing.yml`, and
comes through `Grade.process` unchanged.

The output is a pure function of the results directory. Files are read in
sorted order, no timestamps are written, and every number is rounded once
here by `num`, so rendering the same directory twice gives the same bytes.
That is a test, not a claim.

Two columns need a word. `outcome` sits next to `total` in every table
because `total` folds the receipts components in, and an ablation that
removes a rule loses that rule's weight by construction. Whether the rule
changed the answer is a question about `outcome` alone. And the contrast
column counts runs that pass Honeycomb's process evaluator while scoring
under `OUTCOME_FAIL_BELOW` on ours; the line is half the maximum and the same
number the grader uses to call a top hypothesis wrong, and it was fixed
before anything was rendered rather than chosen to make the column look good.
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
    """The one rounding function. Thousands separated, fixed decimals."""
    return f"{value:,.{places}f}"


def load_results(results_dir: Path = RESULTS_DIR) -> list[GradedRun]:
    """Every `grade.json` under `<config>/<scenario>/<n>/`, sorted."""
    runs = [load_run(path) for path in sorted(results_dir.glob("*/*/*/grade.json"))]
    runs.sort(key=lambda item: (item.scenario_id, config_order(item.config), item.repeat))
    return runs


def config_order(name: str) -> tuple[int, str]:
    """Known configs in `CONFIGS` order, `full` first; anything else after, alphabetically."""
    known = list(CONFIGS)
    return (known.index(name), name) if name in known else (len(known), name)


def render(runs: Sequence[GradedRun]) -> str:
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
        "sits next to `total` because an ablation that removes a rule loses that rule's "
        "weight by construction; whether it changed the answer is a question about "
        "`outcome`. `top right` counts runs the grader did not mark `top_wrong`, out of the "
        "runs in the cell; a crash counts as wrong, and a report with no hypothesis is not "
        "marked wrong, so on an incident scenario it counts here and pays in `total` "
        "instead. Ranges are the lowest and highest single run.",
        "",
    ]
    if not runs:
        lines += ["No runs found.", ""]
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

    lines += [
        "## Process by config",
        "",
        "Means over every run in the config, crashes included. `passes theirs, fails ours` "
        "counts runs that pass Honeycomb's process evaluator (a reimplementation of "
        f"`tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, pass at 0.6) and score "
        f"a `total` under {num(OUTCOME_FAIL_BELOW, 2)} on ours. A crash has no process "
        "score and is not counted as passing theirs. `tokens in` is uncached input, as the "
        "grade records it; the prompt cache reads that make up most of what the model read "
        "are in each `report.json` and are already priced into the cost.",
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
        "mean wall s",
        f"passes theirs, fails ours (total < {num(OUTCOME_FAIL_BELOW, 2)})",
    ]
    lines.append(_row(header))
    lines.append(_row(["---"] * len(header)))
    for config in configs:
        cell = [item for item in runs if item.config == config]
        contrast = sum(
            1 for item in cell if item.honeycomb_process_passed and item.total < OUTCOME_FAIL_BELOW
        )
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
                    _mean(cell, lambda item: item.wall_s, 0),
                    f"{contrast} of {len(cell)}",
                ]
            )
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
    return "\n".join(lines)


def _score_cells(cell: Sequence[GradedRun]) -> list[str]:
    """`total`, `outcome`, and `top right` for one group of runs, or three empty cells."""
    if not cell:
        return ["", "", ""]
    totals = [item.total for item in cell]
    outcomes = [item.outcome_score for item in cell]
    right = sum(1 for item in cell if not item.top_wrong)
    return [
        f"{num(fmean(totals), 2)} ({num(min(totals), 2)} to {num(max(totals), 2)})",
        f"{num(fmean(outcomes), 2)} ({num(min(outcomes), 2)} to {num(max(outcomes), 2)})",
        f"{right} of {len(cell)}",
    ]


def _mean(cell: Sequence[GradedRun], pick: Callable[[GradedRun], float], places: int) -> str:
    return num(fmean(pick(item) for item in cell), places) if cell else ""


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _escape(text: str) -> str:
    """Keep an error message on one table row and out of the column separators."""
    return " ".join(text.split()).replace("|", "\\|")


def write_report(results_dir: Path = RESULTS_DIR, path: Path = REPORT_PATH) -> Path:
    path.write_text(render(load_results(results_dir)))
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
