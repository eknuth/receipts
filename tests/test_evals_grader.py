"""Tests for evals/grader.py: outcome scoring against ground truth.

Two kinds of report are graded here. Synthetic ones, built by hand, pin the
ordering the project argues for and the edges of each component. Live ones,
copied from `evals/results/` into `tests/fixtures/reports/` with the team
slug removed, pin the numbers that end up in the README. Nothing here touches
the network or a model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent.report import Evidence, Hypothesis, Report, ToolCall, load_report
from agent.validate import validate_draft
from evals.grader import (
    WEIGHTS,
    Grade,
    arithmetic,
    dims_jaccard,
    grade,
    grade_file,
    honeycomb_process_score,
    main,
    window_start_for,
)
from gen.scenario import Scenario, load_all, load_scenario

FIXTURES = Path(__file__).parent / "fixtures" / "reports"
RUNS_DIR = FIXTURES / "runs"

PAYMENTS = load_scenario("payments-stripe-v251-uswest")
CONTROL = load_scenario("control-quiet")

RUN_ID = "run-synthetic"
WINDOW_START = datetime(2026, 9, 3, 2, 0, tzinfo=UTC)
TRUE_ONSET = WINDOW_START + timedelta(minutes=10)

TRUE_DIMS = {
    "deployment.version": "2.5.1",
    "cloud.region": "us-west-2",
    "payment.provider": "stripe",
}
WRONG_DIMS = {"cloud.region": "eu-west-1"}


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def query_call(
    query_id: str,
    *,
    name: str = "run_query",
    breakdowns: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    calculations: list[dict[str, Any]] | None = None,
    is_error: bool = False,
) -> ToolCall:
    spec: dict[str, Any] = {
        "calculations": calculations or [{"op": "P99", "column": "duration_ms"}],
        "filters": [{"column": "scenario.run_id", "op": "=", "value": RUN_ID}, *(filters or [])],
    }
    if breakdowns:
        spec["breakdowns"] = breakdowns
    return ToolCall(
        name=name,
        args={"dataset_slug": "receipts-shop", "query_spec": spec},
        query_id=query_id,
        is_error=is_error,
    )


def good_log() -> list[ToolCall]:
    """Context, a breakdown on the true dims, a negation on one of them, a baseline."""
    return [
        ToolCall(name="get_workspace_context", args={}),
        query_call(
            "Q1",
            breakdowns=["deployment.version", "cloud.region", "payment.provider"],
            filters=[{"column": "name", "op": "=", "value": "payments.charge"}],
        ),
        query_call("Q2", filters=[{"column": "cloud.region", "op": "!=", "value": "us-west-2"}]),
        query_call("Q3"),
    ]


def hypothesis(**overrides: Any) -> Hypothesis:
    base: dict[str, Any] = {
        "claim": "stripe in us-west-2 on 2.5.1 got slow",
        "dims": dict(TRUE_DIMS),
        "slow_or_failing_span": "payments.charge",
        "confidence": "high",
        "evidence": [Evidence(query_id="Q1", summary="P99 180ms to 1100ms")],
        "negation": Evidence(query_id="Q2", summary="P99 flat outside"),
    }
    base.update(overrides)
    return Hypothesis.model_validate(base)


def report(**overrides: Any) -> Report:
    base: dict[str, Any] = {
        "run_id": RUN_ID,
        "scenario_id": PAYMENTS.id,
        "provider": "test",
        "model": "test-model",
        "incident_present": True,
        "hypotheses": [hypothesis()],
        "onset_estimate": TRUE_ONSET,
        "not_checked": ["customer.id", "http.route"],
        "baseline_evidence": [Evidence(query_id="Q3", summary="flat before onset")],
        "tool_log": good_log(),
        "tool_calls": 4,
        "tokens_in": 1000,
        "tokens_out": 200,
        "cost_usd": 0.0123,
        "wall_s": 12.5,
        "stop_reason": "report",
    }
    base.update(overrides)
    return Report.model_validate(base)


def control_report(**overrides: Any) -> Report:
    base: dict[str, Any] = {
        "scenario_id": CONTROL.id,
        "incident_present": False,
        "hypotheses": [],
        "onset_estimate": None,
    }
    base.update(overrides)
    return report(**base)


def grade_synthetic(r: Report) -> Grade:
    scenario = PAYMENTS if r.scenario_id == PAYMENTS.id else CONTROL
    return grade(r, scenario, window_start=WINDOW_START)


# --------------------------------------------------------------------------
# The thesis: calibration ordering with the real weights
# --------------------------------------------------------------------------


def test_the_weights_sum_to_one() -> None:
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_confident_right_is_a_perfect_score() -> None:
    result = grade_synthetic(report())
    assert result.total == pytest.approx(1.0)
    assert result.outcome_score == pytest.approx(0.75)
    assert result.receipts_score == pytest.approx(0.25)
    assert result.penalties.total == 0.0


def test_the_calibration_ordering_holds_with_the_real_weights() -> None:
    """confident-wrong < hedged-wrong < hedged-right < confident-right, as totals."""
    wrong = {"dims": WRONG_DIMS, "slow_or_failing_span": "db.query"}
    confident_wrong = grade_synthetic(report(hypotheses=[hypothesis(confidence="high", **wrong)]))
    hedged_wrong = grade_synthetic(report(hypotheses=[hypothesis(confidence="low", **wrong)]))
    hedged_right = grade_synthetic(report(hypotheses=[hypothesis(confidence="low")]))
    confident_right = grade_synthetic(report(hypotheses=[hypothesis(confidence="high")]))

    # Wrong dims and span: incident 0.15 + onset 0.10 + receipts 0.15 + not_checked 0.10.
    assert confident_wrong.before_penalties == pytest.approx(0.50)
    assert confident_wrong.penalties.calibration == -0.50
    assert confident_wrong.total == pytest.approx(0.00)

    assert hedged_wrong.penalties.calibration == -0.10
    assert hedged_wrong.total == pytest.approx(0.40)

    assert hedged_right.before_penalties == pytest.approx(1.0)
    assert hedged_right.penalties.calibration == -0.10
    assert hedged_right.total == pytest.approx(0.90)

    assert confident_right.total == pytest.approx(1.0)

    assert confident_wrong.total < hedged_wrong.total < hedged_right.total < confident_right.total


def test_medium_sits_between_high_and_low_in_both_tables() -> None:
    wrong = {"dims": WRONG_DIMS, "slow_or_failing_span": "db.query"}
    medium_wrong = grade_synthetic(report(hypotheses=[hypothesis(confidence="medium", **wrong)]))
    medium_right = grade_synthetic(report(hypotheses=[hypothesis(confidence="medium")]))
    assert medium_wrong.total == pytest.approx(0.25)
    assert medium_right.total == pytest.approx(0.95)


def test_the_wrong_line_is_a_dims_score_under_a_half() -> None:
    two_of_three = {k: v for k, v in list(TRUE_DIMS.items())[:2]}
    one_of_three = {"cloud.region": "us-west-2"}
    assert grade_synthetic(report(hypotheses=[hypothesis(dims=two_of_three)])).top_wrong is False
    assert grade_synthetic(report(hypotheses=[hypothesis(dims=one_of_three)])).top_wrong is True


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


def test_a_right_answer_with_no_evidence_scores_below_one_with_receipts() -> None:
    with_receipts = grade_synthetic(report())
    bare = grade_synthetic(report(hypotheses=[hypothesis(evidence=[], negation=None)]))
    # Outcome intact (0.75), receipts 0, not_checked 0.10, then 0.25 off for zero evidence.
    assert bare.outcome_score == pytest.approx(0.75)
    assert bare.components.receipts == 0.0
    assert bare.penalties.zero_evidence == -0.25
    assert bare.total == pytest.approx(0.60)
    assert bare.total < with_receipts.total


def test_an_invented_query_id_is_zero_evidence() -> None:
    invented = hypothesis(evidence=[Evidence(query_id="Q-made-up", summary="trust me")])
    result = grade_synthetic(report(hypotheses=[invented]))
    assert result.penalties.zero_evidence == -0.25
    assert result.components.receipts == 0.0


def test_a_query_the_server_rejected_is_no_receipt() -> None:
    log = [*good_log(), query_call("Q9", is_error=True)]
    cites_failure = hypothesis(evidence=[Evidence(query_id="Q9", summary="rows")])
    result = grade_synthetic(report(hypotheses=[cites_failure], tool_log=log))
    assert result.penalties.zero_evidence == -0.25


def test_the_zero_evidence_penalty_caps_at_a_half() -> None:
    bare = [hypothesis(evidence=[], negation=None, confidence="low") for _ in range(3)]
    result = grade_synthetic(report(hypotheses=bare))
    assert result.penalties.zero_evidence == -0.50


def test_a_missing_negation_costs_the_receipts_weight_and_nothing_else() -> None:
    """The R10 ablation, by construction: outcome unchanged, receipts gone."""
    full = grade_synthetic(report())
    no_negation = grade_synthetic(report(hypotheses=[hypothesis(negation=None)]))
    assert no_negation.outcome_score == full.outcome_score
    assert no_negation.components.receipts == 0.0
    assert no_negation.receipts_score == pytest.approx(WEIGHTS["not_checked"])
    assert no_negation.total == pytest.approx(full.total - WEIGHTS["receipts"])


def test_receipts_is_a_share_over_all_hypotheses() -> None:
    second = hypothesis(confidence="low", dims=WRONG_DIMS, negation=None)
    result = grade_synthetic(report(hypotheses=[hypothesis(), second]))
    assert result.components.receipts == pytest.approx(0.5)


def test_the_grader_and_the_validator_agree_on_what_a_citation_is() -> None:
    """A draft the validator accepts has full receipts; one it rejects for
    its citations does not."""
    accepted = report()
    assert validate_draft(accepted, accepted.tool_log, run_id=RUN_ID) == []
    assert grade_synthetic(accepted).components.receipts == 1.0

    same_query_twice = hypothesis(negation=Evidence(query_id="Q1", summary="same rows"))
    rejected = report(hypotheses=[same_query_twice])
    issues = validate_draft(rejected, rejected.tool_log, run_id=RUN_ID)
    assert [issue.code for issue in issues] == ["partial"]
    assert grade_synthetic(rejected).components.receipts == 0.0


def test_validation_failed_costs_a_quarter() -> None:
    result = grade_synthetic(report(validation_failed=True))
    assert result.penalties.validation_failed == -0.25
    assert result.total == pytest.approx(0.75)


def test_the_total_floors_at_minus_one() -> None:
    bare = [
        hypothesis(evidence=[], negation=None, dims=WRONG_DIMS, slow_or_failing_span="db.query")
        for _ in range(3)
    ]
    result = grade_synthetic(
        report(
            incident_present=False,
            hypotheses=bare,
            onset_estimate=None,
            not_checked=[],
            validation_failed=True,
        )
    )
    assert result.before_penalties == pytest.approx(0.0)
    assert result.penalties.total == pytest.approx(-1.25)
    assert result.total == -1.0


# --------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------


def test_a_quiet_report_on_a_control_is_perfect() -> None:
    result = grade_synthetic(control_report())
    assert result.before_penalties == pytest.approx(1.0)
    assert result.total == pytest.approx(1.0)
    assert result.jaccard is None


def test_a_confident_hypothesis_on_a_control_scores_at_most_a_quarter() -> None:
    result = grade_synthetic(
        control_report(incident_present=True, hypotheses=[hypothesis(dims=WRONG_DIMS)])
    )
    assert result.components.dims == 0.0
    assert result.components.span == 0.0
    assert result.components.onset == 0.0
    assert result.components.incident == 0.0
    assert result.before_penalties == pytest.approx(0.25)
    assert result.penalties.calibration == -0.50
    assert result.total == pytest.approx(-0.25)
    assert result.total <= 0.25


def test_a_low_hypothesis_on_a_control_keeps_the_outcome_components() -> None:
    hedge = hypothesis(dims=WRONG_DIMS, confidence="low")
    result = grade_synthetic(control_report(hypotheses=[hedge]))
    assert result.components.dims == 1.0
    assert result.components.span == 1.0
    assert result.components.onset == 1.0
    assert result.penalties.calibration == -0.10
    assert result.total == pytest.approx(0.90)


@pytest.mark.parametrize("stop_reason", ["schema", "call_cap", "wall_cap", "model_stopped"])
def test_a_control_run_that_filed_nothing_earns_no_restraint(stop_reason: str) -> None:
    """The empty defaults say incident_present=False, which is what a control
    wants to hear, but nobody said it."""
    result = grade_synthetic(control_report(stop_reason=stop_reason, baseline_evidence=[]))
    assert result.components.dims == 0.0
    assert result.components.span == 0.0
    assert result.components.onset == 0.0
    assert result.components.incident == 0.0
    assert result.outcome_score == 0.0
    assert result.jaccard is None
    assert result.penalties.calibration == 0.0
    assert any(
        note.startswith("not filed: the run stopped on " + stop_reason) for note in result.notes
    )


def test_a_schema_stop_on_a_control_scores_the_validation_penalty_and_nothing_else() -> None:
    result = grade_synthetic(
        control_report(
            stop_reason="schema",
            validation_failed=True,
            baseline_evidence=[],
            not_checked=[],
        )
    )
    assert result.before_penalties == 0.0
    assert result.total == pytest.approx(-0.25)


def test_an_incident_run_that_filed_nothing_scores_as_before() -> None:
    """On an incident scenario the defaults were already worth nothing; the
    only change is the note."""
    result = grade_synthetic(
        report(stop_reason="wall_cap", incident_present=False, hypotheses=[], onset_estimate=None)
    )
    assert result.outcome_score == 0.0
    assert result.components.incident == 0.0
    assert any(note.startswith("not filed") for note in result.notes)


def test_a_filed_control_report_still_scores_restraint() -> None:
    result = grade_synthetic(control_report(stop_reason="report"))
    assert result.components.incident == 1.0
    assert result.components.dims == 1.0


def test_a_quiet_report_with_no_baseline_query_has_no_receipts() -> None:
    result = grade_synthetic(control_report(baseline_evidence=[]))
    assert result.components.receipts == 0.0
    assert any("baseline" in note for note in result.notes)


# --------------------------------------------------------------------------
# Dims
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ({"cart.size": "9"}, 1.0),
        ({"cart.size": ">=8"}, 1.0),
        ({"cart.size": " >= 8 "}, 1.0),
        ({"cart.size": "8"}, 1.0),
        ({"cart.size": "3"}, 0.0),
        ({"cart.size": ">8"}, 0.0),
        ({"cart.size": "large"}, 0.0),
    ],
)
def test_a_range_dim_is_satisfied_by_a_value_inside_it(
    reported: dict[str, str], expected: float
) -> None:
    assert dims_jaccard(reported, {"cart.size": ">=8"}) == expected


def test_jaccard_counts_missing_and_spurious_pairs_the_same() -> None:
    two_of_three = {k: v for k, v in list(TRUE_DIMS.items())[:2]}
    assert dims_jaccard(two_of_three, TRUE_DIMS) == pytest.approx(2 / 3)
    spurious = {**TRUE_DIMS, "name": "payments.charge"}
    assert dims_jaccard(spurious, TRUE_DIMS) == pytest.approx(3 / 4)
    wrong_value = {**TRUE_DIMS, "cloud.region": "eu-west-1"}
    assert dims_jaccard(wrong_value, TRUE_DIMS) == pytest.approx(2 / 4)
    assert dims_jaccard({}, TRUE_DIMS) == 0.0
    assert dims_jaccard({}, {}) == 1.0


def test_no_hypothesis_on_an_incident_scores_zero_on_dims_and_span() -> None:
    result = grade_synthetic(report(hypotheses=[]))
    assert result.components.dims == 0.0
    assert result.components.span == 0.0
    assert result.jaccard is None
    assert result.penalties.calibration == 0.0


# --------------------------------------------------------------------------
# R10: ground_truth.equivalent_dims
# --------------------------------------------------------------------------

DEPENDENCY = load_scenario("dependency-inventory-db-timeouts")


def dependency_report(dims: dict[str, str]) -> Report:
    return Report.model_validate(
        {
            "run_id": "run-dependency",
            "scenario_id": DEPENDENCY.id,
            "provider": "test",
            "model": "test-model",
            "incident_present": True,
            "hypotheses": [
                {
                    "claim": "inventory-db timed out",
                    "dims": dims,
                    "slow_or_failing_span": "db.query",
                    "confidence": "high",
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("dims", "expected"),
    [
        ({"name": "db.query"}, 1.0),
        # `error: true` is a symptom of this fault, so it is neutral and the
        # score is the same as naming the span alone.
        ({"name": "db.query", "error": "true"}, 1.0),
        ({"service.component": "inventory-db"}, 1.0),
    ],
)
def test_the_grader_takes_the_best_of_root_cause_dims_and_equivalent_dims(
    dims: dict[str, str], expected: float
) -> None:
    result = grade(dependency_report(dims), DEPENDENCY, window_start=WINDOW_START)
    assert result.jaccard == pytest.approx(expected)


def test_name_as_a_dim_on_payments_still_scores_zero_since_it_names_no_true_dim() -> None:
    """payments-stripe-v251-uswest declares no equivalent_dims, so
    `name: payments.charge` is not a selector there. It is a symptom of the
    fault, so it costs nothing, and it earns nothing: the report named none
    of the three true dims, so dims is 0 out of 3."""
    result = grade_synthetic(report(hypotheses=[hypothesis(dims={"name": "payments.charge"})]))
    assert result.components.dims == 0.0


# --------------------------------------------------------------------------
# EDW-1359: symptom dims are neutral
# --------------------------------------------------------------------------

DEPLOY = load_scenario("deploy-regression-v260")
EXCEPTIONS = load_scenario("error-surge-exceptions")
ADYEN = load_scenario("checkout-error-surge-adyen")


def dims_score(scenario: Scenario, dims: dict[str, str]) -> float:
    """The dims component of a report whose top hypothesis names `dims`."""
    top = {
        "claim": "the top hypothesis",
        "dims": dims,
        "slow_or_failing_span": scenario.ground_truth.slow_or_failing_span,
        "confidence": "high",
    }
    filed = Report.model_validate(
        {
            "run_id": "run-symptom",
            "scenario_id": scenario.id,
            "provider": "test",
            "model": "test-model",
            "incident_present": True,
            "hypotheses": [top],
        }
    )
    return grade(filed, scenario, window_start=WINDOW_START).components.dims


@pytest.mark.parametrize(
    ("scenario", "dims", "expected"),
    [
        # The 2026-09-04 run this rule came from: every pair is true of the
        # requests the fault touched, and one of them is the cause.
        (
            DEPENDENCY,
            {"service.component": "inventory-db", "name": "db.query", "error.type": "timeout"},
            1.0,
        ),
        # The same answer through the declared equivalent selector.
        (DEPENDENCY, {"name": "db.query"}, 1.0),
        # A symptom pair rescues nothing. Payments has three true dims and
        # this report names none of them.
        (PAYMENTS, {"name": "payments.charge"}, 0.0),
        # One of three, with the span neutral: 1 matched over 3 in the union.
        (PAYMENTS, {"payment.provider": "stripe", "name": "payments.charge"}, 1 / 3),
        # A wrong value on a true key is not a symptom and still costs.
        (PAYMENTS, {"payment.provider": "paypal"}, 0.0),
        # deploy-regression-v260 adds latency and fails nothing, so `error`
        # is not among its symptoms and the pair is a mismatch: 1 over 2.
        (DEPLOY, {"deployment.version": "2.6.0", "error": "true"}, 0.5),
        # The exception type the effect attaches is a symptom, as is `error`.
        (
            EXCEPTIONS,
            {"payment.provider": "adyen", "error": "true", "exception.type": "ProviderDeclined"},
            1.0,
        ),
        # The status code the root span carries on a failure is a symptom too.
        (ADYEN, {"payment.provider": "adyen", "http.status_code": "500"}, 1.0),
        # `500.0` is not `500`: no number is normalised, so this pair is in
        # the union and the score is 1 over 2.
        (ADYEN, {"payment.provider": "adyen", "http.status_code": "500.0"}, 0.5),
        # Honeycomb renders the error column as `true` and a report may write
        # it back as `True`. Same claim, so still neutral.
        (ADYEN, {"payment.provider": "adyen", "error": "True"}, 1.0),
        # A red herring dressed in the fault's symptoms is still the herring.
        (
            ADYEN,
            {"cloud.region": "us-east-1", "error": "true", "name": "payments.charge"},
            0.0,
        ),
        # A latency fault sets no exact duration, so a duration is never a
        # symptom and this claim stays in the union: 1 over 2.
        (
            load_scenario("trigger-checkout-latency"),
            {"cart.size": ">=8", "duration_ms": ">1000"},
            0.5,
        ),
    ],
)
def test_a_symptom_pair_is_neither_right_nor_wrong(
    scenario: Scenario, dims: dict[str, str], expected: float
) -> None:
    assert dims_score(scenario, dims) == pytest.approx(expected)


def _herring_pair(scenario: Scenario) -> dict[str, str]:
    """One clause from the scenario's first red herring, or nothing."""
    for herring in scenario.red_herrings:
        for key, value in herring.where.items():
            if key != "service.component":
                return {key: value}
    return {}


@pytest.mark.parametrize(
    "scenario",
    [s for s in load_all() if s.ground_truth.incident_present],
    ids=lambda s: s.id,
)
def test_deleting_the_symptom_pairs_leaves_the_score_where_it_was(scenario: Scenario) -> None:
    """The invariant in `evals/grader.md`: a neutral pair leaves the numerator
    and the union both, so a report scores what it would score without its
    symptom pairs. A pair a scenario declares as an equivalent selector is
    scored as that selector against that candidate, so it is not deleted."""
    truth = scenario.ground_truth
    symptoms = scenario.symptom_dims
    reported = {
        **_herring_pair(scenario),
        **truth.root_cause_dims,
        "http.route": "/checkout/gift",
        **symptoms,
    }
    selectors = set(truth.root_cause_dims) | {k for e in truth.equivalent_dims for k in e}
    stripped = {
        key: value
        for key, value in reported.items()
        if not (key in symptoms and value == symptoms[key] and key not in selectors)
    }
    assert stripped != reported
    assert dims_score(scenario, reported) == pytest.approx(dims_score(scenario, stripped))


def test_a_control_has_no_symptoms_and_scores_as_before() -> None:
    assert CONTROL.symptom_dims == {}
    quiet = grade_synthetic(control_report())
    assert quiet.components.dims == 1.0
    loud = grade_synthetic(
        control_report(incident_present=True, hypotheses=[hypothesis(dims={"name": "db.query"})])
    )
    assert loud.components.dims == 0.0
    assert loud.jaccard == 0.0


def test_dims_jaccard_still_takes_two_arguments() -> None:
    assert dims_jaccard({"name": "db.query"}, {"name": "db.query"}) == 1.0
    assert dims_jaccard({"name": "db.query"}, TRUE_DIMS) == 0.0


def test_a_neutral_pair_leaves_the_union_alone() -> None:
    neutral = {"name": "payments.charge", "error": "true"}
    assert dims_jaccard({"payment.provider": "stripe"}, TRUE_DIMS, neutral) == pytest.approx(1 / 3)
    both = {"payment.provider": "stripe", "name": "payments.charge"}
    assert dims_jaccard(both, TRUE_DIMS, neutral) == pytest.approx(1 / 3)
    # A symptom key that is also a truth key is scored as truth, not skipped.
    assert dims_jaccard({"name": "db.query"}, {"name": "db.query"}, neutral) == 1.0
    # A symptom key carrying some other value is a plain mismatch.
    assert dims_jaccard({"error": "false"}, TRUE_DIMS, neutral) == 0.0


# --------------------------------------------------------------------------
# Onset
# --------------------------------------------------------------------------


@pytest.mark.parametrize("offset_min", [0, 2.9, -2.9, 3])
def test_an_onset_within_three_minutes_scores(offset_min: float) -> None:
    result = grade_synthetic(report(onset_estimate=TRUE_ONSET + timedelta(minutes=offset_min)))
    assert result.components.onset == 1.0


@pytest.mark.parametrize("offset_min", [3.1, -10])
def test_an_onset_outside_three_minutes_does_not(offset_min: float) -> None:
    result = grade_synthetic(report(onset_estimate=TRUE_ONSET + timedelta(minutes=offset_min)))
    assert result.components.onset == 0.0


def test_a_naive_onset_is_read_as_utc() -> None:
    result = grade_synthetic(report(onset_estimate=TRUE_ONSET.replace(tzinfo=None)))
    assert result.components.onset == 1.0


def test_without_a_window_start_the_onset_cannot_pass() -> None:
    result = grade(report(), PAYMENTS, window_start=None)
    assert result.components.onset == 0.0
    assert any("window start unknown" in note for note in result.notes)


def test_the_window_start_comes_from_the_run_manifest() -> None:
    assert window_start_for("run-4155490e2a44", RUNS_DIR) == datetime(
        2026, 9, 3, 2, 37, 20, tzinfo=UTC
    )
    assert window_start_for("run-nowhere", RUNS_DIR) is None


# --------------------------------------------------------------------------
# Not checked
# --------------------------------------------------------------------------


def test_a_not_checked_entry_that_was_queried_zeroes_the_component() -> None:
    honest = grade_synthetic(report())
    assert honest.components.not_checked == 1.0
    dishonest = grade_synthetic(report(not_checked=["cloud.region", "customer.id"]))
    assert dishonest.components.not_checked == 0.0
    assert dishonest.total == pytest.approx(honest.total - WEIGHTS["not_checked"])


def test_an_empty_not_checked_list_scores_zero() -> None:
    assert grade_synthetic(report(not_checked=[])).components.not_checked == 0.0


# --------------------------------------------------------------------------
# Process
# --------------------------------------------------------------------------


def test_process_fields_are_copied_through_not_recomputed() -> None:
    result = grade_synthetic(report(cost_usd=99.0, tokens_in=7, tokens_out=3, wall_s=1.5))
    assert result.process.cost_usd == 99.0
    assert result.process.tokens_in == 7
    assert result.process.tokens_out == 3
    assert result.process.wall_s == 1.5
    assert result.process.tool_calls == 4


def test_honeycomb_passes_a_run_that_only_oriented_and_queried_a_p99() -> None:
    log = [ToolCall(name="get_workspace_context", args={}), query_call("Q1")]
    result = honeycomb_process_score(report(tool_log=log))
    # Required 0.30, one of two patterns 0.125, no anti-pattern 0.20, ordered 0.15.
    assert result.score == pytest.approx(0.775)
    assert result.passed
    assert result.score >= 0.6
    assert result.recommended_missing == ["run_bubbleup", "get_trace"]


def test_honeycomb_scores_process_and_never_reads_the_answer() -> None:
    right = grade_synthetic(report())
    wrong = grade_synthetic(
        report(hypotheses=[hypothesis(dims=WRONG_DIMS, slow_or_failing_span="db.query")])
    )
    assert right.process.honeycomb_process_score == wrong.process.honeycomb_process_score
    assert wrong.total < right.total


def test_honeycomb_ordering_wants_context_before_the_first_query() -> None:
    backwards = [query_call("Q1"), ToolCall(name="get_workspace_context", args={})]
    result = honeycomb_process_score(report(tool_log=backwards))
    assert result.ordering_ok is False
    assert result.score == pytest.approx(0.775 - 0.15)


def test_honeycomb_anti_pattern_is_an_average_over_duration() -> None:
    log = [
        ToolCall(name="get_workspace_context", args={}),
        query_call("Q1", calculations=[{"op": "AVG", "column": "duration_ms"}]),
    ]
    result = honeycomb_process_score(report(tool_log=log))
    assert result.anti_pattern_violations
    assert result.score == pytest.approx(0.30 + 0.0 + 0.0 + 0.15)


# --------------------------------------------------------------------------
# Determinism and plumbing
# --------------------------------------------------------------------------


def test_the_same_report_and_scenario_always_grade_the_same() -> None:
    first = grade_synthetic(report())
    second = grade_synthetic(report())
    assert first == second
    assert first.model_dump() == second.model_dump()


def test_a_report_for_another_scenario_is_refused() -> None:
    with pytest.raises(ValueError, match="not 'control-quiet'"):
        grade(report(), CONTROL, window_start=WINDOW_START)


def test_weighted_components_sum_to_before_penalties() -> None:
    result = grade_synthetic(report(hypotheses=[hypothesis(dims=WRONG_DIMS)]))
    total = sum(getattr(result.weighted, name) for name in WEIGHTS)
    assert result.before_penalties == pytest.approx(total)
    assert result.outcome_score + result.receipts_score == pytest.approx(total)


def test_arithmetic_writes_out_the_sum() -> None:
    text = arithmetic(grade_synthetic(report()))
    assert "dims 1.000x0.35" in text
    assert "total 1.000" in text


# --------------------------------------------------------------------------
# The live reports
# --------------------------------------------------------------------------

# Totals by hand. Payments truth: three dims, payments.charge, onset 02:47:20.
# The onset is the manifest's window start plus ten minutes.
LIVE: dict[str, float] = {
    # All three dims, the span, a real negation; onset named as the window
    # start, ten minutes early. 0.35+0.15+0.15+0+0.15+0.10.
    "run-4155490e2a44/report.json": 0.90,
    # Two of three dims (2/3 x 0.35), everything else right; validation_failed
    # was set by an older validator on an entry the current one accepts.
    # 0.2333+0.15+0.15+0.10+0.15+0.10 - 0.25.
    "run-4155490e2a44/report-2.json": 0.633333,
    # One true dim plus the span name as a dim. The span is a symptom of the
    # fault, so it is neutral and the Jaccard is 1/3 over the two true dims
    # the report never named: still under the 0.5 line, so wrong and high.
    # 0.1167+0.15+0.15+0.10+0.15+0.10 - 0.50.
    "run-4155490e2a44/report-3.json": 0.266667,
    # All three dims at low confidence, root span named instead of the child,
    # negation is its own evidence, one not-checked entry names queried spans.
    # 0.35+0+0.15+0.10+0+0 - 0.25 validation - 0.10 hedged right.
    "run-4155490e2a44/report-4.json": 0.25,
    # The confident-wrong control. 0+0+0+0+0.15+0.10 - 0.50.
    "run-ebc9c1e4be3d/report.json": -0.25,
    "run-ebc9c1e4be3d/report-2.json": 1.0,
    "run-ebc9c1e4be3d/report-3.json": 1.0,
    "run-ebc9c1e4be3d/report-4.json": 1.0,
}


@pytest.mark.parametrize(("relative", "expected"), sorted(LIVE.items()))
def test_a_live_report_grades_to_the_number_in_the_readme(relative: str, expected: float) -> None:
    result = grade_file(FIXTURES / relative, runs_dir=RUNS_DIR)
    assert result.total == pytest.approx(expected, abs=1e-6)


def test_the_live_confident_wrong_control_walks_as_expected() -> None:
    result = grade_file(FIXTURES / "run-ebc9c1e4be3d/report.json", runs_dir=RUNS_DIR)
    assert result.components.model_dump() == {
        "dims": 0.0,
        "span": 0.0,
        "incident": 0.0,
        "onset": 0.0,
        "receipts": 1.0,
        "not_checked": 1.0,
    }
    assert result.outcome_score == 0.0
    assert result.receipts_score == pytest.approx(0.25)
    assert result.top_confidence == "high"
    assert result.top_wrong is True
    assert result.penalties.calibration == -0.50
    assert result.total == pytest.approx(-0.25)
    assert result.total < 0


def test_every_live_report_passes_honeycombs_eval() -> None:
    """The contrast the issue is for: their score cannot tell these apart."""
    for relative in LIVE:
        result = grade_file(FIXTURES / relative, runs_dir=RUNS_DIR)
        assert result.process.honeycomb_process_passed, relative


def test_live_reports_carry_no_team_slug() -> None:
    for path in FIXTURES.glob("*/report*.json"):
        text = path.read_text()
        assert "ui.honeycomb.io/team/" in text
        assert "joymath" not in text
        assert "edwin-knuth" not in text


def test_live_reports_still_load_as_reports() -> None:
    for path in FIXTURES.glob("*/report*.json"):
        assert load_report(path).run_id == path.parent.name


def test_the_cli_prints_the_arithmetic(capsys: pytest.CaptureFixture[str]) -> None:
    path = FIXTURES / "run-ebc9c1e4be3d/report.json"
    assert main([str(path), "--runs-dir", str(RUNS_DIR)]) == 0
    out = capsys.readouterr().out
    assert "total -0.250" in out
    assert "calibration -0.50" in out


def test_the_cli_can_print_json(capsys: pytest.CaptureFixture[str]) -> None:
    path = FIXTURES / "run-4155490e2a44/report.json"
    assert main([str(path), "--runs-dir", str(RUNS_DIR), "--json"]) == 0
    assert '"total": 0.9' in capsys.readouterr().out
