"""Check a submitted report against the log of what the run actually did.

The two rules that are the point of this project are enforced here, in code,
against the tool log. Nothing in this module trusts a field the model wrote.

  Receipts. Every hypothesis carries at least one `run_query` that was made in
  this run, and, when negation is required, a `WHERE NOT` query that was made
  too. Every `query_id` in the report has to appear in the log.

  Not checked. Every entry in `not_checked` names something that is absent from
  the arguments of the queries that were run. An entry naming a column the run
  broke down on is a false claim about the run's own coverage, which is worse
  than an empty list.

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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from agent.report import (
    PRIMARY_EVIDENCE_TOOL,
    Evidence,
    ReportDraft,
    ToolCall,
)

# Tools whose arguments name columns and values, which is what "was queried"
# means for the not-checked list. Discovery calls such as `find_columns` are
# not in here: looking a column up is not measuring it.
QUERY_TOOLS: frozenset[str] = frozenset({"run_query", "run_bubbleup", "list_spans"})

# Argument keys that carry a column name or a filter value.
_COLUMN_KEYS: frozenset[str] = frozenset({"column", "columns", "breakdowns", "group_by"})
_VALUE_KEYS: frozenset[str] = frozenset({"value", "values"})

# Identifier-shaped words inside a free-text `not_checked` entry. A match has
# to carry a dot, an underscore, or a hyphen, which is what separates
# `payments.charge`, `duration_ms`, and `us-west-2` from ordinary prose. A
# live run showed why: an entry reading "did not examine specific error
# message text" was rejected for naming the `error` column, which it was not.
# The cost of the rule is that an entry naming a single-word column, such as
# `error` on its own, gets through. That is the cheaper mistake: a false
# rejection burns a turn and reads as the validator being wrong, and the
# single-word columns in this dataset are the ones a reader is least likely to
# care about.
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[.\-][A-Za-z0-9_]+)+|[A-Za-z]+_[A-Za-z0-9_]+")


@dataclass(frozen=True)
class Issue:
    """One reason a report was rejected."""

    code: str
    """`unsupported`, `partial`, `not_checked_empty`, or `not_checked_false`."""

    message: str
    """What is wrong, addressed to the model, so it can be handed straight back."""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def query_ids(tool_log: Sequence[ToolCall], *, tools: Iterable[str] | None = None) -> set[str]:
    """Every `query_id` the run collected, optionally from named tools only."""
    allowed = None if tools is None else set(tools)
    return {
        call.query_id
        for call in tool_log
        if call.query_id and (allowed is None or call.name in allowed)
    }


def queried_terms(tool_log: Sequence[ToolCall], *, run_id: str | None = None) -> set[str]:
    """The columns and values the run's queries named.

    The run id is dropped: it is on every query by construction, so it is not
    evidence that anything was investigated.
    """
    terms: set[str] = set()
    for call in tool_log:
        if call.name in QUERY_TOOLS:
            _collect(call.args, None, terms)
    terms.discard("scenario.run_id")
    if run_id:
        terms.discard(run_id)
    return terms


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
    known = query_ids(tool_log)
    from_queries = query_ids(tool_log, tools=[PRIMARY_EVIDENCE_TOOL])

    issues += _check_hypotheses(draft, known, from_queries, require_negation=require_negation)
    issues += _check_baseline(draft, known, from_queries)
    if require_not_checked:
        issues += _check_not_checked(draft, tool_log, run_id=run_id)
    return issues


def _check_hypotheses(
    draft: ReportDraft,
    known: set[str],
    from_queries: set[str],
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

    for index, hypothesis in enumerate(draft.hypotheses):
        label = f"hypotheses[{index}] ({hypothesis.claim[:60]!r})"

        if not hypothesis.evidence:
            issues.append(
                Issue(
                    "unsupported",
                    f"{label} carries no evidence. Every hypothesis needs at least one "
                    "run_query from this session whose rows support it.",
                )
            )
        else:
            issues += _check_ids(hypothesis.evidence, known, f"{label} evidence")
            if not any(item.query_id in from_queries for item in hypothesis.evidence):
                issues.append(
                    Issue(
                        "partial",
                        f"{label} cites no run_query. BubbleUp and traces point at a cause; "
                        "rows are what carry it. Run the query and cite it.",
                    )
                )

        if hypothesis.negation is None:
            if require_negation:
                issues.append(
                    Issue(
                        "partial",
                        f"{label} has no negation. Run the same measurement WHERE NOT the "
                        "dimensions in dims, over the same window, and cite that query_id.",
                    )
                )
        else:
            issues += _check_ids([hypothesis.negation], known, f"{label} negation")

    return issues


def _check_baseline(draft: ReportDraft, known: set[str], from_queries: set[str]) -> list[Issue]:
    """A report that says nothing happened has to show the window that was flat."""
    issues = _check_ids(draft.baseline_evidence, known, "baseline_evidence")
    if draft.incident_present:
        return issues
    if not draft.baseline_evidence:
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
                "baseline_evidence cites no run_query. A quiet window is a claim about rows.",
            )
        )
    return issues


def _check_ids(evidence: Sequence[Evidence], known: set[str], label: str) -> list[Issue]:
    return [
        Issue(
            "unsupported",
            f"{label} cites query_id {item.query_id!r}, which no tool call in this session "
            "returned. Cite an identifier from a result you received.",
        )
        for item in evidence
        if item.query_id not in known
    ]


def _check_not_checked(
    draft: ReportDraft, tool_log: Sequence[ToolCall], *, run_id: str | None
) -> list[Issue]:
    if not draft.not_checked:
        return [
            Issue(
                "not_checked_empty",
                "not_checked is empty. Name the dimensions, spans, or time windows that were "
                "in scope and that you did not query.",
            )
        ]

    terms = queried_terms(tool_log, run_id=run_id)
    issues: list[Issue] = []
    for entry in draft.not_checked:
        named = {token for token in _TOKEN.findall(entry) if token in terms}
        if named:
            issues.append(
                Issue(
                    "not_checked_false",
                    f"not_checked entry {entry!r} names {sorted(named)}, which your own "
                    "queries used. Take it off the list.",
                )
            )
    return issues


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
