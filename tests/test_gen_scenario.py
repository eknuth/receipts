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


def test_scenario_dir_has_no_stray_yaml_extension() -> None:
    """The loader globs *.yml, so a *.yaml file would be invisible."""
    assert list(SCENARIO_DIR.glob("*.yaml")) == []


@pytest.mark.parametrize("scenario_id", sorted(REQUIRED_SCENARIOS))
def test_ground_truth_affected_share_matches_the_weights(scenario_id: str) -> None:
    """The share written in the file is the share the dimension weights produce."""
    scenario = load_scenario(scenario_id)
    if scenario.fault is None:
        assert scenario.ground_truth.affected_share is None
        return
    expected = scenario.ground_truth.affected_share
    assert expected is not None
    assert scenario.expected_affected_share() == pytest.approx(expected, abs=1e-9)


def test_exactly_one_control_and_three_faults() -> None:
    scenarios = load_all()
    controls = [s for s in scenarios if not s.ground_truth.incident_present]
    assert [s.id for s in controls] == ["control-quiet"]
    assert len(scenarios) - len(controls) == 3


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
