"""The verifier: query shapes, reading numbers back, and the decisions.

The queries in `tests/fixtures/gen/` were captured from live `run_query` calls
with any signed download URLs redacted. The payments fixtures are from
`run-2c818aeb7fbb`, the control fixture from `run-74772e9d3207`, and the error-surge
fixture from `run-a9e5027cb1a5`. Reading them here means the parsing is tested
against what the hosted MCP actually returns, not against what it is assumed
to return.

No test in this file opens a session. The live path is
`uv run python -m gen.verify --scenario <id> --run-id <id>`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gen import verify as V
from gen.emit import EmitResult, iso
from gen.scenario import Scenario, load_scenario

FIXTURES = Path(__file__).parent / "fixtures" / "gen"

PAYMENTS_WHERE = {
    "deployment.version": "2.5.1",
    "cloud.region": "us-west-2",
    "payment.provider": "stripe",
}


def fixture_text(name: str) -> str:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    return data["content_texts"][0]


def manifest(scenario: Scenario, *, requests: int = 18000, onset: bool = True) -> EmitResult:
    start = 1_788_000_000.0
    onset_s = start + 600.0 if onset and scenario.fault else None
    return EmitResult(
        run_id="run-test",
        scenario_id=scenario.id,
        dataset="receipts-shop",
        environment="receipts-demo",
        mode="backdate",
        seed=0,
        rps=15,
        minutes=20,
        window_start_s=start,
        window_end_s=start + 1200.0,
        window_start=iso(start),
        window_end=iso(start + 1200.0),
        onset_s=onset_s,
        onset=None if onset_s is None else iso(onset_s),
        requests=requests,
        spans=requests * 5,
        exported=requests * 5,
        failed_batches=0,
        fault_population=0,
        faulted_requests=0,
        errored_requests=0,
    )


def measurement(
    *,
    before: dict[str, float] | None = None,
    after: dict[str, float] | None = None,
    total: float = 18000.0,
    inside: float = 2160.0,
) -> V.Measurement:
    return V.Measurement(
        before=V.WindowStats(label="before", **(before or {})),
        after=V.WindowStats(label="after", **(after or {})),
        total_root_spans=total,
        inside_root_spans=inside,
    )


# --------------------------------------------------------------------------
# Query construction
# --------------------------------------------------------------------------


def calc(spec: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in spec["calculations"] if c.get("name") == name)


def test_the_window_query_pins_the_run_and_the_span() -> None:
    spec = V.window_query_spec("run-1", "payments.charge", PAYMENTS_WHERE, start="A", end="B")
    assert spec["filters"] == [
        {"column": "scenario.run_id", "op": "=", "value": "run-1"},
        {"column": "name", "op": "=", "value": "payments.charge"},
    ]
    assert spec["start_time"] == "A"
    assert spec["end_time"] == "B"


def test_the_inside_calculations_and_the_where_clauses_together() -> None:
    spec = V.window_query_spec("run-1", "payments.charge", PAYMENTS_WHERE, start="A", end="B")
    inside = calc(spec, "inside_p99")
    assert inside["op"] == "P99"
    assert inside["column"] == "duration_ms"
    assert inside["filters"] == [
        {"column": "deployment.version", "op": "=", "value": "2.5.1"},
        {"column": "cloud.region", "op": "=", "value": "us-west-2"},
        {"column": "payment.provider", "op": "=", "value": "stripe"},
    ]
    assert "filter_combination" not in inside


def test_the_outside_calculations_are_the_negation_by_de_morgan() -> None:
    """NOT(a AND b AND c) is (not a) OR (not b) OR (not c). That OR is the
    verify-by-negation query, and it has to be an OR or it means something
    else entirely."""
    spec = V.window_query_spec("run-1", "payments.charge", PAYMENTS_WHERE, start="A", end="B")
    outside = calc(spec, "outside_p99")
    assert outside["filter_combination"] == "OR"
    assert outside["filters"] == [
        {"column": "deployment.version", "op": "!=", "value": "2.5.1"},
        {"column": "cloud.region", "op": "!=", "value": "us-west-2"},
        {"column": "payment.provider", "op": "!=", "value": "stripe"},
    ]


def test_the_inside_error_count_ands_the_error_flag_onto_the_where_clauses() -> None:
    spec = V.window_query_spec("run-1", "payments.charge", PAYMENTS_WHERE, start="A", end="B")
    errors = calc(spec, "inside_errors")
    assert errors["filters"][-1] == {"column": "error", "op": "=", "value": True}
    assert "filter_combination" not in errors


def test_a_control_query_has_no_population_split() -> None:
    spec = V.window_query_spec("run-1", "HTTP POST /checkout", {}, start="A", end="B")
    names = {c["name"] for c in spec["calculations"]}
    assert names == {"inside_count", "inside_p99", "inside_errors"}
    assert "filters" not in calc(spec, "inside_p99")


def test_outside_errors_move_the_error_flag_to_the_top_level() -> None:
    """A per-calculation filter set gets one combination operator, so an OR of
    negations cannot also carry error=true. It goes in the WHERE instead."""
    spec = V.outside_errors_query_spec(
        "run-1", "payments.charge", PAYMENTS_WHERE, start="A", end="B"
    )
    assert {"column": "error", "op": "=", "value": True} in spec["filters"]
    assert calc(spec, "outside_errors")["filter_combination"] == "OR"


def test_the_population_query_counts_root_spans() -> None:
    spec = V.population_query_spec("run-1", PAYMENTS_WHERE, start="A", end="B")
    assert {"column": "name", "op": "=", "value": "HTTP POST /checkout"} in spec["filters"]
    assert calc(spec, "total_count") == {"op": "COUNT", "name": "total_count"}
    assert calc(spec, "inside_count")["filters"] == V._eq_filters(PAYMENTS_WHERE)


def test_a_control_population_query_asks_only_for_the_total() -> None:
    spec = V.population_query_spec("run-1", {}, start="A", end="B")
    assert [c["name"] for c in spec["calculations"]] == ["total_count"]


def test_split_points_use_onset_for_a_fault_and_the_middle_for_a_control() -> None:
    base = 1_788_000_000.0
    fault = load_scenario("payments-stripe-v251-uswest")
    assert V.split_points(fault, manifest(fault)) == (
        iso(base),
        iso(base + 600.0),
        iso(base + 1200.0),
    )

    control = load_scenario("control-quiet")
    assert V.split_points(control, manifest(control, onset=False))[1] == iso(base + 600.0)


# --------------------------------------------------------------------------
# Reading numbers out of a real response
# --------------------------------------------------------------------------


def test_read_row_takes_the_first_row_of_a_live_result() -> None:
    row = read = V.read_row(fixture_text("payments-stripe-v251-uswest_window_after"))
    assert read["inside_count"] == 1114
    assert row["inside_p99"] == pytest.approx(1101.46)
    assert row["outside_count"] == 7886
    assert row["outside_p99"] == pytest.approx(197.61)
    assert row["inside_errors"] == 2


def test_read_row_ignores_the_trailing_other_and_total_rows() -> None:
    """A query with no breakdowns still comes back with an OTHER row and a
    TOTAL row, and the TOTAL row's P99 differs from the real one."""
    text = fixture_text("payments-stripe-v251-uswest_window_after")
    assert "| 0 | 0 | 0 | 0 | 0 |" in text
    assert V.read_row(text)["inside_p99"] != 1102.01


def test_read_row_on_the_before_window() -> None:
    row = V.read_row(fixture_text("payments-stripe-v251-uswest_window_before"))
    assert row["inside_p99"] == pytest.approx(182.69)
    assert row["outside_p99"] == pytest.approx(198.05)


def test_read_row_on_a_population_query() -> None:
    row = V.read_row(fixture_text("payments-stripe-v251-uswest_population"))
    assert row["total_count"] == 18000
    assert row["inside_count"] == 2221


def test_read_row_on_a_control_query_has_no_outside_columns() -> None:
    row = V.read_row(fixture_text("control-quiet_window_before"))
    assert set(row) == {"inside_count", "inside_errors", "inside_p99"}


def test_read_row_on_a_single_calculation_query() -> None:
    row = V.read_row(fixture_text("checkout-error-surge-adyen_outside_errors_after"))
    assert row["outside_errors"] == 73


def test_read_row_complains_when_there_is_no_table() -> None:
    with pytest.raises(ValueError, match="no '# Results' table"):
        V.read_row("nothing here")


def test_read_row_complains_when_the_table_is_empty() -> None:
    with pytest.raises(ValueError, match="no rows"):
        V.read_row("# Results\n\n| a |\n| --- |\n")


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------


def verdicts(checks: list[V.Check]) -> dict[str, bool]:
    return {check.name: check.ok for check in checks}


def test_the_real_numbers_from_the_verified_payments_run_pass() -> None:
    scenario = load_scenario("payments-stripe-v251-uswest")
    before = V.read_row(fixture_text("payments-stripe-v251-uswest_window_before"))
    after = V.read_row(fixture_text("payments-stripe-v251-uswest_window_after"))
    population = V.read_row(fixture_text("payments-stripe-v251-uswest_population"))
    result = measurement(
        before={k: before[k] for k in before},
        after={k: after[k] for k in after},
        total=population["total_count"],
        inside=population["inside_count"],
    )
    checks = V.decide(scenario, manifest(scenario), result)
    assert all(check.ok for check in checks), [c for c in checks if not c.ok]


def test_a_latency_fault_that_did_not_land_fails() -> None:
    scenario = load_scenario("payments-stripe-v251-uswest")
    checks = V.decide(
        scenario,
        manifest(scenario),
        measurement(
            before={
                "inside_count": 1100,
                "inside_p99": 180,
                "outside_count": 7900,
                "outside_p99": 198,
            },
            after={
                "inside_count": 1100,
                "inside_p99": 190,
                "outside_count": 7900,
                "outside_p99": 198,
            },
        ),
    )
    assert verdicts(checks)["latency step inside the population"] is False
    assert verdicts(checks)["no latency step outside the population"] is True


def test_a_fault_that_leaked_outside_its_population_fails() -> None:
    scenario = load_scenario("payments-stripe-v251-uswest")
    checks = V.decide(
        scenario,
        manifest(scenario),
        measurement(
            before={
                "inside_count": 1100,
                "inside_p99": 180,
                "outside_count": 7900,
                "outside_p99": 198,
            },
            after={
                "inside_count": 1100,
                "inside_p99": 1100,
                "outside_count": 7900,
                "outside_p99": 900,
            },
        ),
    )
    assert verdicts(checks)["latency step inside the population"] is True
    assert verdicts(checks)["no latency step outside the population"] is False


def test_a_partial_export_fails_the_ingest_check() -> None:
    scenario = load_scenario("payments-stripe-v251-uswest")
    checks = V.decide(
        scenario,
        manifest(scenario),
        measurement(
            before={
                "inside_count": 900,
                "inside_p99": 180,
                "outside_count": 6200,
                "outside_p99": 198,
            },
            after={
                "inside_count": 550,
                "inside_p99": 1090,
                "outside_count": 3900,
                "outside_p99": 195,
            },
            total=11500,
            inside=1450,
        ),
    )
    assert verdicts(checks)["ingest"] is False
    assert verdicts(checks)["latency step inside the population"] is True


def test_a_population_that_is_not_the_declared_share_fails() -> None:
    scenario = load_scenario("payments-stripe-v251-uswest")
    checks = V.decide(
        scenario,
        manifest(scenario),
        measurement(
            before={
                "inside_count": 1100,
                "inside_p99": 180,
                "outside_count": 7900,
                "outside_p99": 198,
            },
            after={
                "inside_count": 1100,
                "inside_p99": 1100,
                "outside_count": 7900,
                "outside_p99": 198,
            },
            inside=5400.0,
        ),
    )
    assert verdicts(checks)["affected share"] is False


def test_too_few_rows_fails_before_any_ratio_is_believed() -> None:
    scenario = load_scenario("payments-stripe-v251-uswest")
    checks = V.decide(
        scenario,
        manifest(scenario, requests=200),
        measurement(
            before={"inside_count": 8, "inside_p99": 180, "outside_count": 60, "outside_p99": 198},
            after={"inside_count": 9, "inside_p99": 1100, "outside_count": 60, "outside_p99": 198},
            total=200,
            inside=24,
        ),
    )
    assert verdicts(checks)["row counts"] is False


def test_an_error_fault_that_landed_passes() -> None:
    scenario = load_scenario("checkout-error-surge-adyen")
    checks = V.decide(
        scenario,
        manifest(scenario),
        measurement(
            before={
                "inside_count": 2242,
                "inside_errors": 32,
                "inside_p99": 199,
                "outside_count": 6749,
                "outside_errors": 63,
                "outside_p99": 195,
            },
            after={
                "inside_count": 2232,
                "inside_errors": 583,
                "inside_p99": 200,
                "outside_count": 6768,
                "outside_errors": 73,
                "outside_p99": 199,
            },
            inside=4500.0,
        ),
    )
    assert all(check.ok for check in checks), [c for c in checks if not c.ok]


def test_an_error_fault_below_its_injected_rate_fails() -> None:
    scenario = load_scenario("checkout-error-surge-adyen")
    checks = V.decide(
        scenario,
        manifest(scenario),
        measurement(
            before={
                "inside_count": 2242,
                "inside_errors": 32,
                "inside_p99": 199,
                "outside_count": 6749,
                "outside_errors": 63,
                "outside_p99": 195,
            },
            after={
                "inside_count": 2232,
                "inside_errors": 100,
                "inside_p99": 200,
                "outside_count": 6768,
                "outside_errors": 73,
                "outside_p99": 199,
            },
            inside=4500.0,
        ),
    )
    assert verdicts(checks)["error rate inside the population reaches the injected rate"] is False


def test_errors_everywhere_are_not_an_isolated_fault() -> None:
    scenario = load_scenario("checkout-error-surge-adyen")
    checks = V.decide(
        scenario,
        manifest(scenario),
        measurement(
            before={
                "inside_count": 2242,
                "inside_errors": 32,
                "inside_p99": 199,
                "outside_count": 6749,
                "outside_errors": 63,
                "outside_p99": 195,
            },
            after={
                "inside_count": 2232,
                "inside_errors": 583,
                "inside_p99": 200,
                "outside_count": 6768,
                "outside_errors": 1700,
                "outside_p99": 199,
            },
            inside=4500.0,
        ),
    )
    assert verdicts(checks)["no error step outside the population"] is False


def test_a_quiet_control_passes() -> None:
    scenario = load_scenario("control-quiet")
    checks = V.decide(
        scenario,
        manifest(scenario, onset=False),
        measurement(
            before={"inside_count": 8985, "inside_errors": 43, "inside_p99": 316.5},
            after={"inside_count": 9001, "inside_errors": 34, "inside_p99": 329.5},
            inside=0.0,
        ),
    )
    assert all(check.ok for check in checks), [c for c in checks if not c.ok]
    assert "affected share" not in verdicts(checks)


def test_a_control_that_is_not_quiet_fails() -> None:
    scenario = load_scenario("control-quiet")
    checks = V.decide(
        scenario,
        manifest(scenario, onset=False),
        measurement(
            before={"inside_count": 8985, "inside_errors": 43, "inside_p99": 316.5},
            after={"inside_count": 9001, "inside_errors": 900, "inside_p99": 980.0},
            inside=0.0,
        ),
    )
    assert verdicts(checks)["no latency step"] is False
    assert verdicts(checks)["no error step"] is False


def test_a_before_window_with_no_errors_does_not_divide_by_zero() -> None:
    scenario = load_scenario("checkout-error-surge-adyen")
    checks = V.decide(
        scenario,
        manifest(scenario),
        measurement(
            before={
                "inside_count": 2242,
                "inside_errors": 0,
                "inside_p99": 199,
                "outside_count": 6749,
                "outside_errors": 0,
                "outside_p99": 195,
            },
            after={
                "inside_count": 2232,
                "inside_errors": 583,
                "inside_p99": 200,
                "outside_count": 6768,
                "outside_errors": 0,
                "outside_p99": 199,
            },
            inside=4500.0,
        ),
    )
    assert all(check.ok for check in checks), [c for c in checks if not c.ok]


def test_verify_result_is_ok_only_when_every_check_is() -> None:
    ok = V.VerifyResult("s", "r", [V.Check("a", True, "")], measurement())
    bad = V.VerifyResult("s", "r", [V.Check("a", True, ""), V.Check("b", False, "")], measurement())
    assert ok.ok is True
    assert bad.ok is False


def test_the_rendered_report_names_every_check_and_the_verdict() -> None:
    scenario = load_scenario("payments-stripe-v251-uswest")
    manifest_ = manifest(scenario)
    result = V.VerifyResult(
        scenario.id,
        "run-test",
        [V.Check("ingest", True, "all there"), V.Check("latency", False, "flat")],
        measurement(
            before={
                "inside_count": 1100,
                "inside_p99": 180,
                "outside_count": 7900,
                "outside_p99": 198,
            },
            after={
                "inside_count": 1100,
                "inside_p99": 181,
                "outside_count": 7900,
                "outside_p99": 198,
            },
        ),
    )
    text = V.render(result, manifest_)
    assert "[pass] ingest: all there" in text
    assert "[FAIL] latency: flat" in text
    assert "NOT VERIFIED" in text
    assert manifest_.window_start in text
