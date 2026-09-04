"""Score one investigation against the scenario's ground truth.

This grades outcomes. Honeycomb's own evaluator, `tests/scenarios/evaluator.py`
in `honeycombio/agent-skill`, grades process: which tools were called, in what
order, and whether the query arguments match a regex. Nothing in it asks
whether the answer was right. This module asks only that, and then carries a
faithful copy of their score next to ours so the eval report can show a run
that passes theirs and fails ours.

No model is involved. The same report and scenario grade to the same number
every time, which is what makes the numbers in the README reproducible.

The shape of `Grade` is deliberate. The four outcome components (dims, span,
incident, onset) and the two receipts components (receipts, not_checked) are
reported separately as well as summed, because the ablations in R10 remove
the negation requirement, and that costs the receipts weight by construction.
Whether the rule changes the outcome is a question about the outcome
components alone, and R8 has to be able to answer it without recomputing
anything.

Two checks are borrowed rather than rewritten. What counts as a citation and
whether a not-checked entry is truthful are `agent/validate.py`'s definitions,
called directly. If the validator accepted a report and the grader says its
receipts are missing, one of them has a bug.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent.report import PRIMARY_EVIDENCE_TOOL, Confidence, Hypothesis, Report, load_report
from agent.validate import (
    LogIndex,
    evidence_issues,
    index_log,
    negation_issues,
    not_checked_issues,
    queried_terms,
)
from gen.emit import RUNS_DIR, load_manifest
from gen.scenario import GroundTruth, Scenario, load_scenario

WEIGHTS: dict[str, float] = {
    "dims": 0.35,
    "span": 0.15,
    "incident": 0.15,
    "onset": 0.10,
    "receipts": 0.15,
    "not_checked": 0.10,
}
OUTCOME_COMPONENTS: tuple[str, ...] = ("dims", "span", "incident", "onset")
RECEIPTS_COMPONENTS: tuple[str, ...] = ("receipts", "not_checked")

ONSET_TOLERANCE = timedelta(minutes=3)

# A top hypothesis whose dims score below this is wrong for calibration purposes.
WRONG_BELOW = 0.5

CALIBRATION_PENALTY: dict[Confidence, float] = {"high": -0.50, "medium": -0.25, "low": -0.10}

# A right answer that hedges pays a little too. Without this a report could
# call everything `low`, never risk the big penalty, and tie a confident right
# answer, and the ordering the project rests on (hedged-right below
# confident-right) would not hold. Sized at the wrong-and-low penalty so that
# hedging on a right answer costs what hedging on a wrong one does.
UNDERCONFIDENCE_PENALTY: dict[Confidence, float] = {"high": 0.0, "medium": -0.05, "low": -0.10}
ZERO_EVIDENCE_PENALTY = -0.25
ZERO_EVIDENCE_CAP = -0.50
VALIDATION_FAILED_PENALTY = -0.25
FLOOR = -1.0

# A ground-truth value like ">=8": an operator and a number.
_RANGE = re.compile(r"^\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")


class Components(BaseModel):
    """The six component scores, each 0 to 1, before or after weighting."""

    model_config = ConfigDict(extra="forbid")

    dims: float
    span: float
    incident: float
    onset: float
    receipts: float
    not_checked: float

    def weighted(self) -> Components:
        return Components(**{name: getattr(self, name) * WEIGHTS[name] for name in WEIGHTS})


class Penalties(BaseModel):
    """Each is zero or negative. `total` is their sum.

    `calibration` is the confidence penalty on the top hypothesis: the large
    one when it is wrong, the small underconfidence one when it is right.
    """

    model_config = ConfigDict(extra="forbid")

    calibration: float = 0.0
    zero_evidence: float = 0.0
    validation_failed: float = 0.0

    @property
    def total(self) -> float:
        return self.calibration + self.zero_evidence + self.validation_failed


class HoneycombProcess(BaseModel):
    """Honeycomb's process score, reimplemented from their evaluator.

    Source: `tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, at
    commit f115bdf6d928aed4bb3ca5f386cbe5102ab30c2f (the file's last change,
    read on 2026-09-03 from main at 41214b7dfb97f262adabf295fa6f0fcad85bc0f6).
    The expectations are theirs for `investigation-cascading-failure` in
    `tests/scenarios/definitions/core_scenarios.yml`, the scenario closest to
    ours, except that `get_service_map` is not in the recommended list because
    the issue names `run_bubbleup` and `get_trace` and our client does not
    allow it. `evals/grader.md` lists the guesses.
    """

    model_config = ConfigDict(extra="forbid")

    score: float
    passed: bool
    required_found: list[str]
    required_missing: list[str]
    patterns_matched: list[str]
    patterns_missed: list[str]
    anti_pattern_violations: list[str]
    ordering_ok: bool
    recommended_found: list[str]
    recommended_missing: list[str]


class Process(BaseModel):
    """The process fields, copied through from the report. Cost is not recomputed."""

    model_config = ConfigDict(extra="forbid")

    tool_calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    wall_s: float
    honeycomb_process_score: float
    honeycomb_process_passed: bool
    honeycomb: HoneycombProcess


class Grade(BaseModel):
    """One graded run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    scenario_id: str

    components: Components
    """Raw component scores, each 0 to 1."""

    weighted: Components
    """The same scores times their weights. They sum to `before_penalties`."""

    outcome_score: float
    """Weighted dims, span, incident, and onset. At most 0.75."""

    receipts_score: float
    """Weighted receipts and not_checked. At most 0.25."""

    before_penalties: float
    penalties: Penalties
    total: float
    """`before_penalties` plus the penalties, floored at -1.0."""

    jaccard: float | None
    """Dims agreement of the top hypothesis, or None when there is no hypothesis."""

    top_confidence: Confidence | None
    top_wrong: bool
    """Whether the top hypothesis counts as wrong: dims agreement under 0.5,
    or any hypothesis at all on a control. Decides which calibration table
    applies."""

    notes: list[str] = Field(default_factory=list)
    """Why a component scored the way it did, where the number alone would not say."""

    process: Process


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def grade(
    report: Report,
    scenario: Scenario,
    *,
    window_start: datetime | None = None,
) -> Grade:
    """Score `report` against `scenario.ground_truth`.

    `window_start` is when the run's window opened. The scenario gives the
    onset as minutes into the window, and the report gives it as a timestamp,
    so the onset component needs both. The run manifest carries it; see
    `window_start_for`. Without it the onset component scores 0 with a note,
    because a component that cannot be checked is not a component that passed.
    """
    if report.scenario_id != scenario.id:
        raise ValueError(
            f"report {report.run_id} is for scenario {report.scenario_id!r}, not {scenario.id!r}"
        )

    truth = scenario.ground_truth
    notes: list[str] = []
    index = index_log(report.tool_log)
    terms = queried_terms(report.tool_log, run_id=report.run_id)
    top = report.hypotheses[0] if report.hypotheses else None

    if truth.incident_present:
        jaccard = _best_dims_jaccard(top.dims, truth) if top else None
        dims = jaccard or 0.0
        span = float(top is not None and top.slow_or_failing_span == truth.slow_or_failing_span)
        onset = _onset_score(report, scenario, window_start, notes)
        top_wrong = top is not None and dims < WRONG_BELOW
    else:
        # A control has no cause to name. The right report names none, or
        # names only low-confidence ones, and those three components are
        # awarded for that restraint.
        jaccard = 0.0 if top else None
        quiet = all(h.confidence == "low" for h in report.hypotheses)
        dims = span = onset = float(quiet)
        top_wrong = top is not None
        if not quiet:
            notes.append("control: a hypothesis above low confidence zeroes dims, span, and onset")

    incident = float(report.incident_present == truth.incident_present)
    receipts = _receipts_score(report, index, notes)
    not_checked = float(
        bool(report.not_checked) and not not_checked_issues(report.not_checked, terms)
    )
    if not report.not_checked:
        notes.append("not_checked is empty")
    elif not not_checked:
        false_entries = [issue.message for issue in not_checked_issues(report.not_checked, terms)]
        notes.extend(f"not_checked: {message}" for message in false_entries)

    components = Components(
        dims=dims,
        span=span,
        incident=incident,
        onset=onset,
        receipts=receipts,
        not_checked=not_checked,
    )
    weighted = components.weighted()
    outcome_score = sum(getattr(weighted, name) for name in OUTCOME_COMPONENTS)
    receipts_score = sum(getattr(weighted, name) for name in RECEIPTS_COMPONENTS)
    before = outcome_score + receipts_score

    if top is None:
        calibration = 0.0
    elif top_wrong:
        calibration = CALIBRATION_PENALTY[top.confidence]
    else:
        calibration = UNDERCONFIDENCE_PENALTY[top.confidence]
    penalties = Penalties(
        calibration=calibration,
        zero_evidence=max(
            ZERO_EVIDENCE_CAP,
            ZERO_EVIDENCE_PENALTY * sum(1 for h in report.hypotheses if _zero_evidence(h, index)),
        ),
        validation_failed=VALIDATION_FAILED_PENALTY if report.validation_failed else 0.0,
    )
    total = max(FLOOR, before + penalties.total)
    honeycomb = honeycomb_process_score(report)

    return Grade(
        run_id=report.run_id,
        scenario_id=scenario.id,
        components=components,
        weighted=weighted,
        outcome_score=round(outcome_score, 6),
        receipts_score=round(receipts_score, 6),
        before_penalties=round(before, 6),
        penalties=penalties,
        total=round(total, 6),
        jaccard=None if jaccard is None else round(jaccard, 6),
        top_confidence=top.confidence if top else None,
        top_wrong=top_wrong,
        notes=notes,
        process=Process(
            tool_calls=report.tool_calls,
            tokens_in=report.tokens_in,
            tokens_out=report.tokens_out,
            cost_usd=report.cost_usd,
            wall_s=report.wall_s,
            honeycomb_process_score=honeycomb.score,
            honeycomb_process_passed=honeycomb.passed,
            honeycomb=honeycomb,
        ),
    )


def window_start_for(run_id: str, runs_dir: Path = RUNS_DIR) -> datetime | None:
    """When the run's window opened, from its manifest, or None if there is none."""
    try:
        manifest = load_manifest(run_id, runs_dir)
    except FileNotFoundError:
        return None
    return datetime.fromtimestamp(manifest.window_start_s, tz=UTC)


def grade_file(
    path: Path,
    *,
    scenario: Scenario | None = None,
    runs_dir: Path = RUNS_DIR,
) -> Grade:
    """Grade a report on disk, loading the scenario and the manifest by the ids it carries."""
    report = load_report(path)
    scenario = scenario or load_scenario(report.scenario_id)
    return grade(report, scenario, window_start=window_start_for(report.run_id, runs_dir))


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------


def dims_jaccard(reported: dict[str, str], truth: dict[str, str]) -> float:
    """Jaccard over `key=value` pairs, with ground-truth ranges honoured.

    A truth pair matches when the report names the same key and its value
    satisfies the truth value: equal as a string, or, when the truth is a
    range like `>=8`, a number inside it or the same range written the same
    way. Every other pair on either side is in the union and nowhere else.

    So a report naming two of three true dims and nothing false scores 2/3,
    and one naming all three plus a spurious fourth scores 3/4. An extra pair
    costs the same as a missing one: the spurious dim was a claim about the
    population, and it was wrong. Two live payments reports carry
    `name: payments.charge`, which is the span rather than a dimension, and
    they pay for it here, because that scenario declares no equivalent for
    it. This function scores one candidate against `reported`; `grade` calls
    it once per candidate in `_best_dims_jaccard` and keeps the best.
    """
    if not reported and not truth:
        return 1.0
    matched = sum(
        1 for key, want in truth.items() if key in reported and _satisfies(reported[key], want)
    )
    union = len(truth) + len(reported) - matched
    return matched / union if union else 0.0


def _best_dims_jaccard(reported: dict[str, str], truth: GroundTruth) -> float:
    """The dims score `grade` uses: the best of `root_cause_dims` and every
    `equivalent_dims` alternative.

    `gen/scenario.py` checks each alternative against the topology at load
    time: it keeps every population-restricting dimension of `fault.where`
    and may also name the fault's own span or the service that runs it, so
    it selects the same requests as `root_cause_dims` under a different
    name. A scenario that declares no equivalent for a dimension still
    charges the usual way for it: `name: payments.charge` on the payments
    scenario is a spurious dim there, not an alternative selector.
    """
    return max(
        dims_jaccard(reported, cand) for cand in (truth.root_cause_dims, *truth.equivalent_dims)
    )


def _satisfies(value: str, want: str) -> bool:
    if value.strip() == want.strip():
        return True
    rng = _RANGE.match(want)
    if rng is None:
        return False
    op, bound = rng.group(1), float(rng.group(2))
    as_range = _RANGE.match(value)
    if as_range is not None:
        return as_range.group(1) == op and float(as_range.group(2)) == bound
    try:
        number = float(value)
    except ValueError:
        return False
    if math.isnan(number):
        return False
    return {
        ">=": number >= bound,
        "<=": number <= bound,
        ">": number > bound,
        "<": number < bound,
    }[op]


def _onset_score(
    report: Report,
    scenario: Scenario,
    window_start: datetime | None,
    notes: list[str],
) -> float:
    if report.onset_estimate is None:
        notes.append("onset: no estimate")
        return 0.0
    if window_start is None:
        notes.append("onset: window start unknown (no run manifest), scored 0")
        return 0.0
    assert scenario.onset_min is not None  # an incident scenario always has a fault
    true_onset = _utc(window_start) + timedelta(minutes=scenario.onset_min)
    delta = abs(_utc(report.onset_estimate) - true_onset)
    if delta <= ONSET_TOLERANCE:
        return 1.0
    notes.append(f"onset: estimate is {delta.total_seconds() / 60:.1f} min from the true onset")
    return 0.0


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _receipts_score(report: Report, index: LogIndex, notes: list[str]) -> float:
    """The share of hypotheses that carry a citation and a negation the validator accepts.

    With no hypotheses the question moves to the baseline: a report that says
    nothing happened has receipts when its baseline evidence cites a run_query
    that was made, and none otherwise. That is the validator's rule too.
    """
    if not report.hypotheses:
        from_queries = index.ids_from(PRIMARY_EVIDENCE_TOOL)
        if any(item.query_id in from_queries for item in report.baseline_evidence):
            return 1.0
        notes.append("receipts: no hypotheses and no baseline run_query in the log")
        return 0.0

    supported = 0
    for position, hypothesis in enumerate(report.hypotheses):
        label = f"hypotheses[{position}]"
        issues = evidence_issues(hypothesis, index, label=label)
        issues += negation_issues(hypothesis, index, label=label, require_negation=True)
        if issues:
            notes.extend(f"receipts: {issue.message}" for issue in issues)
        else:
            supported += 1
    return supported / len(report.hypotheses)


def _zero_evidence(hypothesis: Hypothesis, index: LogIndex) -> bool:
    """No evidence entry resolves to a call that succeeded. An invented id is no evidence."""
    return not any(item.query_id in index.all_ids for item in hypothesis.evidence)


# --------------------------------------------------------------------------
# Honeycomb's process score
# --------------------------------------------------------------------------

HC_REQUIRED_TOOLS = ["get_workspace_context", "run_query"]
HC_RECOMMENDED_TOOLS = ["run_bubbleup", "get_trace"]
HC_REQUIRED_PATTERNS = [r'"op":\s*"(HEATMAP|P99|P95|P90)"', r'"column":\s*"error"']
HC_ANTI_PATTERNS = [r'"op":\s*"AVG",\s*"column":\s*"duration']
HC_TOOL_ORDERING = [["get_workspace_context", "run_query"]]

HC_WEIGHT_REQUIRED_TOOLS = 0.30
HC_WEIGHT_REQUIRED_PATTERNS = 0.25
HC_WEIGHT_ANTI_PATTERNS = 0.20
HC_WEIGHT_TOOL_ORDERING = 0.15
HC_WEIGHT_RECOMMENDED_TOOLS = 0.10
HC_PASS_THRESHOLD = 0.6


def honeycomb_process_score(report: Report) -> HoneycombProcess:
    """Their `evaluate`, over our tool log.

    Every call in the log counts, including ones the server rejected, because
    their runner sees tool calls in the transcript and has no error flag.
    `raw_arguments` is the JSON of the argument list, as theirs is.
    """
    names = [call.name for call in report.tool_log]
    arguments = json.dumps([call.args for call in report.tool_log])

    required_found = [tool for tool in HC_REQUIRED_TOOLS if tool in names]
    required_ratio = len(required_found) / len(HC_REQUIRED_TOOLS)

    matched = [pattern for pattern in HC_REQUIRED_PATTERNS if re.search(pattern, arguments)]
    pattern_ratio = len(matched) / len(HC_REQUIRED_PATTERNS)

    anti_text = " ".join(names) + " " + arguments
    violations = [pattern for pattern in HC_ANTI_PATTERNS if re.search(pattern, anti_text)]
    anti_ratio = 1.0 - len(violations) / len(HC_ANTI_PATTERNS)

    in_order = sum(1 for order in HC_TOOL_ORDERING if _in_order(names, order))
    ordering_ratio = in_order / len(HC_TOOL_ORDERING)

    recommended_found = [tool for tool in HC_RECOMMENDED_TOOLS if tool in names]
    recommended_ratio = len(recommended_found) / len(HC_RECOMMENDED_TOOLS)

    score = (
        required_ratio * HC_WEIGHT_REQUIRED_TOOLS
        + pattern_ratio * HC_WEIGHT_REQUIRED_PATTERNS
        + anti_ratio * HC_WEIGHT_ANTI_PATTERNS
        + ordering_ratio * HC_WEIGHT_TOOL_ORDERING
        + recommended_ratio * HC_WEIGHT_RECOMMENDED_TOOLS
    )
    return HoneycombProcess(
        score=round(score, 6),
        passed=score >= HC_PASS_THRESHOLD,
        required_found=required_found,
        required_missing=[tool for tool in HC_REQUIRED_TOOLS if tool not in names],
        patterns_matched=matched,
        patterns_missed=[pattern for pattern in HC_REQUIRED_PATTERNS if pattern not in matched],
        anti_pattern_violations=violations,
        ordering_ok=in_order == len(HC_TOOL_ORDERING),
        recommended_found=recommended_found,
        recommended_missing=[tool for tool in HC_RECOMMENDED_TOOLS if tool not in names],
    )


def _in_order(names: Sequence[str], order: Sequence[str]) -> bool:
    """Their `_check_order`: each tool appears after the previous one, not necessarily adjacent."""
    last = -1
    for tool in order:
        try:
            last = names.index(tool, last + 1)
        except ValueError:
            return False
    return True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def arithmetic(result: Grade) -> str:
    """The sum, written out, so a number in the README can be checked by hand."""
    parts = [
        f"{name} {getattr(result.components, name):.3f}x{WEIGHTS[name]:.2f}" for name in WEIGHTS
    ]
    lines = [
        f"{result.run_id} on {result.scenario_id}",
        "  " + " + ".join(parts) + f" = {result.before_penalties:.3f}",
        f"  outcome {result.outcome_score:.3f}, receipts {result.receipts_score:.3f}",
        f"  penalties: calibration {result.penalties.calibration:+.2f}, "
        f"zero evidence {result.penalties.zero_evidence:+.2f}, "
        f"validation failed {result.penalties.validation_failed:+.2f}",
        f"  total {result.total:.3f}",
        f"  honeycomb process score {result.process.honeycomb_process_score:.3f} "
        f"({'pass' if result.process.honeycomb_process_passed else 'fail'})",
    ]
    lines += [f"  note: {note}" for note in result.notes]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade a report on disk against its scenario.")
    parser.add_argument("report", type=Path, nargs="+", help="path to a report.json")
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR, help="where run manifests live")
    parser.add_argument("--json", action="store_true", help="print the Grade as JSON")
    args = parser.parse_args(argv)
    for path in args.report:
        result = grade_file(path, runs_dir=args.runs_dir)
        if args.json:
            print(result.model_dump_json(indent=2))
        else:
            print(arithmetic(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
