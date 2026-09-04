"""Check a submitted report against the log of what the run actually did.

The two rules that are the point of this project are enforced here, in code,
against the tool log. Nothing in this module trusts a field the model wrote.

  Receipts. Every hypothesis carries at least one `run_query` that was made in
  this run, and, when negation is required, a query that was made in this run
  and that actually excluded one of the dimensions the hypothesis claims. A
  range claim such as `cart.size: ">= 8"` is negated by its complementary
  comparison (`cart.size < 8`) at the same bound, rather than by `!=`. Every
  `query_id` in the report has to appear in the log against the tool that
  returned it.

  Baseline. Both answers cite what the window looked like beforehand. Asking
  it of only one of them made the code cheaper to pass in one direction than
  the other, which is the code choosing an answer.

  Not checked. Every entry in `not_checked` names something that is absent from
  the arguments of the queries that were run. An entry naming a column the run
  broke down on is a false claim about the run's own coverage, which is worse
  than an empty list.

A citation is a pair, not a string. The hosted MCP returns the same `query_id`
from `run_query`, from a `run_bubbleup` built on that query, and from
`get_query_results` reading it back. Matching on the identifier alone therefore
let a BubbleUp ranking satisfy the requirement to cite rows. Every check here
matches the identifier against the tool that produced it, and ignores calls the
server flagged as errors.

The vocabulary comes from `charles/api/src/services/fact-checker.ts`:
`unsupported` for a claim with nothing behind it, `partial` for one that goes
further than its evidence does. The handling comes from there too. A first
failure is handed back to the model with the reasons, the way that service
holds a document until a person clears the flagged claims. A second failure
keeps the report and flags it rather than discarding the run, because a report
that failed its own rules is a result the grader should see and punish.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from agent.report import (
    PRIMARY_EVIDENCE_TOOL,
    Evidence,
    Hypothesis,
    ReportDraft,
    ToolCall,
)
from gen.topology import RANGE_RE

# Tools whose arguments name columns and values, which is what "was queried"
# means for the not-checked list. Discovery calls such as `find_columns` are
# not in here: looking a column up is not measuring it.
QUERY_TOOLS: frozenset[str] = frozenset({"run_query", "run_bubbleup", "list_spans"})

# Filter operators that cut a population out rather than select one. A negation
# query has to carry one of these on a dimension the hypothesis claims. That is
# what separates a real `WHERE NOT` from a second look at the same rows.
EXCLUDING_OPS: frozenset[str] = frozenset(
    {
        "!=",
        "not-in",
        "does-not-contain",
        "does-not-start-with",
        "does-not-end-with",
        "does-not-exist",
        "not-exists",
    }
)

# Argument keys that carry a column name or a filter value.
_COLUMN_KEYS: frozenset[str] = frozenset({"column", "columns", "breakdowns", "group_by"})
_VALUE_KEYS: frozenset[str] = frozenset({"value", "values"})

# The complementary comparison for each range operator: what negates a claim
# of `>= 8` is `< 8` at the same bound, not `!=`. `RANGE_RE` (from
# `gen.topology`, the one definition of what a range value looks like) is
# what tells a range claim like "cart.size: >= 8" apart from an exact one.
_COMPLEMENT_OP: dict[str, str] = {">=": "<", ">": "<=", "<=": ">", "<": ">="}

# Identifier-shaped words inside a free-text `not_checked` entry. A match has
# to carry a dot, an underscore, or a hyphen, which is what separates
# `payments.charge`, `duration_ms`, and `us-west-2` from ordinary prose. A
# live run showed why: an entry reading "did not examine specific error
# message text" was rejected for naming the `error` column, which it was not.
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[.\-][A-Za-z0-9_]+)+|[A-Za-z]+_[A-Za-z0-9_]+")

# A single-word column is only caught when the entry points at it as a column:
# in backticks or quotes, or as the whole entry. That keeps `error` on its own
# from getting through while leaving "specific error message text" alone.
_QUOTED = re.compile(r"[`\"']\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*[`\"']")

# Models write entries as "column - why it was not checked". The claim is the
# subject, and the explanation after the separator names other columns as
# context. A live run listed "deployment.version - did not break down
# payments.charge by deployment version", which was true: the run never
# queried the version. Reading every word of it flagged the entry for naming
# `payments.charge`, a column the run did query but the entry never claimed
# was unchecked. So the check reads the subject when there is one.
_SUBJECT = re.compile(r"^(.*?)(?:\s+[-:]\s+|\s*:\s+)")


@dataclass(frozen=True)
class Issue:
    """One reason a report was rejected."""

    code: str
    """`unsupported`, `partial`, `not_checked_empty`, or `not_checked_false`."""

    message: str
    """What is wrong, addressed to the model, so it can be handed straight back."""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class LogIndex:
    """The tool log, keyed the way the checks need to read it.

    Only calls the server did not flag as errors are indexed. A failed call
    returns no rows, so its identifier is not a receipt for anything.
    """

    ids_by_tool: dict[str, set[str]] = field(default_factory=dict)
    calls_by_id: dict[str, list[ToolCall]] = field(default_factory=dict)

    @property
    def all_ids(self) -> set[str]:
        return {qid for ids in self.ids_by_tool.values() for qid in ids}

    def ids_from(self, tool: str) -> set[str]:
        return self.ids_by_tool.get(tool, set())

    def calls_for(self, query_id: str, *, tool: str) -> list[ToolCall]:
        return [call for call in self.calls_by_id.get(query_id, []) if call.name == tool]


def index_log(tool_log: Sequence[ToolCall]) -> LogIndex:
    """Index the successful calls by tool and by identifier."""
    ids_by_tool: dict[str, set[str]] = {}
    calls_by_id: dict[str, list[ToolCall]] = {}
    for call in tool_log:
        if not call.query_id or call.is_error:
            continue
        ids_by_tool.setdefault(call.name, set()).add(call.query_id)
        calls_by_id.setdefault(call.query_id, []).append(call)
    return LogIndex(ids_by_tool=ids_by_tool, calls_by_id=calls_by_id)


def query_ids(tool_log: Sequence[ToolCall], *, tools: Iterable[str] | None = None) -> set[str]:
    """Every `query_id` the run collected, optionally from named tools only.

    Calls the server flagged as errors are left out.
    """
    allowed = None if tools is None else set(tools)
    return {
        call.query_id
        for call in tool_log
        if call.query_id and not call.is_error and (allowed is None or call.name in allowed)
    }


def queried_terms(tool_log: Sequence[ToolCall], *, run_id: str | None = None) -> set[str]:
    """The columns and values the run's queries named.

    The run id is dropped: it is on every query by construction, so it is not
    evidence that anything was investigated. A call the server rejected is
    dropped too: it returned no rows, so it measured nothing.
    """
    terms: set[str] = set()
    for call in tool_log:
        if call.name in QUERY_TOOLS and not call.is_error:
            _collect(call.args, None, terms)
    terms.discard("scenario.run_id")
    if run_id:
        terms.discard(run_id)
    return terms


def excluded_columns(
    calls: Sequence[ToolCall], *, dims: Mapping[str, str] | None = None
) -> set[str]:
    """The columns these calls filtered out, by any operator that excludes.

    With `dims`, a filter on a claimed dimension whose claimed value is a
    numeric range (matches `RANGE_RE`, e.g. ">= 8") also counts when the
    filter's operator is the complement at the same bound (`< 8`), even
    though `<` is not itself in `EXCLUDING_OPS`: the complement is what
    negates a range claim, the way `!=` negates an exact one. Plain values
    are unaffected. `dims` defaults to None, which is the old behaviour.

    A complement on a column the same query measures does not count. A
    claim of `duration_ms > 1000` negated by `P99(duration_ms) WHERE
    duration_ms <= 1000` says that fast requests are fast. That is a second
    look at the symptom, not a test of a cause, and the review of the first
    version of this rule found a stored run that would have been credited
    for exactly that. The population a negation excludes has to be one the
    measurement can move on.
    """
    out: set[str] = set()
    for call in calls:
        _collect_exclusions(call.args, out, dims=dims, measured=_measured_columns(call.args))
    return out


def _measured_columns(args: Mapping[str, object]) -> frozenset[str]:
    """The columns a `run_query`'s calculations are computed over."""
    spec = args.get("query_spec")
    if not isinstance(spec, dict):
        return frozenset()
    calculations = spec.get("calculations")
    if not isinstance(calculations, list):
        return frozenset()
    return frozenset(
        calc["column"]
        for calc in calculations
        if isinstance(calc, dict) and isinstance(calc.get("column"), str)
    )


def _parse_number(value: object) -> float | None:
    """`value` as a float, or None when it is not one.

    A filter's bound may arrive as a number or as a numeric string; a value
    such as "eight" does not parse and does not match. Booleans are excluded
    even though `bool` is an `int` subclass: `True` is not a bound.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _range_complement_match(claimed_value: str, op: str, filter_value: object) -> bool:
    """True when `op filter_value` is the complement of a range claim, at the same bound."""
    claimed = RANGE_RE.match(claimed_value)
    if claimed is None:
        return False
    if op != _COMPLEMENT_OP[claimed.group(1)]:
        return False
    bound = _parse_number(filter_value)
    if bound is None:
        return False
    return bound == float(claimed.group(2))


def _collect(node: object, key: str | None, out: set[str]) -> None:
    """Walk one arguments tree, collecting column names and filter values."""
    if isinstance(node, dict):
        for name, value in node.items():
            # A BubbleUp group selection is `{"group": {"column": "value"}}`,
            # so both sides of it are terms the run named.
            if name == "group" and isinstance(value, dict):
                out.update(str(k) for k in value)
                out.update(str(v) for v in value.values() if isinstance(v, str))
            _collect(value, name, out)
    elif isinstance(node, list):
        for item in node:
            _collect(item, key, out)
    elif isinstance(node, str) and key in (_COLUMN_KEYS | _VALUE_KEYS):
        out.add(node)


def _collect_exclusions(
    node: object,
    out: set[str],
    *,
    dims: Mapping[str, str] | None = None,
    measured: frozenset[str] = frozenset(),
) -> None:
    """Walk one arguments tree for filter clauses that exclude a population.

    Per-calculation filters count. A query that measures the population and
    its complement in one shot is the shape Honeycomb's own guidance asks for.
    `measured` names the columns the query calculates over; a range
    complement on one of them is not an exclusion (see `excluded_columns`).
    """
    if isinstance(node, dict):
        op = node.get("op")
        column = node.get("column")
        if isinstance(op, str) and isinstance(column, str):
            if op in EXCLUDING_OPS:
                out.add(column)
            elif (
                dims
                and column in dims
                and column not in measured
                and _range_complement_match(dims[column], op, node.get("value"))
            ):
                out.add(column)
        for value in node.values():
            _collect_exclusions(value, out, dims=dims, measured=measured)
    elif isinstance(node, list):
        for item in node:
            _collect_exclusions(item, out, dims=dims, measured=measured)


def validate_draft(
    draft: ReportDraft,
    tool_log: Sequence[ToolCall],
    *,
    run_id: str | None = None,
    require_negation: bool = True,
    require_not_checked: bool = True,
) -> list[Issue]:
    """Every reason this report should not be accepted. Empty means accepted."""
    issues: list[Issue] = []
    index = index_log(tool_log)
    terms = queried_terms(tool_log, run_id=run_id)

    issues += _check_hypotheses(draft, index, terms, require_negation=require_negation)
    issues += _check_baseline(draft, index)
    if require_not_checked:
        issues += not_checked_issues(draft.not_checked, terms)
    return issues


def _check_hypotheses(
    draft: ReportDraft,
    index: LogIndex,
    terms: set[str],
    *,
    require_negation: bool,
) -> list[Issue]:
    issues: list[Issue] = []
    if draft.incident_present and not draft.hypotheses:
        issues.append(
            Issue(
                "unsupported",
                "incident_present is true but there are no hypotheses. Name what happened "
                "or set incident_present to false.",
            )
        )

    for position, hypothesis in enumerate(draft.hypotheses):
        label = f"hypotheses[{position}] ({hypothesis.claim[:60]!r})"
        issues += evidence_issues(hypothesis, index, label=label)
        issues += _check_dims(hypothesis, label, terms)
        issues += negation_issues(hypothesis, index, label=label, require_negation=require_negation)

    return issues


def evidence_issues(hypothesis: Hypothesis, index: LogIndex, *, label: str) -> list[Issue]:
    """Why this hypothesis's evidence does not count as a citation. Empty means it does.

    Public because the grader scores the receipts rule with this exact check.
    One definition of a citation, so the validator and the grader cannot
    disagree about what counts.
    """
    return _check_evidence(hypothesis, label, index)


def negation_issues(
    hypothesis: Hypothesis,
    index: LogIndex,
    *,
    label: str,
    require_negation: bool = True,
) -> list[Issue]:
    """Why this hypothesis's negation does not count as one. Empty means it does.

    A missing negation is an issue only when negation is required. That is
    the R10 ablation knob; the grader never turns it off.
    """
    if hypothesis.negation is None:
        if not require_negation:
            return []
        return [
            Issue(
                "partial",
                f"{label} has no negation. Run the same measurement WHERE NOT the "
                "dimensions in dims, over the same window, and cite that query_id.",
            )
        ]
    return _check_negation(hypothesis, label, index, require_negation=require_negation)


def _check_evidence(hypothesis: Hypothesis, label: str, index: LogIndex) -> list[Issue]:
    if not hypothesis.evidence:
        return [
            Issue(
                "unsupported",
                f"{label} carries no evidence. Every hypothesis needs at least one "
                "run_query from this session whose rows support it.",
            )
        ]
    issues = _check_ids(hypothesis.evidence, index, f"{label} evidence")
    from_queries = index.ids_from(PRIMARY_EVIDENCE_TOOL)
    if not any(item.query_id in from_queries for item in hypothesis.evidence):
        issues.append(
            Issue(
                "partial",
                f"{label} cites no run_query. BubbleUp and traces point at a cause; "
                "rows are what carry it. Run the query and cite it.",
            )
        )
    return issues


def _check_dims(hypothesis: Hypothesis, label: str, terms: set[str]) -> list[Issue]:
    """The dimensions a hypothesis claims have to be dimensions it measured.

    `dims` is the field the grader scores, so it is the field most worth
    checking. A column that appears in no query's arguments was not measured,
    whatever the claim says about it.
    """
    unmeasured = sorted(column for column in hypothesis.dims if column not in terms)
    if not unmeasured:
        return []
    return [
        Issue(
            "unsupported",
            f"{label} names {unmeasured} in dims, and no query in this session filtered or "
            "broke down on them. Measure a dimension before claiming it.",
        )
    ]


def _check_negation(
    hypothesis: Hypothesis,
    label: str,
    index: LogIndex,
    *,
    require_negation: bool,
) -> list[Issue]:
    """A negation has to be a query that excluded what the hypothesis claims.

    Presence of a second identifier is not enough. Without this the rule can be
    satisfied by citing the evidence query twice, or by citing an unrelated
    ranking, which would make the receipts rule a formality.

    One claimed dimension is enough. A live run reported three dimensions and
    negated the deployment version alone, which is a real test of the claim, so
    requiring every dimension to be excluded would reject a correct report.
    """
    negation = hypothesis.negation
    if negation is None:  # pragma: no cover - guarded by the caller
        return []

    issues = _check_ids([negation], index, f"{label} negation")
    if issues:
        return issues

    if negation.query_id not in index.ids_from(PRIMARY_EVIDENCE_TOOL):
        return [
            Issue(
                "partial",
                f"{label} negation cites {negation.query_id!r}, which no run_query in this "
                "session returned. A negation is a measurement, so it has to be a run_query.",
            )
        ]

    if negation.query_id in {item.query_id for item in hypothesis.evidence}:
        return [
            Issue(
                "partial",
                f"{label} negation cites {negation.query_id!r}, the same query as its own "
                "evidence. Run the measurement again WHERE NOT the dimensions in dims.",
            )
        ]

    if not require_negation:
        return []

    if not hypothesis.dims:
        return [
            Issue(
                "partial",
                f"{label} carries a negation but no dims, so there is nothing for it to "
                "exclude. Name the dimensions that select the affected population.",
            )
        ]

    excluded = excluded_columns(
        index.calls_for(negation.query_id, tool=PRIMARY_EVIDENCE_TOOL), dims=hypothesis.dims
    )
    claimed = set(hypothesis.dims)
    if not (excluded & claimed):
        return [
            Issue(
                "partial",
                f"{label} negation query {negation.query_id!r} excludes {sorted(excluded)} and "
                f"the claim rests on {sorted(claimed)}. Run the same measurement with a "
                "!= or not-in on at least one of the dimensions in dims, or, for a range "
                "value such as >= 8, the complementary comparison (< 8) at the same bound "
                "on a column the query does not itself calculate over.",
            )
        ]
    return []


def _check_baseline(draft: ReportDraft, index: LogIndex) -> list[Issue]:
    """Both answers need the baseline, and for the same reason.

    This used to be asked only of a report that said nothing happened, which
    made denying an incident cost a query that asserting one did not. The
    cheaper path was the one the code enforced. An incident is a claim that
    something changed, so it rests on what the level was beforehand exactly as
    a quiet window rests on the level holding steady.

    What the baseline query has to show is not prescribed here. The phases
    live in the prompt and the model picks its own queries, so a check that
    demanded a particular shape would be this module writing the method.
    """
    issues = _check_ids(draft.baseline_evidence, index, "baseline_evidence")
    for candidate in draft.rejected_candidates:
        issues += _check_ids(
            candidate.evidence, index, f"rejected candidate {candidate.claim[:40]!r}"
        )

    from_queries = index.ids_from(PRIMARY_EVIDENCE_TOOL)
    if not draft.baseline_evidence:
        if draft.incident_present:
            issues.append(
                Issue(
                    "unsupported",
                    "baseline_evidence is empty. An incident is a change, so cite at least one "
                    "run_query whose rows show what the measurement was before it.",
                )
            )
        else:
            issues.append(
                Issue(
                    "unsupported",
                    "incident_present is false and baseline_evidence is empty. Cite at least one "
                    "run_query whose rows show the measurement flat across the window.",
                )
            )
    elif not any(item.query_id in from_queries for item in draft.baseline_evidence):
        issues.append(
            Issue(
                "partial",
                "baseline_evidence cites no run_query. What the window looked like beforehand "
                "is a claim about rows.",
            )
        )
    return issues


def _check_ids(evidence: Sequence[Evidence], index: LogIndex, label: str) -> list[Issue]:
    known = index.all_ids
    return [
        Issue(
            "unsupported",
            f"{label} cites query_id {item.query_id!r}, which no successful tool call in this "
            "session returned. Cite an identifier from a result you received.",
        )
        for item in evidence
        if item.query_id not in known
    ]


def not_checked_issues(not_checked: Sequence[str], terms: set[str]) -> list[Issue]:
    """Why the not-checked list is not truthful. Empty means it is.

    Public for the same reason as `evidence_issues`: the grader's not-checked
    component is this check, not a second opinion of it.
    """
    if not not_checked:
        return [
            Issue(
                "not_checked_empty",
                "not_checked is empty. Name the dimensions, spans, or time windows that were "
                "in scope and that you did not query.",
            )
        ]

    lowered = {term.lower() for term in terms}
    issues: list[Issue] = []
    for entry in not_checked:
        named = sorted({token for token in _candidates(entry) if token.lower() in lowered})
        if named:
            issues.append(
                Issue(
                    "not_checked_false",
                    f"not_checked entry {entry!r} names {named}, which your own "
                    "queries used. Take it off the list.",
                )
            )
    return issues


def _candidates(entry: str) -> set[str]:
    """The columns one entry claims were not checked.

    When the entry has the shape "subject - explanation", only the subject is
    the claim; identifiers in the explanation are context. Otherwise every
    identifier-shaped word counts, plus anything quoted, plus the entry itself
    when it is a bare name.

    The cost is that a false claim buried in the explanation of a true one
    gets through. That is the cheaper mistake: the subject is what the list is
    for, and a false rejection burns a turn and reads as the validator being
    wrong about a report that was honest.
    """
    subject = _SUBJECT.match(entry)
    text = subject.group(1) if subject and subject.group(1).strip() else entry
    found = set(_TOKEN.findall(text))
    found |= set(_QUOTED.findall(text))
    stripped = text.strip().strip("`\"'").strip()
    if stripped and " " not in stripped:
        found.add(stripped)
    return found


def rejection_message(issues: Sequence[Issue]) -> str:
    """The text handed back to the model after a rejected report."""
    lines = [
        "The report was rejected. Every claim in it is checked against the log of the tool "
        "calls you made in this session, and these did not hold up:",
        "",
    ]
    lines += [f"  {issue}" for issue in issues]
    lines += [
        "",
        "Fix the report and call submit_report again. Run whatever queries you still need "
        "first, within the remaining budget.",
    ]
    return "\n".join(lines)
