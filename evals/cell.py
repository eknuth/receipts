"""Read one result cell: why it scored what it scored.

    uv run python -m evals.cell full payments-stripe-v251-uswest 2

Prints the grade components and penalties, the grader's notes, the validation
messages, the hypotheses with their evidence and negation summaries, the
baseline evidence, the rejected candidates, the partially checked and not
checked lists, and then the tool log with every `run_query` on one line:
calculations, filters (the run id filter dropped, it is on every query),
breakdowns, granularity, and time range. Other calls get their name and a
compact view of their arguments.

Ported from the `dump.py` scripts that were rewritten in four session
scratchpads. Reading a cell this way is the step that precedes every method
change: the loss has to be traced to a query the model ran or did not run.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent.report import Report, ToolCall, load_report
from evals.report import RESULTS_DIR
from evals.run import GradedRun

QUERY_TOOLS = ("run_query", "list_spans")
DROPPED_ARGS = {"environment_slug", "dataset_slug"}


def effective_spec(args: Mapping[str, Any]) -> Mapping[str, Any]:
    """`args["query_spec"]` as a mapping, parsing it when the model sent a JSON string."""
    spec = args.get("query_spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            return args
    return spec if isinstance(spec, dict) else args


def _filter_text(clause: Mapping[str, Any]) -> str:
    value = clause.get("value")
    text = f"{clause.get('column')} {clause.get('op')}"
    return text if value is None else f"{text} {json.dumps(value)}"


def _calc_text(calc: Mapping[str, Any]) -> str:
    column = calc.get("column")
    text = f"{calc.get('op')}({column})" if column else str(calc.get("op"))
    nested = [c for c in calc.get("filters") or [] if isinstance(c, Mapping)]
    if nested:
        text += " WHERE[" + "; ".join(_filter_text(c) for c in nested) + "]"
    return text


def query_line(call: ToolCall, *, run_id: str | None) -> str:
    """One line for a `run_query`: id, error flag, calcs, filters, breakdowns, granularity,
    window."""
    spec = effective_spec(call.args)
    calcs = ", ".join(
        _calc_text(c) for c in spec.get("calculations") or [] if isinstance(c, Mapping)
    )
    filters = "; ".join(
        _filter_text(c)
        for c in spec.get("filters") or []
        if isinstance(c, Mapping) and not (run_id and c.get("column") == "scenario.run_id")
    )
    breakdowns = ", ".join(str(b) for b in spec.get("breakdowns") or [])
    window = f"{spec.get('from') or spec.get('time_range') or '?'} -> {spec.get('to') or '?'}"
    flag = " ERROR" if call.is_error else ""
    head = f"[{call.query_id or '-'}] {call.name}{flag}"
    return (
        f"{head} CALC {calcs or '-'} | WHERE {filters or '-'} | "
        f"BY {breakdowns or '-'} | GRAN {spec.get('granularity')} | {window}"
    )


def call_line(call: ToolCall) -> str:
    """One line for any other call: name, id, error flag, compact arguments."""
    args = {k: v for k, v in call.args.items() if k not in DROPPED_ARGS}
    flag = " ERROR" if call.is_error else ""
    text = json.dumps(args, sort_keys=True)
    if len(text) > 200:
        text = text[:197] + "..."
    return f"[{call.query_id or '-'}] {call.name}{flag} {text}"


def render(grade: GradedRun, report: Report | None) -> str:
    out: list[str] = []
    out.append(
        f"# {grade.config}/{grade.scenario_id}/{grade.repeat} run={grade.run_id} "
        f"model={grade.model} stop={grade.stop_reason}"
    )
    out.append(
        f"total={grade.total:.3f} outcome={grade.outcome_score:.3f} "
        f"receipts={grade.receipts_score:.3f} calls={grade.tool_calls} "
        f"wall={grade.wall_s:.0f}s cost=${grade.cost_usd:.2f}"
    )
    if grade.error:
        out.append(f"error: {grade.error}")
    if grade.grade is not None:
        g = grade.grade
        out.append("")
        out.append("## Grade")
        out.append("")
        for name, raw in g.components.model_dump().items():
            weighted = getattr(g.weighted, name)
            out.append(f"- {name}: {raw:.3f} (weighted {weighted:.3f})")
        pen = g.penalties
        out.append(
            f"- penalties: calibration {pen.calibration:+.2f}, "
            f"zero_evidence {pen.zero_evidence:+.2f}, "
            f"validation_failed {pen.validation_failed:+.2f}"
        )
        out.append(f"- top: confidence={g.top_confidence} wrong={g.top_wrong} jaccard={g.jaccard}")
        for note in g.notes:
            out.append(f"- note: {note}")
    for note in grade.notes:
        out.append(f"- runner note: {note}")
    if report is None:
        out.append("")
        out.append("no report.json in this cell")
        return "\n".join(out) + "\n"

    out.append("")
    out.append("## Report")
    out.append("")
    out.append(
        f"incident_present={report.incident_present} validation_failed={report.validation_failed} "
        f"onset={report.onset_estimate} population={report.affected_population!r}"
    )
    for message in report.validation_messages:
        out.append(f"- validation: {message}")
    if report.rejections is None:
        out.append("- rejections: not recorded (run predates EDW-1369)")
    else:
        for i, message in enumerate(report.rejections, 1):
            out.append(f"- rejection {i}: {message}")
    out.append("")
    out.append("## Hypotheses")
    out.append("")
    if not report.hypotheses:
        out.append("none")
    for i, h in enumerate(report.hypotheses, 1):
        dims = json.dumps(h.dims, sort_keys=True)
        out.append(f"{i}. [{h.confidence}] dims={dims} span={h.slow_or_failing_span}")
        out.append(f"   claim: {h.claim}")
        for e in h.evidence:
            out.append(f"   evidence [{e.query_id}] {e.summary}")
        if h.negation:
            out.append(f"   negation [{h.negation.query_id}] {h.negation.summary}")
        else:
            out.append("   negation: none")
    out.append("")
    out.append("## Baseline evidence")
    out.append("")
    out += [f"- [{e.query_id}] {e.summary}" for e in report.baseline_evidence] or ["none"]
    out.append("")
    out.append("## Rejected candidates")
    out.append("")
    if not report.rejected_candidates:
        out.append("none")
    for c in report.rejected_candidates:
        out.append(f"- dims={json.dumps(c.dims, sort_keys=True)} {c.claim}")
        out.append(f"  reason: {c.reason}")
    out.append("")
    out.append("## Partially checked")
    out.append("")
    if not report.partially_checked:
        out.append("none")
    for p in report.partially_checked:
        out.append(f"- {p.subject} ({p.reading}) ran: {p.queried_as} | not run: {p.not_run}")
    out.append("")
    out.append("## Not checked")
    out.append("")
    out.append(", ".join(report.not_checked) if report.not_checked else "none")
    out.append("")
    out.append(f"## Tool log ({len(report.tool_log)} calls)")
    out.append("")
    for call in report.tool_log:
        if call.name in QUERY_TOOLS:
            out.append(query_line(call, run_id=report.run_id))
        else:
            out.append(call_line(call))
    return "\n".join(out) + "\n"


def read_cell(results_dir: Path, column: str, scenario_id: str, repeat: int) -> str:
    directory = results_dir / column / scenario_id / str(repeat)
    grade_path = directory / "grade.json"
    if not grade_path.exists():
        raise FileNotFoundError(f"no grade.json at {grade_path}")
    grade = GradedRun.model_validate_json(grade_path.read_text())
    report_path = directory / "report.json"
    report = load_report(report_path) if report_path.exists() else None
    return render(grade, report)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read one result cell.")
    parser.add_argument("column", help="column path under evals/results/, for example full")
    parser.add_argument("scenario", help="scenario id")
    parser.add_argument("repeat", type=int, help="repeat number")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)
    try:
        print(read_cell(args.results_dir, args.column, args.scenario, args.repeat), end="")
    except FileNotFoundError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
