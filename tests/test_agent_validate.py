"""Tests for agent/validate.py: the receipts rule and the not-checked list.

Every test here builds a synthetic tool log by hand. That is the point: the
validator's job is to disagree with the model when the model's claims and the
log disagree, so the log has to be something a test can lie about.
"""

from __future__ import annotations

from typing import Any

from agent.report import Evidence, Hypothesis, ReportDraft, ToolCall
from agent.validate import (
    queried_terms,
    query_ids,
    rejection_message,
    validate_draft,
)

RUN_ID = "run-abc123"


def query_call(
    query_id: str,
    *,
    name: str = "run_query",
    breakdowns: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    calculations: list[dict[str, Any]] | None = None,
) -> ToolCall:
    """One `run_query` in the log, with the shape the hosted MCP takes."""
    spec: dict[str, Any] = {
        "calculations": calculations or [{"op": "COUNT"}],
        "filters": [{"column": "scenario.run_id", "op": "=", "value": RUN_ID}, *(filters or [])],
    }
    if breakdowns:
        spec["breakdowns"] = breakdowns
    return ToolCall(
        name=name,
        args={"dataset_slug": "receipts-shop", "query_spec": spec},
        query_id=query_id,
        permalink=f"https://ui.honeycomb.io/x/result/{query_id}",
    )


def hypothesis(**overrides: Any) -> Hypothesis:
    base: dict[str, Any] = {
        "claim": "one population got slow",
        "dims": {"deployment.version": "9.9.9"},
        "slow_or_failing_span": "some.span",
        "confidence": "high",
        "evidence": [Evidence(query_id="Q1", summary="P99 180ms to 1100ms")],
        "negation": Evidence(query_id="Q2", summary="P99 flat outside"),
    }
    base.update(overrides)
    return Hypothesis.model_validate(base)


def draft(**overrides: Any) -> ReportDraft:
    base: dict[str, Any] = {
        "incident_present": True,
        "hypotheses": [hypothesis()],
        "affected_population": "12%",
        "not_checked": ["customer.id", "http.route"],
    }
    base.update(overrides)
    return ReportDraft.model_validate(base)


def good_log() -> list[ToolCall]:
    return [
        ToolCall(name="get_workspace_context", args={}),
        query_call("Q1", breakdowns=["deployment.version"]),
        query_call("Q2", filters=[{"column": "deployment.version", "op": "!=", "value": "9.9.9"}]),
    ]


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_report_that_cites_queries_it_ran_is_accepted() -> None:
    assert validate_draft(draft(), good_log(), run_id=RUN_ID) == []


def test_query_ids_can_be_filtered_to_one_tool() -> None:
    log = [*good_log(), ToolCall(name="run_bubbleup", args={}, query_id="B1")]
    assert query_ids(log) == {"Q1", "Q2", "B1"}
    assert query_ids(log, tools=["run_query"]) == {"Q1", "Q2"}


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------


def test_a_hypothesis_with_no_evidence_is_rejected() -> None:
    issues = validate_draft(draft(hypotheses=[hypothesis(evidence=[])]), good_log(), run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["unsupported"]
    assert "carries no evidence" in issues[0].message


def test_a_query_id_that_was_never_run_is_rejected() -> None:
    made_up = hypothesis(evidence=[Evidence(query_id="Q-invented", summary="trust me")])
    issues = validate_draft(draft(hypotheses=[made_up]), good_log(), run_id=RUN_ID)
    codes = [issue.code for issue in issues]
    assert "unsupported" in codes
    assert any("Q-invented" in issue.message for issue in issues)


def test_evidence_from_bubbleup_alone_does_not_carry_a_hypothesis() -> None:
    """BubbleUp ranks; rows are what a claim rests on."""
    log = [ToolCall(name="run_bubbleup", args={}, query_id="B1"), *good_log()]
    only_bubbleup = hypothesis(evidence=[Evidence(query_id="B1", summary="version ranked first")])
    issues = validate_draft(draft(hypotheses=[only_bubbleup]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]
    assert "cites no run_query" in issues[0].message


def test_a_missing_negation_is_rejected() -> None:
    issues = validate_draft(
        draft(hypotheses=[hypothesis(negation=None)]), good_log(), run_id=RUN_ID
    )
    assert [issue.code for issue in issues] == ["partial"]
    assert "no negation" in issues[0].message


def test_a_negation_citing_an_unrun_query_is_rejected() -> None:
    bad = hypothesis(negation=Evidence(query_id="Q-nope", summary="I meant to run this"))
    issues = validate_draft(draft(hypotheses=[bad]), good_log(), run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["unsupported"]


def test_require_negation_off_accepts_a_hypothesis_without_one() -> None:
    """The R10 ablation removes the rule rather than softening it."""
    issues = validate_draft(
        draft(hypotheses=[hypothesis(negation=None)]),
        good_log(),
        run_id=RUN_ID,
        require_negation=False,
    )
    assert issues == []


def test_an_incident_with_no_hypotheses_is_rejected() -> None:
    issues = validate_draft(draft(hypotheses=[]), good_log(), run_id=RUN_ID)
    assert any("no hypotheses" in issue.message for issue in issues)


# --------------------------------------------------------------------------
# The no-incident case
# --------------------------------------------------------------------------


def test_a_quiet_window_still_needs_a_query_behind_it() -> None:
    quiet = ReportDraft(incident_present=False, not_checked=["customer.id"])
    issues = validate_draft(quiet, good_log(), run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["unsupported"]
    assert "baseline_evidence is empty" in issues[0].message


def test_a_quiet_window_with_a_flat_query_is_accepted() -> None:
    quiet = ReportDraft(
        incident_present=False,
        not_checked=["customer.id"],
        baseline_evidence=[Evidence(query_id="Q1", summary="P99 316ms then 329ms, flat")],
    )
    assert validate_draft(quiet, good_log(), run_id=RUN_ID) == []


# --------------------------------------------------------------------------
# The not-checked list
# --------------------------------------------------------------------------


def test_an_empty_not_checked_list_is_rejected() -> None:
    issues = validate_draft(draft(not_checked=[]), good_log(), run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_empty"]


def test_a_not_checked_entry_that_was_queried_is_rejected() -> None:
    log = good_log()
    draft_ = draft(not_checked=["deployment.version was never broken down on"])
    issues = validate_draft(draft_, log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]
    assert "deployment.version" in issues[0].message


def test_a_not_checked_entry_that_names_a_filter_value_is_rejected() -> None:
    log = [
        *good_log(),
        query_call("Q3", filters=[{"column": "name", "op": "=", "value": "payments.charge"}]),
    ]
    draft_ = draft(not_checked=["the payments.charge span was never looked at"])
    issues = validate_draft(draft_, log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]


def test_the_run_id_filter_does_not_make_every_entry_false() -> None:
    """Every query carries the run id, so it cannot count as coverage."""
    terms = queried_terms(good_log(), run_id=RUN_ID)
    assert RUN_ID not in terms
    assert "scenario.run_id" not in terms
    assert "deployment.version" in terms


def test_discovery_calls_do_not_count_as_having_queried_a_column() -> None:
    log = [
        ToolCall(name="find_columns", args={"query": "cart.size"}),
        ToolCall(name="get_dataset_columns", args={"column": "cart.size"}),
        *good_log(),
    ]
    assert "cart.size" not in queried_terms(log, run_id=RUN_ID)
    assert validate_draft(draft(not_checked=["cart.size"]), log, run_id=RUN_ID) == []


def test_a_bubbleup_group_selection_counts_as_a_query() -> None:
    log = [
        *good_log(),
        ToolCall(
            name="run_bubbleup",
            args={"query_pk": "Q1", "selection": {"type": "group", "group": {"http.route": "/x"}}},
            query_id="B1",
        ),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    assert {"http.route", "/x"} <= terms


def test_require_not_checked_off_skips_the_list_entirely() -> None:
    issues = validate_draft(
        draft(not_checked=[]), good_log(), run_id=RUN_ID, require_not_checked=False
    )
    assert issues == []


# --------------------------------------------------------------------------
# What the model is handed back
# --------------------------------------------------------------------------


def test_the_rejection_message_names_every_issue() -> None:
    issues = validate_draft(
        draft(hypotheses=[hypothesis(evidence=[], negation=None)], not_checked=[]),
        good_log(),
        run_id=RUN_ID,
    )
    message = rejection_message(issues)
    assert len(issues) == 3
    for issue in issues:
        assert issue.message in message
    assert "call submit_report again" in message
