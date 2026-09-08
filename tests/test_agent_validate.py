"""Tests for agent/validate.py: the receipts rule and the not-checked list.

Every test here builds a synthetic tool log by hand. That is the point: the
validator's job is to disagree with the model when the model's claims and the
log disagree, so the log has to be something a test can lie about.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent.report import Evidence, Hypothesis, PartialCheck, ReportDraft, ToolCall
from agent.validate import (
    SYSTEM_COLUMNS,
    _parse_time_bound,
    column_usage,
    excluded_columns,
    partially_checked_issues,
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
    from_: str | None = None,
    to: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> ToolCall:
    """One `run_query` in the log, with the shape the hosted MCP takes.

    `from_`/`to` and `start_time`/`end_time` are the two spellings a query's
    own time range comes in (`agent/format.py:401-408`); a test that wants a
    range on the spec passes one pair or the other, never both.
    """
    spec: dict[str, Any] = {
        "calculations": calculations or [{"op": "COUNT"}],
        "filters": [{"column": "scenario.run_id", "op": "=", "value": RUN_ID}, *(filters or [])],
    }
    if breakdowns:
        spec["breakdowns"] = breakdowns
    if from_ is not None:
        spec["from"] = from_
    if to is not None:
        spec["to"] = to
    if start_time is not None:
        spec["start_time"] = start_time
    if end_time is not None:
        spec["end_time"] = end_time
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
        "baseline_evidence": [Evidence(query_id="Q3", summary="flat before onset")],
    }
    base.update(overrides)
    return ReportDraft.model_validate(base)


def good_log() -> list[ToolCall]:
    return [
        ToolCall(name="get_workspace_context", args={}),
        query_call("Q1", breakdowns=["deployment.version"]),
        query_call("Q2", filters=[{"column": "deployment.version", "op": "!=", "value": "9.9.9"}]),
        baseline_call("Q3"),
    ]


def baseline_call(query_id: str = "Q3") -> ToolCall:
    """A plain measurement over the window, which is what a baseline cites.

    No breakdowns, so adding it to a log does not change what counts as
    covered for the not-checked list.
    """
    return query_call(query_id, calculations=[{"op": "P99", "column": "duration_ms"}])


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_report_that_cites_queries_it_ran_is_accepted() -> None:
    assert validate_draft(draft(), good_log(), run_id=RUN_ID) == []


def test_query_ids_can_be_filtered_to_one_tool() -> None:
    log = [*good_log(), ToolCall(name="run_bubbleup", args={}, query_id="B1")]
    assert query_ids(log) == {"Q1", "Q2", "Q3", "B1"}
    assert query_ids(log, tools=["run_query"]) == {"Q1", "Q2", "Q3"}


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


def test_an_ordinary_english_word_is_not_read_as_a_column_name() -> None:
    """A live run was rejected for the word "error" inside "error message text"."""
    log = [
        *good_log(),
        query_call("Q4", filters=[{"column": "error", "op": "=", "value": True}]),
        baseline_call(),
    ]
    entry = "status_message content, did not examine specific error message text"
    assert validate_draft(draft(not_checked=[entry]), log, run_id=RUN_ID) == []


def test_a_dotted_or_underscored_name_is_still_caught() -> None:
    log = [*good_log(), query_call("Q4", calculations=[{"op": "P99", "column": "duration_ms"}])]
    for entry in ("deployment.version was left alone", "duration_ms was never measured"):
        issues = validate_draft(draft(not_checked=[entry]), log, run_id=RUN_ID)
        assert [issue.code for issue in issues] == ["not_checked_false"], entry


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
        baseline_call(),
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
        baseline_call(),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    assert {"http.route", "/x"} <= terms


def test_require_not_checked_off_skips_the_list_entirely() -> None:
    issues = validate_draft(
        draft(not_checked=[]), good_log(), run_id=RUN_ID, require_not_checked=False
    )
    assert issues == []


# --------------------------------------------------------------------------
# A run_query breakdown's result_values fold into queried_terms too: EDW-1367
# said they would and they never did.
# --------------------------------------------------------------------------


def test_a_result_value_folds_into_queried_terms() -> None:
    log = [
        ToolCall(
            name="run_query",
            args={"query_spec": {"breakdowns": ["deployment.version"]}},
            query_id="Q1",
            result_values={"deployment.version": ["9.9.9"]},
        )
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    assert "9.9.9" in terms


def test_a_result_value_makes_a_not_checked_entry_false() -> None:
    from agent.validate import not_checked_issues

    log = [
        ToolCall(
            name="run_query",
            args={"query_spec": {"breakdowns": ["deployment.version"]}},
            query_id="Q1",
            result_values={"deployment.version": ["9.9.9"]},
        )
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    issues = not_checked_issues(["9.9.9"], terms)
    assert [issue.code for issue in issues] == ["not_checked_false"]


def test_a_result_value_passes_the_partially_checked_membership_check() -> None:
    """Folded into terms, a result value passes partially_checked_issues's
    membership check the same as any other queried term. `usage` is built by
    hand here rather than through column_usage (which never keys by value),
    so this isolates the membership check from the separate column_usage
    check EDW-1367's own after-pass added."""
    from agent.validate import Usage

    log = [
        ToolCall(
            name="run_query",
            args={"query_spec": {"breakdowns": ["deployment.version"]}},
            query_id="Q1",
            result_values={"deployment.version": ["9.9.9"]},
        )
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    usage = {
        "9.9.9": [
            Usage(
                query_id="Q1",
                in_breakdowns=True,
                in_filters=False,
                in_calculations=False,
                other_filter_columns=frozenset(),
                calculations=(),
                has_granularity=False,
            )
        ]
    }
    check = partial_check(subject="9.9.9", reading="over_time")
    assert partially_checked_issues([check], usage, terms) == []


def test_a_value_past_the_shown_row_cap_does_not_fold_into_queried_terms() -> None:
    """breakdown_values (agent/format.py) is capped to MAX_QUERY_ROWS, the same
    rows the rendered table shows, so agent/loop.py never records a value past
    that cap into result_values. A value the model was never shown must not
    make a not_checked entry naming it false."""
    log = [
        ToolCall(
            name="run_query",
            args={"query_spec": {"breakdowns": ["customer.id"]}},
            query_id="Q1",
            result_values={"customer.id": ["customer-0000"]},  # what the loop would have kept
        )
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    assert "customer-9999" not in terms
    from agent.validate import not_checked_issues

    assert not_checked_issues(["customer-9999"], terms) == []


# --------------------------------------------------------------------------
# not_checked: system columns are out of scope
# --------------------------------------------------------------------------


def test_system_column_entries_are_rejected_together_in_one_issue() -> None:
    entries = ["trace.span_id", "telemetry.sdk.name", "span.num_events was never read"]
    issues = validate_draft(draft(not_checked=entries), good_log(), run_id=RUN_ID)
    out_of_scope = [issue for issue in issues if issue.code == "not_checked_out_of_scope"]
    assert len(out_of_scope) == 1
    for entry in entries:
        assert entry in out_of_scope[0].message
    assert not any(issue.code == "not_checked_false" for issue in issues)


def test_a_mixed_entry_is_not_out_of_scope() -> None:
    """An entry naming a system column alongside an in-scope one is a real claim,
    even when the in-scope column turns out to have been queried, which is a
    different rejection (not_checked_false) than being out of scope."""
    issues = validate_draft(
        draft(not_checked=["trace.span_id and deployment.version were both left unread"]),
        good_log(),
        run_id=RUN_ID,
    )
    assert not any(issue.code == "not_checked_out_of_scope" for issue in issues)
    assert any(issue.code == "not_checked_false" for issue in issues)


def test_status_code_and_error_stay_in_scope() -> None:
    """A fault shows up in these columns, so they are not system columns."""
    issues = validate_draft(
        draft(not_checked=["status_code", "error", "exception.type"]), good_log(), run_id=RUN_ID
    )
    assert not any(issue.code == "not_checked_out_of_scope" for issue in issues)


def test_not_checked_issues_alone_never_raises_out_of_scope() -> None:
    """The grader calls `not_checked_issues` directly; scope is the validator's question only."""
    from agent.validate import not_checked_issues, not_checked_scope_issues

    entries = ["trace.span_id", "telemetry.sdk.name"]
    assert not_checked_issues(entries, {"cart.size"}) == []
    assert [i.code for i in not_checked_scope_issues(entries)] == ["not_checked_out_of_scope"]


def test_the_out_of_scope_check_is_skipped_when_not_checked_is_not_required() -> None:
    issues = validate_draft(
        draft(not_checked=["trace.span_id"]), good_log(), run_id=RUN_ID, require_not_checked=False
    )
    assert not any(issue.code == "not_checked_out_of_scope" for issue in issues)


# --------------------------------------------------------------------------
# The partially-checked list
# --------------------------------------------------------------------------


def partial_check(**overrides: Any) -> PartialCheck:
    base: dict[str, Any] = {
        "subject": "deployment.version",
        "queried_as": "broke down P99 duration_ms by deployment.version",
        "reading": "over_time",
        "not_run": "did not read it bucket by bucket across the window",
    }
    base.update(overrides)
    return PartialCheck.model_validate(base)


def test_a_queried_subject_is_accepted() -> None:
    log = good_log()
    assert partially_checked_issues([partial_check()], log, queried_terms(log, run_id=RUN_ID)) == []


def test_a_queried_subject_is_accepted_case_insensitively() -> None:
    log = good_log()
    terms = queried_terms(log, run_id=RUN_ID)
    issues = partially_checked_issues([partial_check(subject="Deployment.Version")], log, terms)
    assert issues == []


def test_an_unqueried_subject_is_rejected() -> None:
    log = good_log()
    terms = queried_terms(log, run_id=RUN_ID)
    issues = partially_checked_issues([partial_check(subject="never.queried")], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_false"]
    assert "never.queried" in issues[0].message
    assert "not_checked" in issues[0].message


def test_partially_checked_runs_whether_or_not_not_checked_is_required() -> None:
    """This check is not the R10 ablation knob; it always runs."""
    bad = draft(partially_checked=[partial_check(subject="never.queried")])
    for require_not_checked in (True, False):
        issues = validate_draft(
            bad, good_log(), run_id=RUN_ID, require_not_checked=require_not_checked
        )
        assert any(issue.code == "partially_checked_false" for issue in issues)


def test_queried_as_is_free_text() -> None:
    """queried_as carries no check of its own: reading is what is checked."""
    log = good_log()
    terms = queried_terms(log, run_id=RUN_ID)
    odd = partial_check(queried_as="whatever I feel like")
    assert partially_checked_issues([odd], log, terms) == []


def test_partially_checked_issues_accepts_a_precomputed_usage_mapping() -> None:
    """`tool_log_or_usage` can be the tool log or `column_usage`'s own output."""
    log = good_log()
    terms = queried_terms(log, run_id=RUN_ID)
    usage = column_usage(log)
    assert partially_checked_issues([partial_check()], usage, terms) == []
    assert partially_checked_issues([partial_check()], log, terms) == []


# --------------------------------------------------------------------------
# partially_checked: each reading, accepted when nothing contradicts it and
# rejected by the usage that does
# --------------------------------------------------------------------------


def test_per_value_is_accepted_when_never_broken_down_on() -> None:
    log = [
        query_call("Q1", filters=[{"column": "cart.size", "op": ">=", "value": 8}]),
        baseline_call(),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="cart.size", reading="per_value")
    assert partially_checked_issues([check], log, terms) == []


def test_per_value_is_contradicted_by_a_breakdown() -> None:
    log = [query_call("Q1", breakdowns=["deployment.version"]), baseline_call()]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="deployment.version", reading="per_value")
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_contradicted"]
    assert "per_value" in issues[0].message
    assert "deployment.version" in issues[0].message
    assert "Q1" in issues[0].message


def test_over_time_is_accepted_without_a_granularity() -> None:
    log = [query_call("Q1", breakdowns=["deployment.version"]), baseline_call()]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="deployment.version", reading="over_time")
    assert partially_checked_issues([check], log, terms) == []


def test_over_time_is_not_contradicted_by_a_filter_to_one_value_with_a_granularity() -> None:
    """A query filtered to name = payments.charge with a granularity reads that one
    span over time. It does not read the `name` column over time, so an entry saying
    `name` was never read bucket by bucket stands. Two cells of the EDW-1367 after-pass
    lost 0.25 each to the opposite reading of this case."""
    filtered = ToolCall(
        name="run_query",
        args={
            "dataset_slug": "receipts-shop",
            "query_spec": {
                "calculations": [{"op": "COUNT"}],
                "filters": [
                    {"column": "scenario.run_id", "op": "=", "value": RUN_ID},
                    {"column": "name", "op": "=", "value": "payments.charge"},
                ],
                "granularity": 60,
            },
        },
        query_id="Q1",
    )
    log = [filtered, baseline_call()]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="name", reading="over_time")
    assert partially_checked_issues([check], log, terms) == []


def test_over_time_is_contradicted_by_a_granularity() -> None:
    granular = ToolCall(
        name="run_query",
        args={
            "dataset_slug": "receipts-shop",
            "query_spec": {
                "calculations": [{"op": "COUNT"}],
                "filters": [{"column": "scenario.run_id", "op": "=", "value": RUN_ID}],
                "breakdowns": ["deployment.version"],
                "granularity": 120,
            },
        },
        query_id="Q1",
    )
    log = [granular, baseline_call()]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="deployment.version", reading="over_time")
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_contradicted"]
    assert "over_time" in issues[0].message
    assert "Q1" in issues[0].message


def test_over_time_is_contradicted_by_a_granularity_on_a_calculation_alone() -> None:
    """A P99(duration_ms) with a granularity and no breakdown reads duration_ms
    bucket by bucket just as much as a breakdown with a granularity does."""
    granular = ToolCall(
        name="run_query",
        args={
            "dataset_slug": "receipts-shop",
            "query_spec": {
                "calculations": [{"op": "P99", "column": "duration_ms"}],
                "filters": [{"column": "scenario.run_id", "op": "=", "value": RUN_ID}],
                "granularity": 60,
            },
        },
        query_id="Q1",
    )
    log = [granular, baseline_call()]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="duration_ms", reading="over_time")
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_contradicted"]


def test_outside_selection_is_accepted_when_only_ever_filtered_alongside_another_column() -> None:
    log = [
        query_call(
            "Q1",
            breakdowns=["deployment.version"],
            filters=[{"column": "cloud.region", "op": "=", "value": "us-west-2"}],
        ),
        baseline_call(),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="deployment.version", reading="outside_selection")
    assert partially_checked_issues([check], log, terms) == []


def test_outside_selection_is_contradicted_by_a_query_with_no_other_filter() -> None:
    log = [query_call("Q1", breakdowns=["deployment.version"]), baseline_call()]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="deployment.version", reading="outside_selection")
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_contradicted"]
    assert "outside_selection" in issues[0].message
    assert "Q1" in issues[0].message


def test_outside_selection_is_contradicted_by_a_calculation_with_no_other_filter() -> None:
    """A calculation over the column reads the traffic as a whole just as much
    as a breakdown or a plain filter does."""
    log = [query_call("Q1", calculations=[{"op": "P99", "column": "duration_ms"}]), baseline_call()]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="duration_ms", reading="outside_selection")
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_contradicted"]


def test_other_measurement_is_accepted_when_the_named_calculation_never_ran() -> None:
    """No query anywhere in this log computes P99(duration_ms): unlike
    baseline_call(), this one only counts."""
    log = [query_call("Q1", calculations=[{"op": "COUNT"}], breakdowns=["duration_ms"])]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(
        subject="duration_ms", reading="other_measurement", measurement="P99(duration_ms)"
    )
    assert partially_checked_issues([check], log, terms) == []


def test_other_measurement_is_contradicted_when_the_calculation_already_ran() -> None:
    log = [
        query_call(
            "Q1",
            calculations=[{"op": "P99", "column": "duration_ms"}],
            breakdowns=["duration_ms"],
        ),
        baseline_call(),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(
        subject="duration_ms", reading="other_measurement", measurement="P99(duration_ms)"
    )
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_contradicted"]
    assert "other_measurement" in issues[0].message
    assert "Q1" in issues[0].message


def test_other_measurement_without_a_column_matches_op_alone() -> None:
    log = [
        query_call("Q1", calculations=[{"op": "COUNT"}], breakdowns=["cart.size"]),
        baseline_call(),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="cart.size", reading="other_measurement", measurement="COUNT")
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_contradicted"]


def test_other_measurement_with_no_measurement_is_incomplete() -> None:
    log = [query_call("Q1", breakdowns=["cart.size"]), baseline_call()]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="cart.size", reading="other_measurement", measurement=None)
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_incomplete"]


def test_other_measurement_with_an_unparseable_measurement_is_incomplete() -> None:
    """ "p99 duration_ms" is not OP or OP(column): this used to make
    _reading_contradiction return None silently, which accepted the entry."""
    log = [query_call("Q1", breakdowns=["cart.size"]), baseline_call()]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(
        subject="cart.size", reading="other_measurement", measurement="p99 duration_ms"
    )
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_incomplete"]
    assert "OP(column)" in issues[0].message


def test_a_subject_never_queried_is_still_rejected_before_any_reading_check() -> None:
    log = good_log()
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="never.queried", reading="per_value")
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_false"]


# --------------------------------------------------------------------------
# partially_checked_subject_not_a_column: a value or a span name is in terms
# but was never itself broken down, filtered, or calculated over
# --------------------------------------------------------------------------


def test_a_filter_value_is_rejected_as_not_a_column() -> None:
    """ "payments.charge" is in terms because a filter used it as a value, but
    no query ever broke down, filtered, or calculated over a column called
    that, so no reading of it could ever be checked against the log."""
    log = [
        query_call("Q1", filters=[{"column": "name", "op": "=", "value": "payments.charge"}]),
        baseline_call(),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="payments.charge", reading="per_value")
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_subject_not_a_column"]
    assert "payments.charge" in issues[0].message


def test_a_real_column_still_passes_the_subject_not_a_column_check() -> None:
    """The column that carried the value ("name") is unaffected."""
    log = [
        query_call("Q1", filters=[{"column": "name", "op": "=", "value": "payments.charge"}]),
        baseline_call(),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="name", reading="outside_selection")
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_contradicted"]


# --------------------------------------------------------------------------
# The ping-pong: a qualified subject such as "cart.size < 8" must not be sent
# back to not_checked, which would only send it back here again.
# --------------------------------------------------------------------------


def test_a_qualified_subject_is_pointed_at_its_column_not_not_checked() -> None:
    log = [
        query_call("Q1", filters=[{"column": "cart.size", "op": ">=", "value": 8}]),
        baseline_call(),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="cart.size < 8", reading="per_value")
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_false"]
    assert "not_checked" not in issues[0].message
    assert "cart.size" in issues[0].message


def test_a_subject_with_no_queried_column_at_all_still_points_at_not_checked() -> None:
    """The ping-pong fix only changes the message when a candidate inside the
    subject was in fact queried; an entirely unqueried subject still points
    at not_checked, which is the correct move for it."""
    log = good_log()
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="cart.size < 8", reading="per_value")
    issues = partially_checked_issues([check], log, terms)
    assert [issue.code for issue in issues] == ["partially_checked_false"]
    assert "not_checked" in issues[0].message


# --------------------------------------------------------------------------
# not_checked_false: a bare name vs. an entry with a qualifier
# --------------------------------------------------------------------------


def test_a_bare_name_entry_is_told_to_take_it_off_the_list() -> None:
    issues = validate_draft(draft(not_checked=["deployment.version"]), good_log(), run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]
    assert "take it off the list" in issues[0].message.lower()
    assert "partially_checked" not in issues[0].message


def test_a_qualified_entry_is_pointed_at_partially_checked() -> None:
    entry = "deployment.version was never broken down on"
    issues = validate_draft(draft(not_checked=[entry]), good_log(), run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]
    assert "partially_checked" in issues[0].message
    assert "deployment.version" in issues[0].message


def test_a_quoted_bare_name_is_still_told_to_take_it_off_the_list() -> None:
    log = [
        query_call("Q1", breakdowns=["error"]),
        query_call("Q2", filters=[{"column": "deployment.version", "op": "!=", "value": "9.9.9"}]),
        baseline_call(),
    ]
    issues = validate_draft(draft(not_checked=["`error`"]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]
    assert "partially_checked" not in issues[0].message


def test_a_second_unqueried_name_in_the_entry_is_not_read_as_a_qualifier() -> None:
    """ "cart.size, customer.id" with only cart.size queried is not a claim
    that cart.size was read some particular way: customer.id is a second
    name, and it was never queried. This must not be pointed at
    partially_checked, which has no subject to put there."""
    log = [*good_log(), query_call("Q4", breakdowns=["cart.size"])]
    issues = validate_draft(draft(not_checked=["cart.size, customer.id"]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]
    assert "cart.size" in issues[0].message
    assert "customer.id" in issues[0].message
    assert "partially_checked" not in issues[0].message


def test_a_dash_separated_qualifier_is_still_pointed_at_partially_checked() -> None:
    """A regression from the fix above: "cart.size - explanation" has a
    subject of just "cart.size" (per _candidates's own separator rule), and
    the explanation naming a measurement like P99(duration_ms) is context,
    not a second unqueried name. _remainder_names_something_else has to read
    the same subject text _candidates does, or an explanation that happens to
    mention an identifier-shaped measurement gets misread as one."""
    log = [*good_log(), query_call("Q4", breakdowns=["cart.size"])]
    entry = "cart.size - broke down but did not compare values below 8 on P99(duration_ms)"
    issues = validate_draft(draft(not_checked=[entry]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]
    assert "partially_checked" in issues[0].message
    assert "split it out" not in issues[0].message


def test_a_colon_separated_qualifier_is_still_pointed_at_partially_checked() -> None:
    log = [*good_log(), query_call("Q4", breakdowns=["cart.size"])]
    entry = "cart.size: broke down but did not compare values below 8 on P99(duration_ms)"
    issues = validate_draft(draft(not_checked=[entry]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]
    assert "partially_checked" in issues[0].message
    assert "split it out" not in issues[0].message


# --------------------------------------------------------------------------
# What the model is handed back
# --------------------------------------------------------------------------


def test_the_rejection_message_names_every_issue() -> None:
    issues = validate_draft(
        draft(hypotheses=[hypothesis(evidence=[], negation=None)], not_checked=[]),
        good_log(),
        run_id=RUN_ID,
    )
    terms = queried_terms(good_log(), run_id=RUN_ID)
    message = rejection_message(issues, terms)
    assert len(issues) == 3
    for issue in issues:
        assert issue.message in message
    assert "call submit_report again" in message


def test_the_rejection_message_ends_with_the_queried_terms() -> None:
    issues = validate_draft(draft(not_checked=[]), good_log(), run_id=RUN_ID)
    message = rejection_message(issues, queried_terms(good_log(), run_id=RUN_ID))
    last_line = message.splitlines()[-1]
    assert last_line.startswith("Columns and values your queries used: ")
    assert "deployment.version" in last_line


def test_the_rejection_message_says_so_when_there_are_no_terms_yet() -> None:
    message = rejection_message([], [])
    assert message.splitlines()[-1] == "Columns and values your queries used: none yet."


# --------------------------------------------------------------------------
# The negation has to be a negation
#
# Every case below was accepted before the semantic check existed. A model
# that ran one query and cited it as its own negation passed both rules, which
# made the receipts rule a formality. These are the adversarial cases from the
# R6 review, kept as tests so the holes stay shut.
# --------------------------------------------------------------------------


def test_a_negation_that_cites_the_evidence_query_is_rejected() -> None:
    """One query cannot be both the measurement and its own control."""
    same = hypothesis(negation=Evidence(query_id="Q1", summary="same query, twice"))
    issues = validate_draft(draft(hypotheses=[same]), good_log(), run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]
    assert "the same query as its own" in issues[0].message


def test_a_negation_that_selects_instead_of_excluding_is_rejected() -> None:
    log = [
        ToolCall(name="get_workspace_context", args={}),
        query_call("Q1", breakdowns=["deployment.version"]),
        query_call("Q2", filters=[{"column": "deployment.version", "op": "=", "value": "9.9.9"}]),
        baseline_call(),
    ]
    issues = validate_draft(draft(), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]
    assert "!= or not-in" in issues[0].message


def test_a_negation_that_excludes_an_unclaimed_dimension_is_rejected() -> None:
    """Excluding a column the claim never rests on tests nothing."""
    log = [
        query_call("Q1", breakdowns=["deployment.version"]),
        query_call("Q2", filters=[{"column": "cloud.region", "op": "!=", "value": "eu-west-1"}]),
        baseline_call(),
    ]
    issues = validate_draft(draft(), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]
    assert "deployment.version" in issues[0].message


def test_an_unrelated_second_query_does_not_count_as_a_negation() -> None:
    log = [
        query_call("Q1", breakdowns=["deployment.version"]),
        query_call("Q2", breakdowns=["cart.size"]),
        baseline_call(),
    ]
    issues = validate_draft(draft(), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]
    assert "!= or not-in" in issues[0].message


def test_a_negation_citing_a_bubbleup_is_rejected_even_when_the_id_matches() -> None:
    """The hosted MCP returns the source query's id from run_bubbleup too.

    Matching on the identifier alone let a ranking stand in for a measurement,
    so every check pairs the identifier with the tool that produced it.
    """
    log = [
        query_call("Q1", breakdowns=["deployment.version"]),
        ToolCall(
            name="run_bubbleup",
            args={"group": {"deployment.version": "9.9.9"}},
            query_id="B1",
        ),
        baseline_call(),
    ]
    issues = validate_draft(
        draft(hypotheses=[hypothesis(negation=Evidence(query_id="B1", summary="ranked"))]),
        log,
        run_id=RUN_ID,
    )
    assert [issue.code for issue in issues] == ["partial"]
    assert "has to be a run_query" in issues[0].message


def test_a_negation_in_a_per_calculation_filter_is_accepted() -> None:
    """Measuring the population and its complement in one query is the shape
    Honeycomb's own guidance asks for, so it has to count."""
    log = [
        query_call("Q1", breakdowns=["deployment.version"]),
        query_call(
            "Q2",
            calculations=[
                {
                    "op": "P99",
                    "column": "duration_ms",
                    "name": "outside",
                    "filters": [
                        {"column": "deployment.version", "op": "!=", "value": "9.9.9"},
                    ],
                }
            ],
        ),
        baseline_call(),
    ]
    assert validate_draft(draft(), log, run_id=RUN_ID) == []


def test_not_in_counts_as_an_exclusion() -> None:
    log = [
        query_call("Q1", breakdowns=["deployment.version"]),
        query_call(
            "Q2",
            filters=[{"column": "deployment.version", "op": "not-in", "value": ["9.9.9"]}],
        ),
        baseline_call(),
    ]
    assert validate_draft(draft(), log, run_id=RUN_ID) == []


def test_one_claimed_dimension_is_enough_to_negate() -> None:
    """A live run claimed three dimensions and negated the version alone. That
    is a real test of the claim, so requiring all three would reject it."""
    three = hypothesis(
        dims={
            "deployment.version": "9.9.9",
            "cloud.region": "us-west-2",
            "payment.provider": "stripe",
        }
    )
    log = [
        query_call("Q1", breakdowns=["deployment.version", "cloud.region", "payment.provider"]),
        query_call("Q2", filters=[{"column": "deployment.version", "op": "!=", "value": "9.9.9"}]),
        baseline_call(),
    ]
    assert validate_draft(draft(hypotheses=[three]), log, run_id=RUN_ID) == []


def test_a_negation_with_no_dims_to_negate_is_rejected() -> None:
    issues = validate_draft(draft(hypotheses=[hypothesis(dims={})]), good_log(), run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]
    assert "nothing for it to" in issues[0].message


def test_require_negation_off_still_rejects_a_negation_that_was_never_run() -> None:
    """The ablation drops the requirement to negate, not the requirement that a
    cited identifier be real."""
    made_up = hypothesis(negation=Evidence(query_id="Q-invented", summary="trust me"))
    issues = validate_draft(
        draft(hypotheses=[made_up]), good_log(), run_id=RUN_ID, require_negation=False
    )
    assert [issue.code for issue in issues] == ["unsupported"]


# --------------------------------------------------------------------------
# Claimed dimensions have to have been measured
# --------------------------------------------------------------------------


def test_dims_naming_a_column_no_query_touched_are_rejected() -> None:
    """`dims` is the field the grader scores, so it is the field most worth
    checking. Before this, it carried no receipts at all."""
    invented = hypothesis(dims={"never.queried": "mars-1"})
    issues = validate_draft(draft(hypotheses=[invented]), good_log(), run_id=RUN_ID)
    codes = [issue.code for issue in issues]
    assert "unsupported" in codes
    assert any("never.queried" in issue.message for issue in issues)


def test_dims_measured_by_a_breakdown_are_accepted() -> None:
    log = [
        query_call("Q1", breakdowns=["cloud.region"]),
        query_call("Q2", filters=[{"column": "cloud.region", "op": "!=", "value": "us-west-2"}]),
        baseline_call(),
    ]
    only_region = hypothesis(dims={"cloud.region": "us-west-2"})
    assert validate_draft(draft(hypotheses=[only_region]), log, run_id=RUN_ID) == []


# --------------------------------------------------------------------------
# A failed call is not a receipt
# --------------------------------------------------------------------------


def test_a_query_id_from_a_failed_call_is_not_a_receipt() -> None:
    log = [
        ToolCall(name="run_query", args={}, query_id="Q1", is_error=True),
        ToolCall(name="run_query", args={}, query_id="Q2", is_error=True),
        baseline_call(),
    ]
    issues = validate_draft(draft(), log, run_id=RUN_ID)
    cited = [issue for issue in issues if "cites query_id" in issue.message]
    assert [issue.code for issue in cited] == ["unsupported", "unsupported"]
    assert any("Q1" in issue.message for issue in cited)
    assert any("Q2" in issue.message for issue in cited)


def test_query_ids_leaves_out_failed_calls() -> None:
    log = [*good_log(), ToolCall(name="run_query", args={}, query_id="Q9", is_error=True)]
    assert query_ids(log) == {"Q1", "Q2", "Q3"}


# --------------------------------------------------------------------------
# The not-checked matcher
# --------------------------------------------------------------------------


def test_a_not_checked_entry_in_a_different_case_is_still_caught() -> None:
    issues = validate_draft(
        draft(not_checked=["Deployment.Version was never examined"]), good_log(), run_id=RUN_ID
    )
    assert [issue.code for issue in issues] == ["not_checked_false"]


def test_a_backticked_single_word_column_is_caught() -> None:
    log = [
        query_call("Q1", breakdowns=["deployment.version"], filters=[{"column": "error"}]),
        query_call("Q2", filters=[{"column": "deployment.version", "op": "!=", "value": "9.9.9"}]),
        baseline_call(),
    ]
    issues = validate_draft(draft(not_checked=["`error`"]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]


def test_a_bare_single_word_column_as_the_whole_entry_is_caught() -> None:
    log = [
        query_call("Q1", breakdowns=["error"]),
        query_call("Q2", filters=[{"column": "deployment.version", "op": "!=", "value": "9.9.9"}]),
        baseline_call(),
    ]
    issues = validate_draft(draft(not_checked=["error"]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]


def test_the_same_word_inside_ordinary_prose_is_still_allowed() -> None:
    """The live false positive that prompted the separator rule. An entry about
    error message text does not claim the `error` column was unexamined."""
    log = [
        query_call("Q1", breakdowns=["error", "deployment.version"]),
        query_call("Q2", filters=[{"column": "deployment.version", "op": "!=", "value": "9.9.9"}]),
        baseline_call(),
    ]
    entry = "did not examine specific error message text"
    assert validate_draft(draft(not_checked=[entry]), log, run_id=RUN_ID) == []


def test_an_entry_naming_a_queried_column_in_its_explanation_is_allowed() -> None:
    """The live false positive that prompted the subject rule.

    The run never queried `deployment.version`, so the claim is true. The
    explanation mentions `payments.charge`, which the run did query, as
    context for what the breakdown would have been.
    """
    log = [
        query_call("Q1", filters=[{"column": "name", "op": "=", "value": "payments.charge"}]),
        query_call("Q2", filters=[{"column": "cloud.region", "op": "!=", "value": "us-west-2"}]),
        baseline_call(),
    ]
    entry = "deployment.version - did not break down payments.charge by deployment version"
    only_region = hypothesis(dims={"cloud.region": "us-west-2"})
    assert (
        validate_draft(draft(hypotheses=[only_region], not_checked=[entry]), log, run_id=RUN_ID)
        == []
    )


def test_the_subject_of_an_entry_is_still_checked() -> None:
    log = [
        query_call("Q1", breakdowns=["deployment.version"]),
        query_call("Q2", filters=[{"column": "deployment.version", "op": "!=", "value": "9.9.9"}]),
        baseline_call(),
    ]
    entry = "deployment.version - never broken down"
    issues = validate_draft(draft(not_checked=[entry]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]


def test_a_colon_separates_the_subject_too() -> None:
    log = [
        query_call("Q1", breakdowns=["http.route"]),
        query_call("Q2", filters=[{"column": "deployment.version", "op": "!=", "value": "9.9.9"}]),
        baseline_call(),
    ]
    issues = validate_draft(draft(not_checked=["http.route: not broken down"]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["not_checked_false"]


# --------------------------------------------------------------------------
# The baseline is asked of both answers
#
# Requiring it only when a report denied an incident made the code cheaper to
# pass in one direction than the other, which is the code choosing an answer.
# --------------------------------------------------------------------------


def test_an_incident_with_no_baseline_evidence_is_rejected() -> None:
    issues = validate_draft(draft(baseline_evidence=[]), good_log(), run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["unsupported"]
    assert "An incident is a change" in issues[0].message


def test_a_quiet_window_with_no_baseline_evidence_is_still_rejected() -> None:
    quiet = draft(incident_present=False, hypotheses=[], baseline_evidence=[])
    issues = validate_draft(quiet, good_log(), run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["unsupported"]
    assert "flat across the window" in issues[0].message


def test_both_answers_are_accepted_once_they_cite_a_baseline() -> None:
    log = good_log()
    incident = draft()
    quiet = draft(
        incident_present=False,
        hypotheses=[],
        baseline_evidence=[Evidence(query_id="Q3", summary="flat")],
    )
    assert validate_draft(incident, log, run_id=RUN_ID) == []
    assert validate_draft(quiet, log, run_id=RUN_ID) == []


def test_a_baseline_citing_a_query_that_was_never_run_is_rejected() -> None:
    made_up = draft(baseline_evidence=[Evidence(query_id="Q-invented", summary="trust me")])
    issues = validate_draft(made_up, good_log(), run_id=RUN_ID)
    # Two things are wrong with it: the identifier is not in the log, and what
    # is left cites no run_query.
    assert [issue.code for issue in issues] == ["unsupported", "partial"]
    assert "Q-invented" in issues[0].message


# --------------------------------------------------------------------------
# EDW-1366: a baseline with no rows in the run
#
# A query outside the run window returned a real answer, just not one about
# this run's traffic. The live case: "0% error rate for the 10 minutes
# before the window", from a query that ended exactly when the window began.
# --------------------------------------------------------------------------

WINDOW_START = "2026-09-04T06:30:30Z"
WINDOW_END = "2026-09-04T06:50:30Z"


def test_a_baseline_entirely_before_the_window_is_rejected() -> None:
    log = [
        *good_log(),
        query_call("Q4", from_="2026-09-04T06:20:30Z", to="2026-09-04T06:25:30Z"),
    ]
    report = draft(baseline_evidence=[Evidence(query_id="Q4", summary="0% error before onset")])
    issues = validate_draft(
        report, log, run_id=RUN_ID, window_start=WINDOW_START, window_end=WINDOW_END
    )
    assert [issue.code for issue in issues] == ["unsupported"]
    assert "Q4" in issues[0].message
    assert "outside the run window" in issues[0].message


def test_a_baseline_ending_exactly_at_the_window_start_is_rejected() -> None:
    """The live case: a query that ends the instant the window opens has no
    rows from this run, even though its `to` matches the window's own start."""
    log = [
        *good_log(),
        query_call("Q4", from_="2026-09-04T06:20:30Z", to=WINDOW_START),
    ]
    report = draft(baseline_evidence=[Evidence(query_id="Q4", summary="0% error before onset")])
    issues = validate_draft(
        report, log, run_id=RUN_ID, window_start=WINDOW_START, window_end=WINDOW_END
    )
    assert [issue.code for issue in issues] == ["unsupported"]
    assert "Q4" in issues[0].message


def test_a_baseline_overlapping_the_window_edge_is_accepted() -> None:
    log = [
        *good_log(),
        query_call("Q4", from_="2026-09-04T06:20:30Z", to="2026-09-04T06:35:30Z"),
    ]
    report = draft(baseline_evidence=[Evidence(query_id="Q4", summary="flat across onset")])
    issues = validate_draft(
        report, log, run_id=RUN_ID, window_start=WINDOW_START, window_end=WINDOW_END
    )
    assert issues == []


def test_a_baseline_inside_the_window_is_accepted() -> None:
    log = [
        *good_log(),
        query_call("Q4", from_="2026-09-04T06:31:00Z", to="2026-09-04T06:40:00Z"),
    ]
    report = draft(baseline_evidence=[Evidence(query_id="Q4", summary="flat")])
    issues = validate_draft(
        report, log, run_id=RUN_ID, window_start=WINDOW_START, window_end=WINDOW_END
    )
    assert issues == []


def test_no_window_given_means_the_out_of_window_check_is_skipped() -> None:
    log = [
        *good_log(),
        query_call("Q4", from_="2026-09-04T06:20:30Z", to="2026-09-04T06:25:30Z"),
    ]
    report = draft(baseline_evidence=[Evidence(query_id="Q4", summary="0% error before onset")])
    assert validate_draft(report, log, run_id=RUN_ID) == []


def test_a_baseline_using_start_time_and_end_time_is_checked_the_same_way() -> None:
    log = [
        *good_log(),
        query_call("Q4", start_time="2026-09-04T06:20:30Z", end_time="2026-09-04T06:25:30Z"),
    ]
    report = draft(baseline_evidence=[Evidence(query_id="Q4", summary="0% error before onset")])
    issues = validate_draft(
        report, log, run_id=RUN_ID, window_start=WINDOW_START, window_end=WINDOW_END
    )
    assert [issue.code for issue in issues] == ["unsupported"]
    assert "Q4" in issues[0].message


# --------------------------------------------------------------------------
# A candidate that was ruled out
# --------------------------------------------------------------------------


def test_a_rejected_candidate_is_accepted_with_the_query_that_ruled_it_out() -> None:
    """There was nowhere to record this before. A model that measured a
    suspicious number, checked it, and decided against it could only drop the
    finding or report it as an incident."""
    ruled_out = draft(
        rejected_candidates=[
            {
                "claim": "one region looked slow on the database span",
                "dims": {"cloud.region": "eu-west-1"},
                "reason": "it was already at that level before anything changed",
                "evidence": [{"query_id": "Q3", "summary": "flat across the whole window"}],
            }
        ]
    )
    assert validate_draft(ruled_out, good_log(), run_id=RUN_ID) == []


def test_a_rejected_candidate_citing_an_unrun_query_is_rejected() -> None:
    """A citation is a citation wherever it appears in the report."""
    ruled_out = draft(
        rejected_candidates=[
            {
                "claim": "one region looked slow",
                "reason": "pre-existing",
                "evidence": [{"query_id": "Q-invented", "summary": "trust me"}],
            }
        ]
    )
    issues = validate_draft(ruled_out, good_log(), run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["unsupported"]
    assert "Q-invented" in issues[0].message


def test_a_rejected_candidate_needs_no_negation() -> None:
    """Ruling something out is not a claim about a population, so the negation
    rule does not apply to it."""
    ruled_out = draft(
        rejected_candidates=[{"claim": "cart size looked correlated", "reason": "it was not"}]
    )
    assert validate_draft(ruled_out, good_log(), run_id=RUN_ID) == []


# --------------------------------------------------------------------------
# A range claim (cart.size: ">= 8") is negated by its complement, not by !=
#
# EDW-1361: trigger-checkout-latency repeat 5 claimed cart.size >= 8 and
# negated with cart.size < 8, the exact complement, and was rejected because
# `<` was not in EXCLUDING_OPS. A live run scored a right answer as a
# validation failure.
# --------------------------------------------------------------------------


def test_the_trigger_5_shape_passes() -> None:
    """cart.size >= 8, negated with cart.size < 8 in the same run_query shape
    that cost a right answer 0.25 on 2026-09-04."""
    range_claim = hypothesis(
        dims={"cart.size": ">= 8", "name": "checkout.process"},
        negation=Evidence(query_id="Q2", summary="P99 flat under cart.size < 8"),
    )
    log = [
        query_call(
            "Q1",
            filters=[{"column": "name", "op": "=", "value": "checkout.process"}],
            breakdowns=["cart.size"],
        ),
        query_call(
            "Q2",
            filters=[
                {"column": "name", "op": "=", "value": "checkout.process"},
                {"column": "cart.size", "op": "<", "value": 8},
            ],
        ),
        baseline_call(),
    ]
    assert validate_draft(draft(hypotheses=[range_claim]), log, run_id=RUN_ID) == []


def test_a_complement_on_the_measured_column_is_not_a_negation() -> None:
    """The trigger-2 shape from 2026-09-04: `duration_ms > 1000` negated by
    `P99(duration_ms) WHERE duration_ms <= 1000`, which says fast requests
    are fast. A negation excludes a population; it does not re-slice the
    measurement."""
    symptom_claim = hypothesis(
        dims={"duration_ms": "> 1000", "name": "HTTP POST /checkout"},
        negation=Evidence(query_id="Q2", summary="P99 under 1000 for the rest"),
    )
    log = [
        query_call("Q1", breakdowns=["duration_ms"]),
        query_call(
            "Q2",
            calculations=[{"op": "P99", "column": "duration_ms"}],
            filters=[
                {"column": "name", "op": "=", "value": "HTTP POST /checkout"},
                {"column": "duration_ms", "op": "<=", "value": 1000},
            ],
        ),
        baseline_call(),
    ]
    issues = validate_draft(draft(hypotheses=[symptom_claim]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]
    assert "excludes []" in issues[0].message


def test_a_complement_on_a_column_the_query_does_not_measure_still_counts() -> None:
    """Same complement, but the query counts rows rather than calculating
    over the claimed column, so the filter selects a population."""
    range_claim = hypothesis(
        dims={"cart.size": ">= 8"},
        negation=Evidence(query_id="Q2", summary="P99 flat under cart.size < 8"),
    )
    log = [
        query_call("Q1", breakdowns=["cart.size"]),
        query_call(
            "Q2",
            calculations=[{"op": "P99", "column": "duration_ms"}],
            filters=[{"column": "cart.size", "op": "<", "value": 8}],
        ),
        baseline_call(),
    ]
    assert validate_draft(draft(hypotheses=[range_claim]), log, run_id=RUN_ID) == []


def test_excluded_columns_without_dims_ignores_the_measured_set() -> None:
    """The old rule is untouched: `!=` on a measured column still counts,
    as it did before, so the grader's stored cells do not move."""
    call = query_call(
        "Q2",
        calculations=[{"op": "P99", "column": "duration_ms"}],
        filters=[{"column": "duration_ms", "op": "!=", "value": 1000}],
    )
    assert excluded_columns([call]) == {"duration_ms"}
    assert excluded_columns([call], dims={"duration_ms": "> 1000"}) == {"duration_ms"}


@pytest.mark.parametrize("spelling", ["8-12", "8, 9, 10, 11, 12"])
def test_a_spelled_out_claim_is_negated_by_its_complement(spelling: str) -> None:
    """2026-09-08: two Sonnet cells wrote the population as a span and as a
    list. `cart.size < 8` excludes exactly those rows, read the way the grader
    reads the claim (`gen.topology.selects`), so it is the negation."""
    range_claim = hypothesis(
        dims={"cart.size": spelling},
        negation=Evidence(query_id="Q2", summary="P99 flat under cart.size < 8"),
    )
    log = [
        query_call("Q1", breakdowns=["cart.size"]),
        query_call("Q2", filters=[{"column": "cart.size", "op": "<", "value": 8}]),
        baseline_call(),
    ]
    assert validate_draft(draft(hypotheses=[range_claim]), log, run_id=RUN_ID) == []


@pytest.mark.parametrize("spelling", ["8-12", "8, 9, 10, 11, 12"])
def test_a_spelled_out_claim_is_negated_by_not_in(spelling: str) -> None:
    range_claim = hypothesis(
        dims={"cart.size": spelling},
        negation=Evidence(query_id="Q2", summary="P99 flat outside 8 to 12"),
    )
    log = [
        query_call("Q1", breakdowns=["cart.size"]),
        query_call(
            "Q2", filters=[{"column": "cart.size", "op": "not-in", "value": [8, 9, 10, 11, 12]}]
        ),
        baseline_call(),
    ]
    assert validate_draft(draft(hypotheses=[range_claim]), log, run_id=RUN_ID) == []


def test_a_spelled_out_claim_against_less_than_9_fails_the_wrong_bound() -> None:
    range_claim = hypothesis(
        dims={"cart.size": "8-12"},
        negation=Evidence(query_id="Q2", summary="P99 flat under cart.size < 9"),
    )
    log = [
        query_call("Q1", breakdowns=["cart.size"]),
        query_call("Q2", filters=[{"column": "cart.size", "op": "<", "value": 9}]),
        baseline_call(),
    ]
    issues = validate_draft(draft(hypotheses=[range_claim]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]
    assert "complementary comparison" in issues[0].message
    assert "8" not in issues[0].message.replace("Q2", "")


def test_cart_size_less_than_9_against_gte_8_fails_the_wrong_bound() -> None:
    range_claim = hypothesis(
        dims={"cart.size": ">= 8"},
        negation=Evidence(query_id="Q2", summary="P99 flat under cart.size < 9"),
    )
    log = [
        query_call("Q1", breakdowns=["cart.size"]),
        query_call("Q2", filters=[{"column": "cart.size", "op": "<", "value": 9}]),
        baseline_call(),
    ]
    issues = validate_draft(draft(hypotheses=[range_claim]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]
    assert "complementary comparison" in issues[0].message


def test_cart_size_gte_8_against_gte_8_fails_the_same_side() -> None:
    range_claim = hypothesis(
        dims={"cart.size": ">= 8"},
        negation=Evidence(query_id="Q2", summary="P99 for cart.size >= 8"),
    )
    log = [
        query_call("Q1", breakdowns=["cart.size"]),
        query_call("Q2", filters=[{"column": "cart.size", "op": ">=", "value": 8}]),
        baseline_call(),
    ]
    issues = validate_draft(draft(hypotheses=[range_claim]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]


def test_a_plain_value_negation_with_not_equal_still_passes() -> None:
    plain = hypothesis(
        dims={"payment.provider": "adyen"},
        negation=Evidence(query_id="Q2", summary="flat outside adyen"),
    )
    log = [
        query_call("Q1", breakdowns=["payment.provider"]),
        query_call("Q2", filters=[{"column": "payment.provider", "op": "!=", "value": "adyen"}]),
        baseline_call(),
    ]
    assert validate_draft(draft(hypotheses=[plain]), log, run_id=RUN_ID) == []


def test_a_less_than_filter_on_a_non_range_claimed_value_still_fails() -> None:
    """cart.size claimed as the plain string "8" is an exact claim, not a
    range, so a `<` filter on it is not a negation of anything."""
    plain = hypothesis(
        dims={"cart.size": "8"},
        negation=Evidence(query_id="Q2", summary="cart.size under 8"),
    )
    log = [
        query_call("Q1", breakdowns=["cart.size"]),
        query_call("Q2", filters=[{"column": "cart.size", "op": "<", "value": 8}]),
        baseline_call(),
    ]
    issues = validate_draft(draft(hypotheses=[plain]), log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]


def test_the_bound_arriving_as_a_string_still_passes() -> None:
    range_claim = hypothesis(
        dims={"cart.size": ">= 8"},
        negation=Evidence(query_id="Q2", summary="flat under cart.size < 8"),
    )
    log = [
        query_call("Q1", breakdowns=["cart.size"]),
        query_call("Q2", filters=[{"column": "cart.size", "op": "<", "value": "8"}]),
        baseline_call(),
    ]
    assert validate_draft(draft(hypotheses=[range_claim]), log, run_id=RUN_ID) == []


def test_greater_than_against_less_or_equal_complement_passes() -> None:
    range_claim = hypothesis(
        dims={"cart.size": "> 8"},
        negation=Evidence(query_id="Q2", summary="flat at cart.size <= 8"),
    )
    log = [
        query_call("Q1", breakdowns=["cart.size"]),
        query_call("Q2", filters=[{"column": "cart.size", "op": "<=", "value": 8}]),
        baseline_call(),
    ]
    assert validate_draft(draft(hypotheses=[range_claim]), log, run_id=RUN_ID) == []


def test_the_rejection_message_names_the_complement_as_an_accepted_form() -> None:
    range_claim = hypothesis(
        dims={"cart.size": ">= 8"},
        negation=Evidence(query_id="Q2", summary="cart.size >= 8 again"),
    )
    log = [
        query_call("Q1", breakdowns=["cart.size"]),
        query_call("Q2", filters=[{"column": "cart.size", "op": ">=", "value": 8}]),
        baseline_call(),
    ]
    issues = validate_draft(draft(hypotheses=[range_claim]), log, run_id=RUN_ID)
    assert "complementary comparison (< 50) at the same bound" in issues[0].message


# --------------------------------------------------------------------------
# column_usage
# --------------------------------------------------------------------------


def test_column_usage_records_breakdowns_filters_and_calculations() -> None:
    log = [
        query_call(
            "Q1",
            breakdowns=["deployment.version"],
            filters=[{"column": "cloud.region", "op": "=", "value": "us-west-2"}],
            calculations=[{"op": "P99", "column": "duration_ms"}],
        )
    ]
    usage = column_usage(log)
    version = usage["deployment.version"][0]
    assert version.query_id == "Q1"
    assert version.in_breakdowns is True
    assert version.in_filters is False
    assert version.in_calculations is False
    assert version.other_filter_columns == frozenset({"cloud.region"})
    assert version.calculations == ("P99(duration_ms)",)
    assert version.has_granularity is False

    region = usage["cloud.region"][0]
    assert region.in_filters is True
    # deployment.version is a breakdown column on this query, not a filter, so
    # it is not one of cloud.region's *other filter* columns.
    assert region.other_filter_columns == frozenset()

    duration = usage["duration_ms"][0]
    assert duration.in_calculations is True


def test_column_usage_ignores_errored_calls() -> None:
    log = [query_call("Q1", breakdowns=["deployment.version"], name="run_query")]
    log[0] = ToolCall(name="run_query", args=log[0].args, query_id="Q1", is_error=True)
    assert column_usage(log) == {}


def test_column_usage_ignores_tools_outside_query_tools() -> None:
    log = [
        ToolCall(name="get_dataset_columns", args={"column": "deployment.version"}, query_id="Q1")
    ]
    assert column_usage(log) == {}


def test_column_usage_reads_a_bubbleup_group_selection_as_a_filter() -> None:
    log = [
        ToolCall(
            name="run_bubbleup",
            args={"query_pk": "Q1", "selection": {"type": "group", "group": {"http.route": "/x"}}},
            query_id="B1",
        )
    ]
    usage = column_usage(log)
    assert usage["http.route"][0].in_filters is True
    assert usage["http.route"][0].query_id == "B1"


def test_column_usage_keys_by_the_column_as_the_call_named_it() -> None:
    """Case-insensitive lookup, when it is wanted, is the caller's job."""
    log = [query_call("Q1", breakdowns=["Deployment.Version"])]
    usage = column_usage(log)
    assert "Deployment.Version" in usage
    assert "deployment.version" not in usage


# --------------------------------------------------------------------------
# column_usage: orders, havings, and a list_spans call with no query_id.
# `queried_terms` (via `_collect`) already treats these columns as queried;
# column_usage used to have no entry for them at all, which made a true
# subject fail as partially_checked_subject_not_a_column.
# --------------------------------------------------------------------------


def test_a_column_named_only_in_orders_gets_a_harmless_usage_record() -> None:
    log = [
        ToolCall(
            name="run_query",
            args={
                "query_spec": {
                    "calculations": [{"op": "P99", "column": "duration_ms"}],
                    "orders": [{"column": "cart.size", "order": "descending"}],
                }
            },
            query_id="Q1",
        )
    ]
    usage = column_usage(log)
    assert "cart.size" in usage
    record = usage["cart.size"][0]
    assert record.in_breakdowns is False
    assert record.in_filters is False
    assert record.in_calculations is False
    assert record.calculations == ()


def test_a_column_named_only_in_havings_gets_a_harmless_usage_record() -> None:
    log = [
        ToolCall(
            name="run_query",
            args={
                "query_spec": {
                    "calculations": [{"op": "COUNT"}],
                    "havings": [
                        {"calculate_op": "COUNT", "column": "cloud.region", "op": ">", "value": 0}
                    ],
                }
            },
            query_id="Q1",
        )
    ]
    usage = column_usage(log)
    assert "cloud.region" in usage
    assert usage["cloud.region"][0].calculations == ()


def test_an_orders_only_column_is_not_rejected_as_not_a_column_and_contradicts_nothing() -> None:
    """The real risk of adding orders/havings columns: an order-only column
    must not inherit the query's own calculations, or a claim that it was
    read with other_measurement X would be falsely contradicted by a
    calculation the column had nothing to do with."""
    log = [
        ToolCall(
            name="run_query",
            args={
                "query_spec": {
                    "calculations": [{"op": "P99", "column": "duration_ms"}],
                    "orders": [{"column": "cart.size", "order": "descending"}],
                }
            },
            query_id="Q1",
        ),
        baseline_call(),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    assert "cart.size" in terms
    for reading in ("per_value", "over_time", "outside_selection"):
        check = partial_check(subject="cart.size", reading=reading)
        assert partially_checked_issues([check], log, terms) == [], reading
    check = partial_check(
        subject="cart.size", reading="other_measurement", measurement="P99(duration_ms)"
    )
    assert partially_checked_issues([check], log, terms) == []


def test_a_list_spans_call_with_no_query_id_still_gets_a_column_usage_record() -> None:
    log = [
        ToolCall(
            name="list_spans",
            args={"query_spec": {"filters": [{"column": "cart.size", "op": ">=", "value": 8}]}},
            query_id=None,
        )
    ]
    usage = column_usage(log)
    assert "cart.size" in usage
    assert usage["cart.size"][0].in_filters is True


def test_a_column_from_a_query_id_less_list_spans_call_is_not_rejected_as_not_a_column() -> None:
    log = [
        ToolCall(
            name="list_spans",
            args={"query_spec": {"filters": [{"column": "cart.size", "op": ">=", "value": 8}]}},
            query_id=None,
        ),
        baseline_call(),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="cart.size", reading="per_value")
    assert partially_checked_issues([check], log, terms) == []


def test_a_granularity_of_zero_does_not_count_as_a_granularity() -> None:
    """`bool(spec.get("granularity"))` was True for the string "0": a
    non-empty string is truthy even when it parses to zero."""
    log = [
        ToolCall(
            name="run_query",
            args={
                "query_spec": {
                    "calculations": [{"op": "COUNT"}],
                    "breakdowns": ["deployment.version"],
                    "granularity": "0",
                }
            },
            query_id="Q1",
        )
    ]
    usage = column_usage(log)
    assert usage["deployment.version"][0].has_granularity is False


def test_a_granularity_of_zero_does_not_contradict_over_time() -> None:
    log = [
        ToolCall(
            name="run_query",
            args={
                "query_spec": {
                    "calculations": [{"op": "COUNT"}],
                    "breakdowns": ["deployment.version"],
                    "granularity": 0,
                }
            },
            query_id="Q1",
        ),
        baseline_call(),
    ]
    terms = queried_terms(log, run_id=RUN_ID)
    check = partial_check(subject="deployment.version", reading="over_time")
    assert partially_checked_issues([check], log, terms) == []


# --------------------------------------------------------------------------
# SYSTEM_COLUMNS
# --------------------------------------------------------------------------


def test_system_columns_cover_the_documented_prefixes_and_names() -> None:
    for column in (
        "meta.signal_type",
        "telemetry.sdk.name",
        "library.name",
        "trace.span_id",
        "span.num_events",
        "scenario.run_id",
        "type",
        "parent_name",
        "service.name",
    ):
        assert column in SYSTEM_COLUMNS, column
    for column in ("status_code", "status_message", "error", "exception.type", "customer.id"):
        assert column not in SYSTEM_COLUMNS, column


def test_system_columns_match_case_insensitively() -> None:
    assert "Trace.Span_Id" in SYSTEM_COLUMNS


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-04T06:30:30Z",
        "2026-09-04T06:30:30+00:00",
        "2026-09-04T06:30:30",
        "2026-09-04 06:30:30Z",
        "2026-09-04T06:30:30.250Z",
        1788_000_000,
        1788_000_000.5,
    ],
)
def test_a_time_bound_in_a_shape_the_logs_carry_parses(value: object) -> None:
    assert _parse_time_bound(value) is not None


@pytest.mark.parametrize("value", ["-2h", "", None, True, 1788_000_000_000, "-1e20", "now"])
def test_a_time_bound_that_is_not_a_moment_does_not_parse(value: object) -> None:
    """Relative and open-ended bounds (`-2h` appears in live logs) are not a
    moment, so the out-of-window check has nothing to compare and skips
    the entry. Epoch milliseconds are past the year 50000 and count as
    unparseable rather than as a date."""
    assert _parse_time_bound(value) is None


def test_a_baseline_with_only_a_from_bound_is_accepted() -> None:
    """`from: -2h` with no `to` is an open range the check cannot place, so it
    neither adds nor removes an issue."""
    log = [*good_log(), query_call("Q4", from_="-2h")]
    report = draft(baseline_evidence=[Evidence(query_id="Q4", summary="last two hours")])
    issues = validate_draft(
        report, log, run_id=RUN_ID, window_start=WINDOW_START, window_end=WINDOW_END
    )
    assert issues == []


def test_a_baseline_whose_spec_arrived_as_a_json_string_is_still_checked() -> None:
    """Twenty-one live run_query calls carried `query_spec` as a JSON string.
    The bounds inside it count the same as a dict's."""
    call = query_call("Q4", from_="2026-09-04T06:20:30Z", to="2026-09-04T06:30:30Z")
    call = call.model_copy(
        update={"args": {**call.args, "query_spec": json.dumps(call.args["query_spec"])}}
    )
    report = draft(baseline_evidence=[Evidence(query_id="Q4", summary="0% before the window")])
    issues = validate_draft(
        report, [*good_log(), call], run_id=RUN_ID, window_start=WINDOW_START, window_end=WINDOW_END
    )
    assert [issue.code for issue in issues] == ["unsupported"]
    assert "2026-09-04T06:30:30Z" in issues[0].message
    assert "+00:00" not in issues[0].message


def test_an_unparseable_window_bound_skips_the_check() -> None:
    log = [*good_log(), query_call("Q4", from_="2026-09-04T06:20:30Z", to="2026-09-04T06:25:30Z")]
    report = draft(baseline_evidence=[Evidence(query_id="Q4", summary="before")])
    assert (
        validate_draft(report, log, run_id=RUN_ID, window_start="soon", window_end=WINDOW_END) == []
    )
