"""Paired-cell comparison of two result columns.

    uv run python -m evals.compare r18-pass/full full

Both names are paths under `evals/results/`: a live column is one level
(`full`), a parked one is two (`r18-pass/full`). A cell is one
`<scenario>/<n>/grade.json`, keyed by scenario id and repeat. The paired mean
is over the cells present in both columns, which is the number to read when
the columns differ in size: a headline mean over an unequal set moves for
reasons that have nothing to do with the change under test.

Ported from the session scratchpad script that was rewritten by hand for
every before-and-after pass since EDW-1364. Every number is read from a
`grade.json` or `report.json`, nothing is typed in.
"""

from __future__ import annotations

import argparse
import re
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agent.report import SCHEMA_REJECTION, Report, load_report
from evals.report import RESULTS_DIR
from evals.run import GradedRun

CellKey = tuple[str, int]


@dataclass(frozen=True)
class Cell:
    grade: GradedRun
    report: Report | None


def load_column(results_dir: Path, column: str) -> dict[CellKey, Cell]:
    """Every graded cell under `results_dir/column`, keyed by (scenario_id, repeat)."""
    root = results_dir / column
    cells: dict[CellKey, Cell] = {}
    for path in sorted(root.glob("*/*/grade.json")):
        grade = GradedRun.model_validate_json(path.read_text())
        report_path = path.parent / "report.json"
        report = load_report(report_path) if report_path.exists() else None
        cells[(grade.scenario_id, grade.repeat)] = Cell(grade, report)
    return cells


def _mean(values: Sequence[float]) -> float:
    return statistics.mean(values) if values else 0.0


def summary_line(cells: dict[CellKey, Cell]) -> str:
    grades = [c.grade for c in cells.values()]
    if not grades:
        return "n=0"
    return (
        f"n={len(grades)} total={_mean([g.total for g in grades]):.3f} "
        f"outcome={_mean([g.outcome_score for g in grades]):.3f} "
        f"receipts={_mean([g.receipts_score for g in grades]):.3f} "
        f"top_right={sum(g.top_right for g in grades)} "
        f"val_failed={sum(g.validation_failed for g in grades)} "
        f"crashed={sum(g.crashed for g in grades)} "
        f"calls={_mean([g.tool_calls for g in grades]):.1f} "
        f"cost=${sum(g.cost_usd for g in grades):.2f}"
    )


def paired_keys(before: dict[CellKey, Cell], after: dict[CellKey, Cell]) -> list[CellKey]:
    return sorted(set(before) & set(after))


def scenario_rows(before: dict[CellKey, Cell], after: dict[CellKey, Cell]) -> list[list[str]]:
    """One row per scenario: mean total and cell count on each side, and the delta."""
    by: dict[str, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for (scenario, _), cell in before.items():
        by[scenario][0].append(cell.grade.total)
    for (scenario, _), cell in after.items():
        by[scenario][1].append(cell.grade.total)
    rows = []
    for scenario in sorted(by):
        b, a = by[scenario]
        fb = f"{_mean(b):.3f} ({len(b)})" if b else "-"
        fa = f"{_mean(a):.3f} ({len(a)})" if a else "-"
        delta = f"{_mean(a) - _mean(b):+.3f}" if a and b else ""
        rows.append([scenario, fb, fa, delta])
    return rows


def moved_cells(
    before: dict[CellKey, Cell], after: dict[CellKey, Cell], *, at_least: float = 0.05
) -> list[tuple[CellKey, float, float]]:
    """Paired cells whose total moved by `at_least`, largest move first."""
    out = []
    for key in paired_keys(before, after):
        b, a = before[key].grade.total, after[key].grade.total
        if abs(a - b) >= at_least:
            out.append((key, b, a))
    return sorted(out, key=lambda item: -abs(item[2] - item[1]))


_ISSUE_CODE = re.compile(r"^ {0,2}([a-z][a-z0-9_]*): ", re.MULTILINE)
"""`agent.validate.Issue.__str__` renders one issue as `code: message` on its
own line, indented two spaces inside `rejection_message`'s wrapping prose; a
message with no such line (or a schema rejection, which has none) has no code
to count under."""


def _codes_in_message(message: str) -> list[str]:
    """The rule codes named in one rejection message, one per issue line."""
    if message.startswith(SCHEMA_REJECTION):
        return ["schema"]
    return _ISSUE_CODE.findall(message)


def validation_classes(
    cells: dict[CellKey, Cell], *, field: str = "validation_messages"
) -> dict[str, int]:
    """Rule codes named in a column's `field` (`validation_messages` or `rejections`).

    `validation_messages` only ever holds a run's *last* rejection, the one
    that was kept and flagged; `rejections` holds every attempt, fixed or
    not (`None` on a run recorded before EDW-1369, skipped rather than
    counted as zero).
    """
    counts: dict[str, int] = defaultdict(int)
    for cell in cells.values():
        if cell.report is None:
            continue
        messages = getattr(cell.report, field)
        if messages is None:
            continue
        for message in messages:
            for code in _codes_in_message(message):
                counts[code] += 1
    return dict(sorted(counts.items()))


def control_rows(cells: dict[CellKey, Cell]) -> list[list[str]]:
    """The control cells: what each filed, since restraint is the whole test there."""
    rows = []
    for (scenario, repeat), cell in sorted(cells.items()):
        if not scenario.startswith("control"):
            continue
        report = cell.report
        filed = "no report" if report is None else str(report.incident_present)
        hyps = 0 if report is None else len(report.hypotheses)
        top = report.hypotheses[0].confidence if report and report.hypotheses else ""
        rows.append(
            [
                f"{scenario}/{repeat}",
                filed,
                str(hyps),
                top,
                f"{cell.grade.total:.3f}",
                cell.grade.stop_reason,
            ]
        )
    return rows


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def render(
    before_name: str,
    after_name: str,
    before: dict[CellKey, Cell],
    after: dict[CellKey, Cell],
) -> str:
    out = [f"# {before_name} vs {after_name}", ""]
    out.append(f"before ({before_name}): {summary_line(before)}")
    out.append(f"after  ({after_name}): {summary_line(after)}")
    keys = paired_keys(before, after)
    if keys:
        pb = _mean([before[k].grade.total for k in keys])
        pa = _mean([after[k].grade.total for k in keys])
        ob = _mean([before[k].grade.outcome_score for k in keys])
        oa = _mean([after[k].grade.outcome_score for k in keys])
        out.append(
            f"paired n={len(keys)} total {pb:.3f} to {pa:.3f} ({pa - pb:+.3f}), "
            f"outcome {ob:.3f} to {oa:.3f} ({oa - ob:+.3f})"
        )
    else:
        out.append("paired n=0: the columns share no (scenario, repeat) cell")
    out.append("")
    out.append("## By scenario")
    out.append("")
    out += _table(
        ["scenario", "before (cells)", "after (cells)", "delta"], scenario_rows(before, after)
    )
    moved = moved_cells(before, after)
    out.append("")
    out.append("## Paired cells that moved by 0.05 or more")
    out.append("")
    if moved:
        rows = [[f"{s}/{n}", f"{b:.3f}", f"{a:.3f}", f"{a - b:+.3f}"] for (s, n), b, a in moved]
        out += _table(["cell", "before", "after", "delta"], rows)
    else:
        out.append("none")
    out.append("")
    out.append("## Controls in the after column")
    out.append("")
    controls = control_rows(after)
    if controls:
        out += _table(
            ["cell", "incident_present", "hypotheses", "top confidence", "total", "stopped by"],
            controls,
        )
    else:
        out.append("no control cells")
    out.append("")
    out.append("## Validation messages by code")
    out.append("")
    vb, va = validation_classes(before), validation_classes(after)
    codes = sorted(set(vb) | set(va))
    if codes:
        out += _table(
            ["code", "before", "after"], [[c, str(vb.get(c, 0)), str(va.get(c, 0))] for c in codes]
        )
    else:
        out.append("none in either column")
    out.append("")
    out.append("## Every rejection by code (fixed or not, EDW-1369)")
    out.append("")
    rb = validation_classes(before, field="rejections")
    ra = validation_classes(after, field="rejections")
    rcodes = sorted(set(rb) | set(ra))
    if rcodes:
        out += _table(
            ["code", "before", "after"],
            [[c, str(rb.get(c, 0)), str(ra.get(c, 0))] for c in rcodes],
        )
    else:
        out.append("none recorded in either column")
    return "\n".join(out) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two result columns cell by cell.")
    parser.add_argument(
        "before", help="column path under evals/results/, for example r18-pass/full"
    )
    parser.add_argument("after", help="column path under evals/results/, for example full")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)
    for name in (args.before, args.after):
        if not (args.results_dir / name).is_dir():
            parser.error(f"no column at {args.results_dir / name}")
    before = load_column(args.results_dir, args.before)
    after = load_column(args.results_dir, args.after)
    print(render(args.before, args.after, before, after), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
