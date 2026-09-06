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
  the arguments of the queries that were run, folded together with the values
  a `run_query` breakdown actually returned (`ToolCall.result_values`), which
  is a value the model was shown whether or not the query's own arguments
  happened to name it. An entry naming a column the run broke down on is a
  false claim about the run's own coverage, which is worse than an empty
  list. An entry naming only instrumentation or trace-structure columns
  (`meta.*`, `telemetry.*`, `trace.*`, `span.*`, `library.*`, `scenario.*`,
  or `type`, `parent_name`, `service.name`) is rejected too: those describe
  the instrumentation and the trace's own shape. They say nothing about the
  traffic, so they are out of scope for a list that exists to say which
  dimensions and spans were never looked at.

  Partially checked. Every entry in `partially_checked` names a subject that a
  query did use, checked the other way round from `not_checked`: its `subject`
  has to appear among the terms the run queried, the same set, and it has to
  be a column a query broke down, filtered, or calculated over. A bare value
  that only ever showed up inside one of those does not qualify. It is the
  slot for a column that was measured but not read one particular way:
  present in the log, not yet ruled out as a candidate. The reading it claims
  was missing is itself checked against the log: a query that broke down on
  the subject contradicts `per_value`, one that broke down on it or calculated over it
  with a granularity contradicts `over_time` (a filter to one of its values
  with a granularity reads that value over time rather than the column, and
  the EDW-1367 after-pass had two entries rejected on exactly that), one that
  carried it in a breakdown, a filter, or a calculation with no other filter
  narrowing the traffic contradicts `outside_selection`, and one whose
  calculations already computed the named `measurement` contradicts
  `other_measurement`.

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
    PartialCheck,
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


@dataclass(frozen=True)
class _SystemColumns:
    """Columns that describe the instrumentation or the trace's own shape.
    They say nothing about the traffic running through it, and so are out of
    scope for `not_checked`: they pass Rule two by construction (no run ever
    breaks down on `trace.span_id` to learn something about the traffic) and
    would otherwise fill the list with entries that say nothing about which
    rows were affected.

    `status_code`, `status_message`, `error`, and `exception.*` are not in
    here even though they sound like instrumentation: a fault shows up in
    those columns, so a run that never looked is missing something real
    about the traffic. That is a different gap from how the trace was
    recorded.
    """

    prefixes: frozenset[str]
    names: frozenset[str]

    def __contains__(self, column: str) -> bool:
        lowered = column.lower()
        return lowered in self.names or any(lowered.startswith(prefix) for prefix in self.prefixes)


SYSTEM_COLUMNS = _SystemColumns(
    prefixes=frozenset({"meta.", "telemetry.", "library.", "trace.", "span.", "scenario."}),
    names=frozenset({"type", "parent_name", "service.name"}),
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
    """One of: `unsupported`, `partial`, `not_checked_empty`, `not_checked_false`,
    `not_checked_out_of_scope`, `partially_checked_false`,
    `partially_checked_subject_not_a_column`, `partially_checked_incomplete`,
    or `partially_checked_contradicted`."""

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

    A successful `run_query`'s `result_values` (the distinct values its
    breakdown columns actually took, per `agent/format.py`'s
    `breakdown_values`) are folded in too. That is a value the model was
    shown, whether or not the args that requested the query happened to name
    it, so it counts as queried the same way an argument does. Only a value
    the model was actually shown counts: `breakdown_values` itself is capped
    to the same `MAX_QUERY_ROWS` rows the rendered table shows, so a value
    that appeared only past that cap was never recorded here to begin with.
    """
    terms: set[str] = set()
    for call in tool_log:
        if call.name in QUERY_TOOLS and not call.is_error:
            _collect(call.args, None, terms)
            for values in call.result_values.values():
                terms.update(values)
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


@dataclass(frozen=True)
class Usage:
    """One query's use of one column, for the `partially_checked` contradiction
    checks in `partially_checked_issues`.

    `other_filter_columns` is what tells `outside_selection` a query measured
    the traffic as a whole rather than some slice of it: the run id column
    (`scenario.run_id`, on every query by construction) and the subject
    column itself are excluded, so an empty set means nothing else narrowed
    the population this query looked at.

    A column named only by an `orders` or `havings` entry, never a breakdown,
    filter, or calculation, still gets a record (so `partially_checked` does
    not call it `partially_checked_subject_not_a_column`), but every flag on
    it is False and `calculations` is empty: sorting or filtering on a
    having-clause result by a column says nothing about how that column was
    read, so no reading of it should ever be contradicted from this record.
    """

    query_id: str | None
    in_breakdowns: bool
    in_filters: bool
    in_calculations: bool
    other_filter_columns: frozenset[str]
    calculations: tuple[str, ...]
    """Every calculation on this query, as `OP` or `OP(column)`, the way a
    query names one. Not filtered to the subject's own column: `other_measurement`
    asks whether the query computed a particular calculation anywhere in it.
    Empty for a column named only by `orders` or `havings`, so that reading
    can never be contradicted by a calculation the column had nothing to do
    with."""
    has_granularity: bool


def _effective_spec(args: Mapping[str, object]) -> Mapping[str, object]:
    """`args["query_spec"]` when it is a dict, otherwise `args` itself.

    `run_query` and (per the server's own docs) `list_spans` carry breakdowns,
    filters, calculations, and granularity under `query_spec`; `run_bubbleup`
    has no `query_spec` at all, and a hand-built log entry might not either.
    Falling back to `args` costs nothing when there is no such key to find.
    """
    spec = args.get("query_spec")
    return spec if isinstance(spec, dict) else args


def _spec_filter_columns(spec: Mapping[str, object]) -> set[str]:
    """Every column named by a `filters` clause in `spec`, top-level or nested
    under one of its `calculations`. Distinct from `_collect_exclusions`: this
    counts every filter, not only the ones that exclude a population."""
    out: set[str] = set()
    for clause in spec.get("filters") or []:
        if isinstance(clause, dict) and isinstance(clause.get("column"), str):
            out.add(clause["column"])
    for calc in spec.get("calculations") or []:
        if not isinstance(calc, dict):
            continue
        for clause in calc.get("filters") or []:
            if isinstance(clause, dict) and isinstance(clause.get("column"), str):
                out.add(clause["column"])
    return out


def _bubbleup_filter_columns(args: Mapping[str, object]) -> set[str]:
    """The columns a `run_bubbleup` group selection fixes to one value.

    `{"selection": {"group": {"column": "value"}}}` is an exact-match filter
    on `column`, the same as a `run_query` filter with `op: "="`, so it counts
    the same way for the reading checks.
    """
    selection = args.get("selection")
    group = selection.get("group") if isinstance(selection, dict) else None
    return {str(column) for column in group} if isinstance(group, dict) else set()


def _calc_reprs(calculations: object) -> tuple[str, ...]:
    """Every calculation in a `calculations` list, as `OP` or `OP(column)`."""
    if not isinstance(calculations, list):
        return ()
    reprs = []
    for calc in calculations:
        if not isinstance(calc, dict):
            continue
        op = calc.get("op")
        if not isinstance(op, str):
            continue
        column = calc.get("column")
        reprs.append(f"{op}({column})" if isinstance(column, str) and column else op)
    return tuple(reprs)


def _spec_secondary_columns(spec: Mapping[str, object], key: str) -> set[str]:
    """The columns named by `spec[key]` (`"orders"` or `"havings"`).

    Each entry there is shaped like a calculation (`op`, an optional
    `column`, plus `order` for `orders` or `calculate_op`/`op`/`value` for
    `havings`); an entry that sorts or filters on a calculation such as
    `COUNT` rather than a raw column names no column at all.
    """
    out: set[str] = set()
    for entry in spec.get(key) or []:
        if isinstance(entry, dict) and isinstance(entry.get("column"), str):
            out.add(entry["column"])
    return out


def column_usage(tool_log: Sequence[ToolCall]) -> dict[str, list[Usage]]:
    """Every column a successful `QUERY_TOOLS` call touched, and how.

    Keyed by the column exactly as the call named it (not lowercased; callers
    that need a case-insensitive lookup, such as `partially_checked_issues`,
    do that themselves). A column earns an entry here when a call broke down
    on it, filtered on it, calculated over it, or sorted or filtered its
    calculations by it in `orders` or `havings`; a call that never mentions
    the column contributes nothing for it.

    A call with no `query_id` still counts: `list_spans`, per the server's
    own docs, does not always return one, and `queried_terms` does not
    require one either, so requiring one here made a column `list_spans`
    alone had touched look never queried at all.
    """
    usage: dict[str, list[Usage]] = {}
    for call in tool_log:
        if call.name not in QUERY_TOOLS or call.is_error:
            continue
        spec = _effective_spec(call.args)
        breakdowns = {c for c in (spec.get("breakdowns") or []) if isinstance(c, str)}
        calculations = spec.get("calculations")
        calc_columns = {
            calc["column"]
            for calc in (calculations if isinstance(calculations, list) else [])
            if isinstance(calc, dict) and isinstance(calc.get("column"), str)
        }
        calc_reprs = _calc_reprs(calculations)
        filters = _spec_filter_columns(spec) | _bubbleup_filter_columns(call.args)
        secondary = _spec_secondary_columns(spec, "orders") | _spec_secondary_columns(
            spec, "havings"
        )
        granularity = _parse_number(spec.get("granularity"))
        has_granularity = granularity is not None and granularity > 0

        touched = breakdowns | filters | calc_columns
        for column in touched | secondary:
            other_filters = frozenset(filters - {column, "scenario.run_id"})
            usage.setdefault(column, []).append(
                Usage(
                    query_id=call.query_id,
                    in_breakdowns=column in breakdowns,
                    in_filters=column in filters,
                    in_calculations=column in calc_columns,
                    other_filter_columns=other_filters,
                    calculations=calc_reprs if column in touched else (),
                    has_granularity=has_granularity,
                )
            )
    return usage


_MEASUREMENT_RE = re.compile(r"^(?P<op>[A-Za-z][A-Za-z0-9_]*)(?:\((?P<column>.+)\))?$")


def _parse_measurement(text: str) -> tuple[str, str | None] | None:
    """`("P99", "duration_ms")` from `"P99(duration_ms)"`, `("COUNT", None)`
    from `"COUNT"`, or None when `text` is not one of those two shapes."""
    match = _MEASUREMENT_RE.match(text.strip())
    if match is None:
        return None
    return match.group("op").upper(), match.group("column")


def _calc_repr_matches(calc_repr: str, op: str, column: str | None) -> bool:
    """True when `calc_repr` (one entry of `Usage.calculations`) is the same
    calculation as `op`/`column`, matching the op case-insensitively and the
    column exactly, when one was given."""
    match = _MEASUREMENT_RE.match(calc_repr.strip())
    if match is None:
        return False
    if match.group("op").upper() != op:
        return False
    return column is None or match.group("column") == column


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
    issues += partially_checked_issues(draft.partially_checked, tool_log, terms)
    if require_not_checked:
        issues += not_checked_issues(draft.not_checked, terms)
        issues += not_checked_scope_issues(draft.not_checked)
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
        if not named:
            continue
        if _remainder_names_something_else(entry, named):
            issues.append(
                Issue(
                    "not_checked_false",
                    f"not_checked entry {entry!r} names {named}, which your own queries "
                    "used. The rest of the entry names something else: split it out and "
                    "say, on its own, whether that part was queried too.",
                )
            )
        elif _has_qualifier(entry, named):
            issues.append(
                Issue(
                    "not_checked_false",
                    f"not_checked entry {entry!r} names {named}, which your own queries "
                    "used. The rest of the entry describes a reading of it that was not "
                    "run, and that is not what not_checked is for. Move it to "
                    f"partially_checked, with {named} as the subject, what you ran on it "
                    "in queried_as, and what you did not in not_run.",
                )
            )
        else:
            issues.append(
                Issue(
                    "not_checked_false",
                    f"not_checked entry {entry!r} names {named}, which your own "
                    "queries used. Take it off the list.",
                )
            )
    return issues


def not_checked_scope_issues(not_checked: Sequence[str]) -> list[Issue]:
    """Why the not-checked list is padded. Empty means it is not.

    Separate from `not_checked_issues` on purpose. That function is the
    grader's not-checked component as well as the validator's, and it answers
    one question: is the list true against the log. This one answers a
    different question, whether an entry says anything about the traffic,
    and only the validator asks it. An entry naming only instrumentation or
    trace-structure columns is rejected here, all such entries in one issue,
    so the grader's score for a report is what it was before this check
    existed.
    """
    out_of_scope = [
        entry
        for entry in not_checked
        if (candidates := _candidates(entry))
        and all(column in SYSTEM_COLUMNS for column in candidates)
    ]
    if not out_of_scope:
        return []
    return [
        Issue(
            "not_checked_out_of_scope",
            f"not_checked entries {out_of_scope!r} name only instrumentation or "
            "trace-structure columns (meta.*, telemetry.*, trace.*, span.*, library.*, "
            "scenario.*, or type, parent_name, service.name). Those describe how the "
            "trace was built. This list is for the dimensions and spans that could have "
            "selected the affected rows.",
        )
    ]


_TRIVIAL_REMAINDER = re.compile(r"^[\s\-:,;.'\"`]*$")


def _remainder(entry: str, named: Sequence[str]) -> str:
    """`entry` with every name in `named` stripped out, case-insensitively."""
    remainder = entry
    for name in named:
        remainder = re.sub(re.escape(name), "", remainder, flags=re.IGNORECASE)
    return remainder


def _has_qualifier(entry: str, named: Sequence[str]) -> bool:
    """True when `entry` says more than the bare names in `named`.

    What is left after stripping every name in `named`, along with any
    quoting around it, is checked against nothing but whitespace and light
    punctuation; any word beyond that is a qualifier, the reading the entry
    describes that a bare name would not carry.
    """
    return _TRIVIAL_REMAINDER.match(_remainder(entry, named)) is None


def _remainder_names_something_else(entry: str, named: Sequence[str]) -> bool:
    """True when what is left after `named` is stripped out still names its
    own column: an identifier-shaped token, or something quoted.

    "cart.size, customer.id" with only `cart.size` queried leaves
    "customer.id" behind, and that is a second name rather than a reading of
    `cart.size`. Calling it a qualifier would send the model to
    `partially_checked` to describe a reading of `cart.size` that was never
    claimed; the entry names two different things, and only one of them was
    queried.

    The remainder is computed over `_subject_text(entry)`, the same text
    `_candidates` read `named` from, not the whole entry: "cart.size - broke
    down but did not compare values below 8 on P99(duration_ms)" has a
    subject of just "cart.size", and the explanation naming
    "P99(duration_ms)" is context, the same way `_has_qualifier` already
    reads it as one.
    """
    remainder = _remainder(_subject_text(entry), named)
    return bool(_TOKEN.search(remainder)) or bool(_QUOTED.search(remainder))


def partially_checked_issues(
    entries: Sequence[PartialCheck],
    tool_log_or_usage: Sequence[ToolCall] | Mapping[str, list[Usage]],
    terms: set[str],
) -> list[Issue]:
    """Why the partially-checked list is not truthful. Empty means it is.

    `subject` has to be one of the terms this run's queries actually named,
    the same set `not_checked` is checked against, compared
    case-insensitively; a subject that fails this is `partially_checked_false`
    and the check goes no further for that entry. When the subject does fail
    that check but names a column that terms does contain (a qualified
    subject such as "cart.size < 8", where "cart.size" was in fact queried),
    the message points at that column instead of at `not_checked`: sending it
    there would only bounce back here, since `not_checked_issues` recognizes
    "cart.size" in the entry and tells the model to move it to
    `partially_checked`.

    Passing the membership check is not enough: `subject` also has to be a
    column some query actually broke down, filtered, or calculated over, per
    `column_usage`. A term that entered `terms` only as a filter's value or a
    breakdown's result value (a span name, a region, a customer id) has no
    `column_usage` entry, so no reading of it could ever be checked against
    the log; that is `partially_checked_subject_not_a_column`.

    `reading` is then checked against the log itself: a usage of the subject
    that contradicts the claimed reading is `partially_checked_contradicted`,
    naming the reading, the subject, and the `query_id` that contradicts it.
    `other_measurement` with no `measurement` is `partially_checked_incomplete`
    before that check runs, since there is nothing to check it against.

    `tool_log_or_usage` accepts either the run's tool log or an
    already-built `column_usage` mapping, so a caller that has one on hand
    (as `validate_draft` does not, today, but a future caller might) does not
    pay to rebuild it. `queried_as` carries no check of its own: it is the
    model's own account of what it ran, and there is no further log entry to
    check it against beyond what `reading` already covers.
    """
    usage = (
        tool_log_or_usage
        if isinstance(tool_log_or_usage, Mapping)
        else column_usage(tool_log_or_usage)
    )
    lowered_usage = {column.lower(): usages for column, usages in usage.items()}
    lowered_terms = {term.lower() for term in terms}

    issues: list[Issue] = []
    for entry in entries:
        subject_key = entry.subject.lower()
        if subject_key not in lowered_terms:
            queried = sorted(c for c in _candidates(entry.subject) if c.lower() in lowered_terms)
            if queried:
                issues.append(
                    Issue(
                        "partially_checked_false",
                        f"partially_checked entry names {entry.subject!r}, and no query in "
                        f"this session used that exact string as a column. {queried[0]!r} is "
                        "the column your queries used: name it as subject, and put the rest "
                        f"of {entry.subject!r} in not_run.",
                    )
                )
            else:
                issues.append(
                    Issue(
                        "partially_checked_false",
                        f"partially_checked entry names {entry.subject!r}, and no query in this "
                        "session used that column, span, service, or window. It was never "
                        "queried, so it belongs in not_checked instead.",
                    )
                )
            continue
        if subject_key not in lowered_usage:
            issues.append(
                Issue(
                    "partially_checked_subject_not_a_column",
                    f"partially_checked entry names {entry.subject!r} as its subject, but no "
                    "query in this session broke down, filtered, or calculated over that "
                    "exact string as a column; it only showed up as a value. Name the column "
                    "the value belongs to as the subject instead.",
                )
            )
            continue
        if entry.reading == "other_measurement" and entry.measurement is None:
            issues.append(
                Issue(
                    "partially_checked_incomplete",
                    f"partially_checked entry for {entry.subject!r} has reading "
                    "other_measurement and no measurement. Name the calculation that was not "
                    "run, as OP or OP(column), for example P99(duration_ms).",
                )
            )
            continue
        contradiction = _reading_contradiction(entry, lowered_usage.get(subject_key, []))
        if contradiction is not None:
            issues.append(contradiction)
    return issues


def _reading_contradiction(entry: PartialCheck, usages: Sequence[Usage]) -> Issue | None:
    """The `partially_checked_contradicted` issue for `entry`, or None when
    nothing in `usages` contradicts the reading it claims."""
    if entry.reading == "per_value":
        hit = next((u for u in usages if u.in_breakdowns), None)
        if hit is None:
            return None
        return Issue(
            "partially_checked_contradicted",
            f"partially_checked entry for {entry.subject!r} claims reading per_value: that "
            f"its values were never compared. But query {hit.query_id!r} broke down on it, "
            "which is exactly that comparison. Say what you did not read about it instead, "
            "or drop the entry.",
        )

    if entry.reading == "over_time":
        hit = next(
            (u for u in usages if u.has_granularity and (u.in_breakdowns or u.in_calculations)),
            None,
        )
        if hit is None:
            return None
        return Issue(
            "partially_checked_contradicted",
            f"partially_checked entry for {entry.subject!r} claims reading over_time: that it "
            f"was never read bucket by bucket across the window. But query {hit.query_id!r} "
            "broke down on it, or calculated over it, with a granularity, which is exactly "
            "that reading.",
        )

    if entry.reading == "outside_selection":
        hit = next(
            (
                u
                for u in usages
                if (u.in_breakdowns or u.in_filters or u.in_calculations)
                and not u.other_filter_columns
            ),
            None,
        )
        if hit is None:
            return None
        return Issue(
            "partially_checked_contradicted",
            f"partially_checked entry for {entry.subject!r} claims reading outside_selection: "
            f"that it was only read inside other filters. But query {hit.query_id!r} carried "
            "it with no other filter narrowing the traffic, which is the traffic as a whole.",
        )

    # other_measurement, with a measurement (the incomplete case is rejected
    # by the caller before this runs).
    parsed = _parse_measurement(entry.measurement or "")
    if parsed is None:
        return Issue(
            "partially_checked_incomplete",
            f"partially_checked entry for {entry.subject!r} has reading other_measurement and "
            f"measurement {entry.measurement!r}, which does not parse as a calculation. Name "
            "it as OP or OP(column), for example P99(duration_ms).",
        )
    op, column = parsed
    hit = next(
        (u for u in usages if any(_calc_repr_matches(c, op, column) for c in u.calculations)),
        None,
    )
    if hit is None:
        return None
    return Issue(
        "partially_checked_contradicted",
        f"partially_checked entry for {entry.subject!r} claims reading other_measurement: "
        f"that it was not read with {entry.measurement}. But query {hit.query_id!r} computed "
        "exactly that calculation.",
    )


def _subject_text(entry: str) -> str:
    """The part of `entry` that names the claim: the text before a " - " or
    ": " separator, when there is one, otherwise the whole entry.

    Shared by `_candidates` and `_remainder_names_something_else`, so both
    agree on what counts as the claim and what counts as explanation. A live
    regression had them disagree: `_candidates` read only the subject before
    the separator, while the other read the whole entry, so an explanation
    that happened to mention a second identifier-shaped word (a measurement
    such as `P99(duration_ms)`) was read as a second, unqueried name.
    """
    subject = _SUBJECT.match(entry)
    return subject.group(1) if subject and subject.group(1).strip() else entry


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
    text = _subject_text(entry)
    found = set(_TOKEN.findall(text))
    found |= set(_QUOTED.findall(text))
    stripped = text.strip().strip("`\"'").strip()
    if stripped and " " not in stripped:
        found.add(stripped)
    return found


def rejection_message(issues: Sequence[Issue], terms: Iterable[str]) -> str:
    """The text handed back to the model after a rejected report.

    `terms` is the same set `not_checked` and `partially_checked` are checked
    against, so the model can fix either list by reading the line at the end
    rather than by recalling what it queried.
    """
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
        "",
    ]
    sorted_terms = sorted(terms)
    if sorted_terms:
        lines.append(f"Columns and values your queries used: {', '.join(sorted_terms)}")
    else:
        lines.append("Columns and values your queries used: none yet.")
    return "\n".join(lines)
