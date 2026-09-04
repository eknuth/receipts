"""Scenario files: loading, validation, and agreement with the generator.

Nothing here touches the network. The point is that a scenario file which
would produce a run nobody can verify fails at load time instead.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from gen import topology
from gen.scenario import (
    AFFECTED_SHARE_TOLERANCE,
    SCENARIO_DIR,
    Scenario,
    available_scenarios,
    load_all,
    load_scenario,
    load_scenario_file,
)

REQUIRED_SCENARIOS = {
    "payments-stripe-v251-uswest",
    "control-quiet",
    "checkout-error-surge-adyen",
    "deploy-regression-v260",
    "dependency-inventory-db-timeouts",
    "trigger-checkout-latency",
    "control-noisy",
    "herring-region-vs-version",
    "herring-customer-whale",
    "error-surge-exceptions",
}

VALID = """
id: sample
narrative: a sample
baseline: {rps: 10, minutes: 20}
fault:
  onset_min: 10
  where: {payment.provider: "stripe"}
  effect: {span: "payments.charge", latency_add_ms: 500}
ground_truth:
  incident_present: true
  root_cause_dims: {payment.provider: "stripe"}
  slow_or_failing_span: payments.charge
  affected_share: 0.6
"""


def write(tmp_path: Path, body: str, name: str = "sample.yml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return path


# --------------------------------------------------------------------------
# The files on disk
# --------------------------------------------------------------------------


def test_every_scenario_on_disk_loads() -> None:
    scenarios = load_all()
    assert {s.id for s in scenarios} == set(available_scenarios())
    assert REQUIRED_SCENARIOS <= {s.id for s in scenarios}


def test_load_all_returns_ten_scenarios() -> None:
    """R1 through R5 add up to ten: four from R1-R4 plus six from R5."""
    assert len(load_all()) == 10


def test_scenario_dir_has_no_stray_yaml_extension() -> None:
    """The loader globs *.yml, so a *.yaml file would be invisible."""
    assert list(SCENARIO_DIR.glob("*.yaml")) == []


@pytest.mark.parametrize("scenario_id", sorted(available_scenarios()))
def test_ground_truth_affected_share_matches_the_weights(scenario_id: str) -> None:
    """The share written in the file is the share the dimension weights produce.

    Tighter than a scenario's own load-time check would need
    (`AFFECTED_SHARE_TOLERANCE`, the same tolerance the model itself enforces)
    rather than an exact match: a plain value's weight product is a clean
    decimal, but a range clause like `cart.size: ">=8"` sums a tail of a
    geometric series, which never lands on the three decimals a file writes.
    """
    scenario = load_scenario(scenario_id)
    if scenario.fault is None:
        assert scenario.ground_truth.affected_share is None
        return
    expected = scenario.ground_truth.affected_share
    assert expected is not None
    assert scenario.expected_affected_share() == pytest.approx(
        expected, abs=AFFECTED_SHARE_TOLERANCE
    )


def test_exactly_two_controls_and_eight_faults() -> None:
    scenarios = load_all()
    controls = [s for s in scenarios if not s.ground_truth.incident_present]
    assert {s.id for s in controls} == {"control-quiet", "control-noisy"}
    assert len(scenarios) - len(controls) == 8


def test_every_scenario_has_at_least_one_red_herring() -> None:
    """A scenario with nothing else moving would be a lookup, not an investigation."""
    for scenario in load_all():
        assert scenario.red_herrings, scenario.id


def test_red_herrings_start_before_the_fault() -> None:
    """A red herring that starts at onset would be indistinguishable from the fault."""
    for scenario in load_all():
        if scenario.fault is None:
            continue
        for herring in scenario.red_herrings:
            assert herring.onset_min < scenario.fault.onset_min, scenario.id


def test_load_scenario_names_the_alternatives_when_missing() -> None:
    with pytest.raises(FileNotFoundError, match="control-quiet"):
        load_scenario("no-such-scenario")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_valid_sample_loads(tmp_path: Path) -> None:
    scenario = load_scenario_file(write(tmp_path, VALID))
    assert scenario.id == "sample"
    assert scenario.baseline.request_count == 12000
    assert scenario.onset_min == 10


def test_id_must_match_the_file_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must match the file name"):
        load_scenario_file(write(tmp_path, VALID, name="other.yml"))


def test_unknown_span_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown span"):
        Scenario.model_validate(
            {
                "id": "s",
                "narrative": "n",
                "baseline": {"rps": 1, "minutes": 1},
                "fault": {
                    "onset_min": 0,
                    "where": {"payment.provider": "stripe"},
                    "effect": {"span": "payments.refund", "latency_add_ms": 1},
                },
                "ground_truth": {"incident_present": True},
            }
        )


def test_an_effect_that_does_nothing_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace(
        'effect: {span: "payments.charge", latency_add_ms: 500}',
        'effect: {span: "payments.charge"}',
    )
    with pytest.raises(ValidationError, match="must add latency"):
        load_scenario_file(write(tmp_path, body))


def test_unknown_dimension_in_where_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace('where: {payment.provider: "stripe"}', 'where: {kubernetes.pod: "a"}')
    with pytest.raises(ValidationError, match="does not emit"):
        load_scenario_file(write(tmp_path, body))


def test_dimension_value_the_scenario_never_emits_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace(
        'where: {payment.provider: "stripe"}', 'where: {payment.provider: "klarna"}'
    )
    with pytest.raises(ValidationError, match="klarna"):
        load_scenario_file(write(tmp_path, body))


def test_a_dimension_override_changes_what_where_may_name() -> None:
    """v2.6.0 exists only because deploy-regression-v260 declares it."""
    assert "2.6.0" not in topology.DIMENSION_WEIGHTS["deployment.version"]
    scenario = load_scenario("deploy-regression-v260")
    assert "2.6.0" in topology.dimension_weights_for(scenario)["deployment.version"]


def test_unknown_dimension_override_is_rejected(tmp_path: Path) -> None:
    body = VALID + "dimensions:\n  kubernetes.pod: {a: 1.0}\n"
    with pytest.raises(ValidationError, match="unknown dimension"):
        load_scenario_file(write(tmp_path, body))


def test_incident_without_a_fault_is_rejected(tmp_path: Path) -> None:
    body = "\n".join(
        line
        for line in textwrap.dedent(VALID).splitlines()
        if not line.startswith(("fault:", "  onset_min", "  where", "  effect"))
    )
    with pytest.raises(ValidationError, match="but there is no fault"):
        load_scenario_file(write(tmp_path, body))


def test_root_cause_dims_must_repeat_the_fault(tmp_path: Path) -> None:
    body = VALID.replace(
        'root_cause_dims: {payment.provider: "stripe"}',
        'root_cause_dims: {payment.provider: "adyen"}',
    )
    with pytest.raises(ValidationError, match="must repeat fault.where"):
        load_scenario_file(write(tmp_path, body))


def test_slow_or_failing_span_must_name_the_fault_span(tmp_path: Path) -> None:
    body = VALID.replace("slow_or_failing_span: payments.charge", "slow_or_failing_span: db.query")
    with pytest.raises(ValidationError, match="must name fault.effect.span"):
        load_scenario_file(write(tmp_path, body))


def test_an_incident_needs_an_affected_share(tmp_path: Path) -> None:
    body = VALID.replace("  affected_share: 0.6\n", "")
    with pytest.raises(ValidationError, match="needs ground_truth.affected_share"):
        load_scenario_file(write(tmp_path, body))


def test_a_control_may_not_carry_a_fault(tmp_path: Path) -> None:
    body = VALID.replace("incident_present: true", "incident_present: false")
    with pytest.raises(ValidationError, match="but a fault is defined"):
        load_scenario_file(write(tmp_path, body))


def test_an_unknown_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="extra_credit"):
        load_scenario_file(write(tmp_path, VALID + "extra_credit: yes\n"))


def test_a_scenario_file_must_be_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="YAML mapping"):
        load_scenario_file(write(tmp_path, "- one\n- two\n"))


# --------------------------------------------------------------------------
# Files that load but could not be verified are rejected
# --------------------------------------------------------------------------


def _payments_dict() -> dict:
    import yaml

    return yaml.safe_load((SCENARIO_DIR / "payments-stripe-v251-uswest.yml").read_text())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["ground_truth"].__setitem__("affected_share", 0.5), "weights give"),
        (lambda d: d["ground_truth"].__setitem__("affected_share", 0.0), "weights give"),
        (lambda d: d["fault"].__setitem__("onset_min", 0), "strictly inside"),
        (lambda d: d["fault"].__setitem__("onset_min", 20), "strictly inside"),
        (lambda d: d["fault"].__setitem__("onset_min", 25), "strictly inside"),
        (lambda d: d["red_herrings"][0].__setitem__("onset_min", 10), "older than"),
        (lambda d: d["red_herrings"][0].__setitem__("onset_min", 99), "past the end"),
        (lambda d: d["baseline"].update({"rps": 0.001, "minutes": 1}), "zero requests"),
        (
            lambda d: d["red_herrings"].append(
                {"where": d["fault"]["where"], "effect": dict(d["fault"]["effect"])}
            ),
            "same population and span",
        ),
    ],
)
def test_a_scenario_the_verifier_could_not_judge_is_rejected(mutate, message: str) -> None:
    data = _payments_dict()
    mutate(data)
    with pytest.raises(ValueError, match=message):
        Scenario.model_validate(data)


def test_a_control_may_not_carry_an_affected_share() -> None:
    import yaml

    data = yaml.safe_load((SCENARIO_DIR / "control-quiet.yml").read_text())
    data["ground_truth"]["affected_share"] = 0.4
    with pytest.raises(ValueError, match="no affected_share"):
        Scenario.model_validate(data)


# --------------------------------------------------------------------------
# R5: timeouts, exceptions, sigma_scale, bursts, ranges, service.component,
# pinned customers, and triggers
# --------------------------------------------------------------------------


def test_effect_timeout_ms_requires_a_positive_error_rate(tmp_path: Path) -> None:
    body = VALID.replace(
        'effect: {span: "payments.charge", latency_add_ms: 500}',
        'effect: {span: "payments.charge", latency_add_ms: 500, timeout_ms: 5000}',
    )
    with pytest.raises(ValidationError, match="timeout_ms requires error_rate"):
        load_scenario_file(write(tmp_path, body))


def test_effect_timeout_ms_and_latency_add_ms_are_exclusive(tmp_path: Path) -> None:
    body = VALID.replace(
        'effect: {span: "payments.charge", latency_add_ms: 500}',
        'effect: {span: "payments.charge", latency_add_ms: 500, error_rate: 0.1, timeout_ms: 5000}',
    )
    with pytest.raises(ValidationError, match="exclusive"):
        load_scenario_file(write(tmp_path, body))


def test_effect_error_type_requires_a_positive_error_rate(tmp_path: Path) -> None:
    body = VALID.replace(
        'effect: {span: "payments.charge", latency_add_ms: 500}',
        'effect: {span: "payments.charge", latency_add_ms: 500, error_type: "timeout"}',
    )
    with pytest.raises(ValidationError, match="error_type requires error_rate"):
        load_scenario_file(write(tmp_path, body))


def test_effect_exception_requires_a_positive_error_rate(tmp_path: Path) -> None:
    body = VALID.replace(
        'effect: {span: "payments.charge", latency_add_ms: 500}',
        'effect: {span: "payments.charge", latency_add_ms: 500, '
        'exception: {type: "ProviderDeclined", message: "declined"}}',
    )
    with pytest.raises(ValidationError, match="exception requires error_rate"):
        load_scenario_file(write(tmp_path, body))


def test_a_timeout_with_an_error_rate_and_error_type_loads(tmp_path: Path) -> None:
    body = VALID.replace(
        'effect: {span: "payments.charge", latency_add_ms: 500}',
        'effect: {span: "payments.charge", error_rate: 0.4, timeout_ms: 5000, '
        'error_type: "timeout"}',
    )
    scenario = load_scenario_file(write(tmp_path, body))
    assert scenario.fault is not None
    assert scenario.fault.effect.timeout_ms == 5000
    assert scenario.fault.effect.error_type == "timeout"


def test_sigma_scale_defaults_to_one() -> None:
    scenario = load_scenario("payments-stripe-v251-uswest")
    assert scenario.baseline.sigma_scale == 1.0


def test_sigma_scale_must_be_positive(tmp_path: Path) -> None:
    body = VALID.replace(
        "baseline: {rps: 10, minutes: 20}", "baseline: {rps: 10, minutes: 20, sigma_scale: 0}"
    )
    with pytest.raises(ValidationError):
        load_scenario_file(write(tmp_path, body))


def test_a_red_herring_burst_may_not_run_past_the_window() -> None:
    data = _payments_dict()
    data["red_herrings"][0]["duration_min"] = 25.0  # onset 0 + 25 > the 20 minute window
    with pytest.raises(ValueError, match="past the end"):
        Scenario.model_validate(data)


def test_a_red_herring_burst_inside_the_window_loads() -> None:
    data = _payments_dict()
    data["red_herrings"][0]["duration_min"] = 5.0
    scenario = Scenario.model_validate(data)
    assert scenario.red_herrings[0].duration_min == 5.0


def test_customer_id_pins_must_sum_to_less_than_one(tmp_path: Path) -> None:
    body = VALID + 'dimensions:\n  customer.id: {"cust-00001": 0.6, "cust-00002": 0.5}\n'
    with pytest.raises(ValidationError, match="leaves nothing for the rest"):
        load_scenario_file(write(tmp_path, body))


def test_a_where_clause_on_customer_id_must_be_pinned_first(tmp_path: Path) -> None:
    body = VALID.replace(
        'where: {payment.provider: "stripe"}', 'where: {customer.id: "cust-00007"}'
    ).replace(
        'effect: {span: "payments.charge", latency_add_ms: 500}',
        'effect: {span: "payments.charge", error_rate: 0.1}',
    )
    with pytest.raises(ValidationError, match="not pinned"):
        load_scenario_file(write(tmp_path, body))


def test_a_where_clause_on_a_pinned_customer_id_loads(tmp_path: Path) -> None:
    body = (
        VALID.replace('where: {payment.provider: "stripe"}', 'where: {customer.id: "cust-00007"}')
        .replace(
            'effect: {span: "payments.charge", latency_add_ms: 500}',
            'effect: {span: "payments.charge", error_rate: 0.1}',
        )
        .replace(
            'root_cause_dims: {payment.provider: "stripe"}',
            'root_cause_dims: {customer.id: "cust-00007"}',
        )
        .replace("affected_share: 0.6", "affected_share: 0.15")
        + 'dimensions:\n  customer.id: {"cust-00007": 0.15}\n'
    )
    scenario = load_scenario_file(write(tmp_path, body))
    assert scenario.fault is not None
    assert scenario.fault.where == {"customer.id": "cust-00007"}


def test_a_where_clause_on_service_component_must_name_the_effect_spans_service(
    tmp_path: Path,
) -> None:
    body = VALID.replace(
        'where: {payment.provider: "stripe"}', 'where: {service.component: "gateway"}'
    )
    with pytest.raises(ValidationError, match="not the service that runs"):
        load_scenario_file(write(tmp_path, body))


def test_a_where_clause_on_the_effect_spans_own_service_component_loads(tmp_path: Path) -> None:
    body = (
        VALID.replace(
            'where: {payment.provider: "stripe"}', 'where: {service.component: "payments"}'
        )
        .replace(
            'root_cause_dims: {payment.provider: "stripe"}',
            'root_cause_dims: {service.component: "payments"}',
        )
        .replace("affected_share: 0.6", "affected_share: 1.0")
    )
    scenario = load_scenario_file(write(tmp_path, body))
    assert scenario.fault is not None
    assert scenario.ground_truth.affected_share == 1.0


def test_a_cart_size_range_on_a_span_without_cart_size_is_rejected(tmp_path: Path) -> None:
    """cart.size is not on payments.charge, so the verifier could not filter for it."""
    body = VALID.replace('where: {payment.provider: "stripe"}', 'where: {cart.size: ">=8"}')
    with pytest.raises(ValidationError, match="does not carry it"):
        load_scenario_file(write(tmp_path, body))


def test_a_cart_size_range_on_a_span_that_carries_it_loads() -> None:
    scenario = load_scenario("trigger-checkout-latency")
    assert scenario.fault is not None
    assert scenario.fault.where == {"cart.size": ">=8"}


def test_a_malformed_cart_size_value_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace(
        'where: {payment.provider: "stripe"}', 'where: {cart.size: "big"}'
    ).replace(
        'effect: {span: "payments.charge", latency_add_ms: 500}',
        'effect: {span: "checkout.process", latency_add_ms: 500}',
    )
    with pytest.raises(ValidationError, match="neither a range"):
        load_scenario_file(write(tmp_path, body))


def test_a_trigger_scenario_carries_its_block() -> None:
    scenario = load_scenario("trigger-checkout-latency")
    assert scenario.trigger is not None
    assert scenario.trigger.id == "bhuYLpkRKnv"
    assert scenario.trigger.threshold_ms == 1500
    assert scenario.trigger.window_min == 5


def test_trigger_is_optional() -> None:
    scenario = load_scenario("payments-stripe-v251-uswest")
    assert scenario.trigger is None


# --------------------------------------------------------------------------
# gen/scenarios/README.md
# --------------------------------------------------------------------------


def test_the_scenarios_readme_table_lists_every_scenario_and_nothing_else() -> None:
    from agent import format as fmt

    text = (SCENARIO_DIR / "README.md").read_text()
    table = fmt.parse_results_table(text, heading=None)
    assert table is not None
    headers, rows = table
    assert headers[0] == "id"
    listed = {row[0].strip("`") for row in rows}
    assert listed == set(available_scenarios())
