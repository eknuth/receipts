"""The generator: determinism, span shape, attributes, and fault application.

These tests are the reason the generator can be trusted as ground truth. They
run without a network and without Honeycomb, on the same request stream that
`gen/emit.py` ships.

Full scenarios are 18,000 requests, which is slow to build four times over, so
most tests shrink the baseline to a couple of minutes. The tests that care
about a share of the population keep the full length, because a share measured
on 300 requests is noise.
"""

from __future__ import annotations

import pytest

from gen import topology
from gen.scenario import Scenario, load_scenario

PAYMENTS = "payments-stripe-v251-uswest"


def shrink(scenario: Scenario, minutes: float = 2.0, rps: float = 10.0) -> Scenario:
    """A copy of `scenario` with a shorter, thinner baseline."""
    smaller = scenario.model_copy(deep=True)
    smaller.baseline.minutes = minutes
    smaller.baseline.rps = rps
    return smaller


@pytest.fixture(scope="module")
def payments_requests() -> list[topology.Request]:
    return topology.generate_requests(load_scenario(PAYMENTS), seed=0)


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def signature(requests: list[topology.Request]) -> list[tuple[float, str, bool]]:
    return [(round(r.duration_ms, 6), r.dims["payment.provider"], r.faulted) for r in requests]


def test_the_same_seed_gives_the_same_stream() -> None:
    scenario = shrink(load_scenario(PAYMENTS))
    assert signature(topology.generate_requests(scenario, seed=3)) == signature(
        topology.generate_requests(scenario, seed=3)
    )


def test_a_different_seed_gives_a_different_stream() -> None:
    scenario = shrink(load_scenario(PAYMENTS))
    assert signature(topology.generate_requests(scenario, seed=3)) != signature(
        topology.generate_requests(scenario, seed=4)
    )


def test_two_scenarios_do_not_share_a_stream_at_the_same_seed() -> None:
    """The seed is mixed with the scenario id, so the control is not the fault
    scenario with the fault removed."""
    a = topology.generate_requests(shrink(load_scenario(PAYMENTS)), seed=0)
    b = topology.generate_requests(shrink(load_scenario("control-quiet")), seed=0)
    assert [r.dims for r in a] != [r.dims for r in b]


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_request_count_follows_rps_and_minutes() -> None:
    scenario = shrink(load_scenario(PAYMENTS), minutes=3, rps=20)
    assert len(topology.generate_requests(scenario, seed=0)) == 3600


def test_offsets_are_inside_the_window_and_ordered() -> None:
    scenario = shrink(load_scenario(PAYMENTS), minutes=2, rps=20)
    offsets = [r.offset_s for r in topology.generate_requests(scenario, seed=0)]
    assert offsets == sorted(offsets)
    assert 0.0 <= min(offsets)
    assert max(offsets) < 120.0


def test_the_span_tree_is_the_documented_topology(
    payments_requests: list[topology.Request],
) -> None:
    root = payments_requests[0].root
    assert root.name == topology.ROOT_SPAN
    assert root.service == topology.GATEWAY
    assert [c.name for c in root.children] == ["checkout.process"]
    checkout = root.children[0]
    assert checkout.service == topology.CHECKOUT
    assert [c.name for c in checkout.children] == ["payments.charge", "inventory.reserve"]
    assert checkout.children[0].service == topology.PAYMENTS
    inventory = checkout.children[1]
    assert inventory.service == topology.INVENTORY_DB
    assert [c.name for c in inventory.children] == ["db.query"]
    assert len(list(root.walk())) == 5


def test_a_parent_span_covers_its_children(payments_requests: list[topology.Request]) -> None:
    for request in payments_requests[:200]:
        for span in request.root.walk():
            for child in span.children:
                assert child.start_offset_ms >= 0
                assert child.start_offset_ms + child.duration_ms <= span.duration_ms + 1e-6
            assert span.duration_ms >= sum(c.duration_ms for c in span.children)


def test_children_run_one_after_another(payments_requests: list[topology.Request]) -> None:
    checkout = payments_requests[0].root.children[0]
    charge, reserve = checkout.children
    assert reserve.start_offset_ms == pytest.approx(charge.start_offset_ms + charge.duration_ms)


def test_span_count_is_five_per_request() -> None:
    requests = topology.generate_requests(shrink(load_scenario(PAYMENTS)), seed=0)
    assert topology.span_count(requests) == 5 * len(requests)


# --------------------------------------------------------------------------
# Attributes
# --------------------------------------------------------------------------


def test_the_selectable_dimensions_ride_on_every_span(
    payments_requests: list[topology.Request],
) -> None:
    """A query for payments.charge has to be able to filter on the fault's dims."""
    for request in payments_requests[:100]:
        for span in request.root.walk():
            for name in topology.PROPAGATED_DIMS:
                assert span.attributes[name] == request.dims[name]
            assert span.attributes["service.component"] == span.service
            assert span.attributes["scenario.id"] == PAYMENTS


def test_root_only_attributes_stay_on_the_root(
    payments_requests: list[topology.Request],
) -> None:
    root = payments_requests[0].root
    assert root.attributes["http.route"].startswith("/checkout")
    assert 1 <= root.attributes["cart.size"] <= 12
    assert root.attributes["customer.id"].startswith("cust-")
    assert root.attributes["http.status_code"] in (200, 500)
    assert root.attributes["scenario.id"] == PAYMENTS
    charge = root.children[0].children[0]
    assert "http.route" not in charge.attributes
    assert "http.status_code" not in charge.attributes


def test_the_database_span_carries_the_statement_hash(
    payments_requests: list[topology.Request],
) -> None:
    db = payments_requests[0].root.children[0].children[1].children[0]
    assert db.name == "db.query"
    assert db.attributes["db.statement.hash"]


def test_customer_ids_are_high_cardinality_and_skewed(
    payments_requests: list[topology.Request],
) -> None:
    ids = [r.root.attributes["customer.id"] for r in payments_requests]
    counts: dict[str, int] = {}
    for value in ids:
        counts[value] = counts.get(value, 0) + 1
    assert len(counts) > 1000
    busiest = max(counts.values())
    assert busiest > 4 * (len(ids) / len(counts))


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def test_the_baseline_error_rate_is_about_half_a_percent() -> None:
    requests = topology.generate_requests(load_scenario("control-quiet"), seed=0)
    rate = sum(1 for r in requests if r.root.error) / len(requests)
    assert rate == pytest.approx(topology.BASELINE_ERROR_RATE, abs=0.002)


def test_an_error_marks_the_failing_span_and_its_ancestors() -> None:
    requests = topology.generate_requests(load_scenario("checkout-error-surge-adyen"), seed=0)
    failed = [r for r in requests if r.root.error]
    assert failed
    for request in failed[:50]:
        for span in request.root.walk():
            if span.error:
                assert span.attributes["error"] is True
            children = list(span.children)
            if any(c.error for c in children):
                assert span.error
        assert request.root.attributes["http.status_code"] == 500


def test_a_healthy_request_carries_error_false(
    payments_requests: list[topology.Request],
) -> None:
    healthy = next(r for r in payments_requests if not r.root.error)
    assert healthy.root.attributes["error"] is False
    assert healthy.root.attributes["http.status_code"] == 200


# --------------------------------------------------------------------------
# Fault application
# --------------------------------------------------------------------------


def test_only_the_where_population_after_onset_is_faulted(
    payments_requests: list[topology.Request],
) -> None:
    scenario = load_scenario(PAYMENTS)
    assert scenario.fault is not None
    onset_s = scenario.fault.onset_min * 60.0
    for request in payments_requests:
        expected_population = topology.matches(request.dims, scenario.fault.where)
        assert request.in_fault_population is expected_population
        assert request.faulted is (expected_population and request.offset_s >= onset_s)


def test_the_fault_population_matches_the_ground_truth_share(
    payments_requests: list[topology.Request],
) -> None:
    scenario = load_scenario(PAYMENTS)
    share = sum(1 for r in payments_requests if r.in_fault_population) / len(payments_requests)
    assert share == pytest.approx(scenario.ground_truth.affected_share, abs=0.01)


def test_the_fault_adds_the_declared_latency_to_the_named_span(
    payments_requests: list[topology.Request],
) -> None:
    def charge_ms(request: topology.Request) -> float:
        return request.root.children[0].children[0].duration_ms

    scenario = load_scenario(PAYMENTS)
    assert scenario.fault is not None
    added = scenario.fault.effect.latency_add_ms

    before = [charge_ms(r) for r in payments_requests if r.in_fault_population and not r.faulted]
    after = [charge_ms(r) for r in payments_requests if r.faulted]
    assert min(after) > max(before) - added  # the whole band moved, not a few outliers
    assert _median(after) - _median(before) == pytest.approx(added, rel=0.1)


def test_the_fault_does_not_touch_the_rest_of_the_population(
    payments_requests: list[topology.Request],
) -> None:
    scenario = load_scenario(PAYMENTS)
    assert scenario.fault is not None
    onset_s = scenario.fault.onset_min * 60.0
    outside = [r for r in payments_requests if not r.in_fault_population]
    before = [r.duration_ms for r in outside if r.offset_s < onset_s]
    after = [r.duration_ms for r in outside if r.offset_s >= onset_s]
    assert _median(after) == pytest.approx(_median(before), rel=0.05)


def test_the_error_fault_reaches_its_declared_rate() -> None:
    scenario = load_scenario("checkout-error-surge-adyen")
    assert scenario.fault is not None
    requests = topology.generate_requests(scenario, seed=0)
    faulted = [r for r in requests if r.faulted]
    charge_failed = [
        r for r in faulted if r.root.children[0].children[0].error and not _other_cause(r)
    ]
    rate = len(charge_failed) / len(faulted)
    assert rate == pytest.approx(scenario.fault.effect.error_rate, abs=0.03)


def _other_cause(request: topology.Request) -> bool:
    """True when something other than payments.charge failed in this request."""
    return any(
        span.error and span.name not in (topology.ROOT_SPAN, "checkout.process", "payments.charge")
        for span in request.root.walk()
    )


def test_a_control_scenario_faults_nothing() -> None:
    requests = topology.generate_requests(load_scenario("control-quiet"), seed=0)
    assert not any(r.faulted for r in requests)
    assert not any(r.in_fault_population for r in requests)


# --------------------------------------------------------------------------
# Red herrings
# --------------------------------------------------------------------------


def test_a_red_herring_is_present_before_onset_and_smaller_than_the_fault(
    payments_requests: list[topology.Request],
) -> None:
    scenario = load_scenario(PAYMENTS)
    assert scenario.fault is not None
    onset_s = scenario.fault.onset_min * 60.0
    herring = scenario.red_herrings[0]

    def db_ms(request: topology.Request) -> float:
        return request.root.children[0].children[1].children[0].duration_ms

    early = [r for r in payments_requests if r.offset_s < onset_s]
    hit = [db_ms(r) for r in early if topology.matches(r.dims, herring.where)]
    miss = [db_ms(r) for r in early if not topology.matches(r.dims, herring.where)]
    added = _median(hit) - _median(miss)
    assert added == pytest.approx(herring.effect.latency_add_ms, rel=0.15)
    assert added < scenario.fault.effect.latency_add_ms


def test_a_red_herring_does_not_step_at_onset(
    payments_requests: list[topology.Request],
) -> None:
    scenario = load_scenario(PAYMENTS)
    assert scenario.fault is not None
    onset_s = scenario.fault.onset_min * 60.0
    herring = scenario.red_herrings[0]

    def db_ms(request: topology.Request) -> float:
        return request.root.children[0].children[1].children[0].duration_ms

    hit = [r for r in payments_requests if topology.matches(r.dims, herring.where)]
    before = [db_ms(r) for r in hit if r.offset_s < onset_s]
    after = [db_ms(r) for r in hit if r.offset_s >= onset_s]
    assert _median(after) == pytest.approx(_median(before), rel=0.05)


# --------------------------------------------------------------------------
# Weighted draws
# --------------------------------------------------------------------------


def test_weighted_choice_reports_the_share_it_draws_at() -> None:
    chooser = topology.WeightedChoice({"a": 3.0, "b": 1.0})
    assert chooser.share("a") == pytest.approx(0.75)
    assert chooser.share("missing") == 0.0


def test_weighted_choice_rejects_empty_and_non_positive_weights() -> None:
    with pytest.raises(ValueError):
        topology.WeightedChoice({})
    with pytest.raises(ValueError):
        topology.WeightedChoice({"a": 0.0})


def test_population_share_is_the_product_of_the_dimension_shares() -> None:
    scenario = load_scenario(PAYMENTS)
    sampler = topology.Sampler(scenario)
    assert scenario.fault is not None
    assert sampler.population_share(scenario.fault.where) == pytest.approx(0.12)
    assert sampler.population_share({"payment.provider": "klarna"}) == 0.0
    assert sampler.population_share({"no.such.dim": "x"}) == 0.0


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
