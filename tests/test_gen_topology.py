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


def test_root_only_attributes_stay_on_the_root(
    payments_requests: list[topology.Request],
) -> None:
    root = payments_requests[0].root
    assert root.attributes["http.route"].startswith("/checkout")
    assert 1 <= root.attributes["cart.size"] <= 12
    assert root.attributes["customer.id"].startswith("cust-")
    assert root.attributes["http.status_code"] in (200, 500)
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


def test_customer_weights_for_spreads_the_remaining_share_over_the_zipf_tail() -> None:
    scenario = load_scenario("herring-customer-whale")
    weights = topology.customer_weights_for(scenario)
    chooser = topology.WeightedChoice(weights)
    assert chooser.share("cust-00007") == pytest.approx(0.15)
    assert sum(weights.values()) == pytest.approx(1.0, rel=1e-9)
    # cust-00000, the busiest unpinned id, still outweighs a rare id: pinning
    # cust-00007 did not flatten the Zipf shape of the rest of the tail.
    assert chooser.share("cust-00000") > chooser.share("cust-01999")


def test_customer_weights_for_is_unchanged_without_a_pin() -> None:
    scenario = load_scenario(PAYMENTS)
    assert topology.customer_weights_for(scenario) == topology.customer_weights()


# --------------------------------------------------------------------------
# R5: matches(), ranges, service.component, and timing windows
# --------------------------------------------------------------------------


def test_matches_treats_service_component_as_always_true() -> None:
    """Every span carries service.component for its own node, not the request's,
    so a where clause naming it is validated at load time and matches every
    request here rather than being checked against `dims`."""
    assert topology.matches({}, {"service.component": "inventory-db"})
    assert topology.matches({"cart.size": 3}, {"service.component": "anything"})


def test_matches_evaluates_a_numeric_range() -> None:
    assert topology.matches({"cart.size": 8}, {"cart.size": ">=8"})
    assert not topology.matches({"cart.size": 7}, {"cart.size": ">=8"})
    assert topology.matches({"cart.size": 3}, {"cart.size": "<5"})
    assert not topology.matches({"cart.size": 3}, {"cart.size": ">5"})


def test_matches_still_does_plain_equality() -> None:
    assert topology.matches({"payment.provider": "adyen"}, {"payment.provider": "adyen"})
    assert not topology.matches({"payment.provider": "stripe"}, {"payment.provider": "adyen"})


def test_a_dependency_fault_times_out_the_failing_span_exactly() -> None:
    """The failing db.query calls hit a 5000ms deadline exactly, no jitter, and
    only the ones the effect actually failed carry error.type=timeout; a
    baseline noise failure that happens to land on db.query does not."""
    scenario = load_scenario("dependency-inventory-db-timeouts")
    requests = topology.generate_requests(scenario, seed=0)

    def db_span(r: topology.Request) -> topology.SpanRecord:
        return r.root.children[0].children[1].children[0]

    faulted = [r for r in requests if r.faulted]
    timed_out = [r for r in faulted if db_span(r).attributes.get("error.type") == "timeout"]
    assert timed_out
    assert {round(db_span(r).duration_ms, 6) for r in timed_out} == {5000.0}
    for r in timed_out:
        assert db_span(r).error is True

    not_timed_out = [r for r in faulted if db_span(r).attributes.get("error.type") != "timeout"]
    assert all("error.type" not in db_span(r).attributes for r in not_timed_out)


def test_exception_event_lands_only_on_the_span_the_effect_failed() -> None:
    scenario = load_scenario("error-surge-exceptions")
    requests = topology.generate_requests(scenario, seed=0)
    assert scenario.fault is not None
    exc = scenario.fault.effect.exception
    assert exc is not None

    def charge_span(r: topology.Request) -> topology.SpanRecord:
        return r.root.children[0].children[0]

    with_exception = [r for r in requests if charge_span(r).exception_type is not None]
    assert with_exception
    for r in with_exception:
        span = charge_span(r)
        assert span.error is True
        assert span.exception_type == exc.type
        assert span.exception_message == exc.message
        assert r.faulted  # only the fault's own failures get one, not baseline or herring noise

    # The us-east-1 herring also fails payments.charge, with no exception.
    herring_failures = [
        r
        for r in requests
        if charge_span(r).error and not r.faulted and charge_span(r).exception_type is None
    ]
    assert herring_failures


def test_a_cart_size_range_selects_the_documented_population() -> None:
    scenario = load_scenario("trigger-checkout-latency")
    sampler = topology.Sampler(scenario)
    assert scenario.fault is not None
    assert sampler.population_share(scenario.fault.where) == pytest.approx(0.1051, abs=0.001)


def test_a_pinned_customer_id_draws_at_its_pinned_share() -> None:
    scenario = load_scenario("herring-customer-whale")
    requests = topology.generate_requests(scenario, seed=0)
    ids = [r.root.attributes["customer.id"] for r in requests]
    share = sum(1 for i in ids if i == "cust-00007") / len(ids)
    assert share == pytest.approx(0.15, abs=0.02)


def test_the_whale_holds_about_a_third_of_all_errors() -> None:
    """cust-00007 is 15% of traffic; its own elevated failure rate makes it
    about 30% of all errors over the window even though adyen, not the whale,
    is the incident."""
    scenario = load_scenario("herring-customer-whale")
    requests = topology.generate_requests(scenario, seed=0)
    whale = "cust-00007"

    traffic_share = sum(1 for r in requests if r.root.attributes["customer.id"] == whale) / len(
        requests
    )
    assert traffic_share == pytest.approx(0.15, abs=0.02)

    errored = [r for r in requests if r.root.error]
    whale_errors = sum(1 for r in errored if r.root.attributes["customer.id"] == whale)
    error_share = whale_errors / len(errored)
    assert error_share == pytest.approx(0.30, abs=0.05)


def test_the_whales_own_error_share_is_not_steady_across_onset() -> None:
    """The whale's 30% share of all errors is a whole-window number, not a
    steady one: adyen's surge after onset dilutes the whale's own share of
    errors from 66% before onset to 24% after, even as the whale's own error
    rate itself climbs (its errors are a shrinking share of a bigger pool).
    Excluding the whale, adyen's own step still holds; excluding adyen, the
    whale's own rate does not step, which is what makes the whale a red
    herring and adyen the incident."""
    scenario = load_scenario("herring-customer-whale")
    requests = topology.generate_requests(scenario, seed=0)
    whale = "cust-00007"
    onset_s = scenario.fault.onset_min * 60.0

    def share(attr: str, value: str, before: bool) -> float:
        window = [r for r in requests if (r.offset_s < onset_s) is before]
        errored = [r for r in window if r.root.error]
        hits = [r for r in errored if r.root.attributes[attr] == value]
        return len(hits) / len(errored)

    assert share("customer.id", whale, before=True) == pytest.approx(0.663, abs=0.01)
    assert share("customer.id", whale, before=False) == pytest.approx(0.236, abs=0.01)

    def own_error_rate(attr: str, value: str, before: bool) -> float:
        window = [
            r
            for r in requests
            if (r.offset_s < onset_s) is before and r.root.attributes[attr] == value
        ]
        return sum(1 for r in window if r.root.error) / len(window)

    assert own_error_rate("customer.id", whale, before=True) == pytest.approx(0.049, abs=0.005)
    assert own_error_rate("customer.id", whale, before=False) == pytest.approx(0.106, abs=0.005)

    def excl_error_rate(attr: str, value: str, before: bool) -> float:
        window = [
            r
            for r in requests
            if (r.offset_s < onset_s) is before and r.root.attributes[attr] != value
        ]
        return sum(1 for r in window if r.root.error) / len(window)

    # Excluding the whale (customer.id != cust-00007): adyen's step survives.
    excl_whale_before = excl_error_rate("customer.id", whale, before=True)
    excl_whale_after = excl_error_rate("customer.id", whale, before=False)
    assert excl_whale_before == pytest.approx(0.0045, abs=0.001)
    assert excl_whale_after == pytest.approx(0.0594, abs=0.005)
    assert excl_whale_after / excl_whale_before > 10  # steps hard, the whale was not carrying it

    # Excluding adyen (payment.provider != adyen): the rate stays flat.
    excl_adyen_before = excl_error_rate("payment.provider", "adyen", before=True)
    excl_adyen_after = excl_error_rate("payment.provider", "adyen", before=False)
    assert excl_adyen_before == pytest.approx(0.0099, abs=0.002)
    assert excl_adyen_after == pytest.approx(0.0134, abs=0.003)
    assert excl_adyen_after / excl_adyen_before < 2  # no real step, unlike excluding the whale


def _self_ms(span: topology.SpanRecord) -> float:
    """A span's own time: its duration minus every child's."""
    return span.duration_ms - sum(child.duration_ms for child in span.children)


def _self_times_by_name(requests: list[topology.Request]) -> dict[str, list[float]]:
    times: dict[str, list[float]] = {}
    for request in requests:
        for span in request.root.walk():
            times.setdefault(span.name, []).append(_self_ms(span))
    return times


def test_sigma_scale_widens_the_spread_without_moving_each_spans_own_median() -> None:
    """sigma_scale widens the log-normal draw for each span's own time, and
    each span's own median holds. The fair comparison is control-noisy
    against an in-memory copy of itself with sigma_scale reset to 1.0:
    comparing against control-quiet, as an earlier version of this test did,
    compares against a scenario with its own red herring on db.query, a
    confound that happened to make the old, looser assertion pass."""
    noisy = load_scenario("control-noisy")
    assert noisy.baseline.sigma_scale == pytest.approx(2.0)
    flat = noisy.model_copy(deep=True)
    flat.baseline.sigma_scale = 1.0

    noisy_times = _self_times_by_name(topology.generate_requests(noisy, seed=0))
    flat_times = _self_times_by_name(topology.generate_requests(flat, seed=0))

    for name in topology.SPAN_NAMES:
        ratio = _median(noisy_times[name]) / _median(flat_times[name])
        assert 0.9 <= ratio <= 1.1, (name, ratio)

    noisy_root = noisy_times[topology.ROOT_SPAN]
    flat_root = flat_times[topology.ROOT_SPAN]
    assert (max(noisy_root) - min(noisy_root)) > (max(flat_root) - min(flat_root))


def test_sigma_scale_still_moves_the_root_median_because_durations_sum() -> None:
    """The root span's duration is its own time plus every descendant's, so
    widening each span's spread compounds: a sum of wider log-normals has a
    heavier right tail, and the root median shifts even though no individual
    span's own median does (the test above)."""
    noisy = load_scenario("control-noisy")
    flat = noisy.model_copy(deep=True)
    flat.baseline.sigma_scale = 1.0

    noisy_root = _median([r.duration_ms for r in topology.generate_requests(noisy, seed=0)])
    flat_root = _median([r.duration_ms for r in topology.generate_requests(flat, seed=0)])
    assert noisy_root / flat_root > 1.1


def test_a_red_herring_burst_only_fires_inside_its_window() -> None:
    scenario = load_scenario("control-noisy")
    herring = scenario.red_herrings[0]
    assert herring.duration_min is not None
    requests = topology.generate_requests(scenario, seed=0)

    def charge_span(r: topology.Request) -> topology.SpanRecord:
        return r.root.children[0].children[0]

    start_s = herring.onset_min * 60.0
    end_s = start_s + herring.duration_min * 60.0
    paypal = [r for r in requests if r.root.attributes["payment.provider"] == "paypal"]
    before = [r for r in paypal if r.offset_s < start_s]
    during = [r for r in paypal if start_s <= r.offset_s < end_s]
    after = [r for r in paypal if r.offset_s >= end_s]
    assert before and during and after

    before_rate = sum(1 for r in before if charge_span(r).error) / len(before)
    during_rate = sum(1 for r in during if charge_span(r).error) / len(during)
    after_rate = sum(1 for r in after if charge_span(r).error) / len(after)
    assert during_rate > 10 * max(before_rate, after_rate, 0.001)
    assert after_rate == pytest.approx(before_rate, abs=0.05)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
