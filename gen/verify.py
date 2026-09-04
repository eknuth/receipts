"""Check that an emitted scenario's fault is actually visible in Honeycomb.

    uv run python -m gen.verify --scenario payments-stripe-v251-uswest --run-id run-abc123

The generator can be right and the run still be wrong: a batch of spans can
fail to export, a timestamp can land outside the window, a scenario file can
select a population that does not exist. So before a run is used as ground
truth, the fault has to be provable from the data with the same queries a
person would write.

The queries go through the hosted Honeycomb MCP (`agent/mcp_client.py`), which
is what is already authenticated here, and they are scoped to `scenario.run_id`
so two runs of the same scenario never contaminate each other.

`run_query`'s `query_spec` takes its time bounds as `from`/`to`; a live check
on 2026-09-04 found the hosted MCP now rejects the `start_time`/`end_time`
names this file used to send, with an error naming the rename. Every query
spec here uses `from`/`to`.

What is checked, per scenario class:

  latency fault
    P99 of the faulted span inside the `where` population is at least twice as
    high after onset as before, and outside that population it moves by less
    than half again. "Outside" is the exact complement, built by OR-ing a `!=`
    for each clause in `where`, which is the verify-by-negation query.

  error fault
    The error rate inside the population after onset reaches at least half the
    injected rate and at least five times its own before-onset rate, and the
    rate outside the population does not step.

  dependency fault (the fault's population is the whole run)
    A `where` naming only `service.component` selects every request, so there
    is no "outside" for the checks above to compare against; the negation
    below stands in for both. The population query itself changes shape too:
    scoped only to the run id, it counts root spans for the total and the
    fault's own span on the service it runs for "inside", a real measurement
    rather than a copy of the total, so a run that dropped that span fails the
    affected-share check.

  every fault with a red herring: fault step survives excluding the herring
    Filtering the fault's own span to the herring's `where`, before and after
    onset, and reading the OUTSIDE numbers (the complement of the herring's
    population) proves the fault's step holds with the herring's population
    excluded, rather than merely present alongside it. A latency fault needs the
    outside P99 to step by `LATENCY_STEP_MIN`; an error fault needs the
    outside error rate to step by `ERROR_STEP_MIN`, which needs its own
    outside-errors query per window, the same way the main measurement does.
    This is what makes `herring-customer-whale` honest: the whale holds 30%
    of all errors over the window, but excluding the whale, adyen's error
    rate still steps 13x at onset, which is the fault surviving the herring's
    exclusion.

  dependency fault's own negation
    P99 of a different, unrelated span (`payments.charge`) across the same
    onset must stay under `OUTSIDE_STEP_MAX`. This is the "outside" check for
    a fault whose own population has no outside, standing in for both the
    latency and the error version of that check.

  control
    No latency step and no error step on the root span between the first half
    of the window and the second. A red herring with a `duration_min` (a
    burst) gets two more checks: its own rate inside its burst window clears
    half its injected rate and steps well above the rest of the window, and
    its rate from the end of the burst to the end of the window falls back
    under `OUTSIDE_ERROR_STEP_MAX` times its rate before the burst, proving
    the burst resolved on its own rather than just tailing off across the
    whole window. The row-count check covers the burst window too.

  trigger
    When the scenario names a Honeycomb trigger, `get_triggers` is called
    once after everything else and the row for that trigger id must report
    `triggered` as true. This reads the trigger's current state, not its
    history: a run where some other run fired it passes on that basis, and a
    run where verify runs after the triggered window has passed fails on
    that basis. A run where it never fired is NOT VERIFIED.

  exception
    When the fault's effect carries an `exception`, a trace with a failure in
    the fault's population is looked up (`run_query` breaking down on
    `trace.trace_id`, one row) and then fetched with `get_trace(show_events=
    true)`; a span_event row named `exception` must hang off the failed
    span. Honeycomb renders the event as its own row, carrying only its own
    attributes and no `exception.type` in that table, which is why a second
    check exists: one more `run_query`, scoped to the run and filtered to
    `name = exception` and `exception.type = <configured type>` (a real
    filter, since Honeycomb also copies the event's attributes onto the
    parent span's row), must return exactly `manifest.span_events` rows, the
    same exactness the ingest check uses. That query extends `to` ten
    seconds past the window's own end, since an event sits at its span's end
    time and a span starting just before the window closes still runs its
    full duration.

  every scenario
    The number of root spans matches the manifest, so a partial export fails
    instead of quietly halving the population, and the share of requests in
    the fault population matches `ground_truth.affected_share`.

Exits 0 when every check passes, 1 when any check fails, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent import format as fmt
from agent.mcp_client import HoneycombMCP, ToolResult
from gen import topology
from gen.emit import RUNS_DIR, EmitResult, iso, load_manifest
from gen.scenario import ExceptionEffect, RedHerring, Scenario, available_scenarios, load_scenario
from receipts.settings import Settings

logger = logging.getLogger(__name__)

# A latency fault has to at least double the tail inside its population.
LATENCY_STEP_MIN = 2.0
# Outside the population, the tail has to stay put. Half again is noise.
OUTSIDE_STEP_MAX = 1.5
# An error fault has to reach half its injected rate and stand well clear of
# whatever the population was already doing.
ERROR_RATE_FRACTION_MIN = 0.5
ERROR_STEP_MIN = 5.0
OUTSIDE_ERROR_STEP_MAX = 3.0
# Divide-by-zero guard for a before-window with no errors at all.
ERROR_RATE_FLOOR = 0.001
# How far the measured fault population may sit from ground_truth.affected_share.
SHARE_TOLERANCE = 0.02
# Below this many rows in a window, the numbers are not worth believing.
MIN_ROWS = 100

# A dependency fault's population is the whole run (a `where` naming only
# `service.component`), so there is nothing outside it to compare. The
# negation instead watches a span the fault does not touch.
DEPENDENCY_NEGATION_SPAN = "payments.charge"
# How much of the manifest's root spans must be queryable.


@dataclass(frozen=True)
class Check:
    """One assertion, its verdict, and the numbers behind it."""

    name: str
    ok: bool
    detail: str


@dataclass
class WindowStats:
    """The numbers for one half of the run, inside and outside the population."""

    label: str
    inside_count: float = 0.0
    inside_p99: float = 0.0
    inside_errors: float = 0.0
    outside_count: float = 0.0
    outside_p99: float = 0.0
    outside_errors: float = 0.0
    query_ids: list[str] = field(default_factory=list)
    permalinks: list[str] = field(default_factory=list)

    @property
    def inside_error_rate(self) -> float:
        return self.inside_errors / self.inside_count if self.inside_count else 0.0

    @property
    def outside_error_rate(self) -> float:
        return self.outside_errors / self.outside_count if self.outside_count else 0.0


@dataclass
class BurstMeasurement:
    """Before/burst/after windows for one red herring with a `duration_min`."""

    herring: RedHerring
    before: WindowStats
    burst: WindowStats
    after: WindowStats


@dataclass
class HerringSurvivalMeasurement:
    """The fault's span, outside one red herring's population, before and
    after onset: does the fault's own step survive with the herring's
    population excluded."""

    herring: RedHerring
    before: WindowStats
    after: WindowStats


@dataclass
class Measurement:
    """Everything one verification read out of Honeycomb."""

    before: WindowStats
    after: WindowStats
    total_root_spans: float = 0.0
    inside_root_spans: float = 0.0
    query_ids: list[str] = field(default_factory=list)
    permalinks: list[str] = field(default_factory=list)
    # Set only for a dependency fault, whose population is the whole run: P99
    # of a different, unrelated span across the same onset.
    negation_before_p99: float | None = None
    negation_after_p99: float | None = None
    # One entry per red herring that has a `duration_min`.
    bursts: list[BurstMeasurement] = field(default_factory=list)
    # One entry per red herring on an incident scenario.
    herring_survival: list[HerringSurvivalMeasurement] = field(default_factory=list)
    # The raw `get_triggers` text, when the scenario names a trigger.
    trigger_text: str | None = None
    # The trace looked up for the exception check, when the fault carries one.
    exception_trace_id: str | None = None
    exception_trace_text: str | None = None
    # COUNT of exception rows of the configured type, over the whole run.
    exception_event_count: float | None = None

    @property
    def measured_share(self) -> float:
        return self.inside_root_spans / self.total_root_spans if self.total_root_spans else 0.0


@dataclass
class VerifyResult:
    scenario_id: str
    run_id: str
    checks: list[Check]
    measurement: Measurement

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


# --------------------------------------------------------------------------
# Query construction
# --------------------------------------------------------------------------


# The complement of each range operator, for `_ne_filters` on a range clause:
# NOT(x >= 8) is x < 8, and so on.
_RANGE_COMPLEMENT: dict[str, str] = {">=": "<", "<=": ">", ">": "<=", "<": ">="}


def _eq_filter(column: str, value: str) -> dict[str, Any]:
    """One `where` clause as a Honeycomb filter.

    A plain value is an `=` filter. A range value like `>=8` (see
    `topology.RANGE_RE`) becomes the range's own operator with a numeric
    value: Honeycomb's `=` operator would compare the string `">=8"`
    literally, not evaluate it as a comparison.
    """
    rng = topology.RANGE_RE.match(str(value))
    if rng is not None:
        return {"column": column, "op": rng.group(1), "value": float(rng.group(2))}
    return {"column": column, "op": "=", "value": value}


def _ne_filter(column: str, value: str) -> dict[str, Any]:
    """The complement of one `where` clause, by De Morgan for a plain value
    (`!=`) or by flipping the comparison for a range value."""
    rng = topology.RANGE_RE.match(str(value))
    if rng is not None:
        return {
            "column": column,
            "op": _RANGE_COMPLEMENT[rng.group(1)],
            "value": float(rng.group(2)),
        }
    return {"column": column, "op": "!=", "value": value}


def _eq_filters(where: Mapping[str, str]) -> list[dict[str, Any]]:
    return [_eq_filter(column, value) for column, value in where.items()]


def _ne_filters(where: Mapping[str, str]) -> list[dict[str, Any]]:
    """The complement of `where`, by De Morgan: NOT(a AND b) is (not a) OR (not b)."""
    return [_ne_filter(column, value) for column, value in where.items()]


def window_query_spec(
    run_id: str,
    span_name: str,
    where: Mapping[str, str],
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    """One query covering both populations for one half of the run.

    Per-calculation filters keep the inside and the outside in a single query:
    the inside calculations AND the `where` clauses together, the outside ones
    OR the negations. The top-level filters pin the run and the span.
    """
    eq = _eq_filters(where)
    calculations: list[dict[str, Any]] = [
        {"op": "COUNT", "name": "inside_count", **({"filters": eq} if eq else {})},
        {
            "op": "P99",
            "column": "duration_ms",
            "name": "inside_p99",
            **({"filters": eq} if eq else {}),
        },
        {
            "op": "COUNT",
            "name": "inside_errors",
            "filters": [*eq, {"column": "error", "op": "=", "value": True}],
        },
    ]
    if where:
        ne = _ne_filters(where)
        calculations += [
            {"op": "COUNT", "name": "outside_count", "filters": ne, "filter_combination": "OR"},
            {
                "op": "P99",
                "column": "duration_ms",
                "name": "outside_p99",
                "filters": ne,
                "filter_combination": "OR",
            },
        ]
    return {
        "calculations": calculations,
        "filters": [
            {"column": "scenario.run_id", "op": "=", "value": run_id},
            {"column": "name", "op": "=", "value": span_name},
        ],
        "from": start,
        "to": end,
    }


def outside_errors_query_spec(
    run_id: str,
    span_name: str,
    where: Mapping[str, str],
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Errors outside the population, as a separate query.

    A per-calculation filter set is combined with one operator, so an OR of
    negations cannot also carry `error = true`. Moving `error = true` to the
    top-level filters splits it into its own query.
    """
    return {
        "calculations": [
            {
                "op": "COUNT",
                "name": "outside_errors",
                "filters": _ne_filters(where),
                "filter_combination": "OR",
            }
        ],
        "filters": [
            {"column": "scenario.run_id", "op": "=", "value": run_id},
            {"column": "name", "op": "=", "value": span_name},
            {"column": "error", "op": "=", "value": True},
        ],
        "from": start,
        "to": end,
    }


def population_query_spec(
    run_id: str,
    where: Mapping[str, str],
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Root spans over the whole run: how many there are, and how many are in scope."""
    eq = _eq_filters(where)
    calculations: list[dict[str, Any]] = [{"op": "COUNT", "name": "total_count"}]
    if eq:
        calculations.append({"op": "COUNT", "name": "inside_count", "filters": eq})
    return {
        "calculations": calculations,
        "filters": [
            {"column": "scenario.run_id", "op": "=", "value": run_id},
            {"column": "name", "op": "=", "value": topology.ROOT_SPAN},
        ],
        "from": start,
        "to": end,
    }


def full_population_query_spec(
    run_id: str,
    root_span: str,
    fault_span: str,
    service: str,
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    """The population query for a fault whose `where` selects the whole run.

    `where` names only `service.component`, which root spans do not carry a
    matching value for (every root span's own `service.component` is
    `"gateway"`, whatever service the fault names): ANDing it onto the usual
    population query measures zero root spans instead of the whole run,
    which is the bug a live run against `dependency-inventory-db-timeouts`
    found (affected share came back 0.000 against a ground truth of 1.000).

    So the total counts root spans, scoped only to the run id, and the
    "inside" count is a real measurement of its own: every span the fault's
    own `fault.effect.span` on the service it runs. Every request builds
    exactly one of each, so a run that dropped that span, or emitted it
    under the wrong service, comes back short and fails the affected-share
    check instead of passing 1.000 against 1.000 by construction.
    """
    return {
        "calculations": [
            {
                "op": "COUNT",
                "name": "total_count",
                "filters": [{"column": "name", "op": "=", "value": root_span}],
            },
            {
                "op": "COUNT",
                "name": "inside_count",
                "filters": [
                    {"column": "name", "op": "=", "value": fault_span},
                    {"column": "service.component", "op": "=", "value": service},
                ],
            },
        ],
        "filters": [{"column": "scenario.run_id", "op": "=", "value": run_id}],
        "from": start,
        "to": end,
    }


# --------------------------------------------------------------------------
# Reading numbers back out
# --------------------------------------------------------------------------


def read_row(text: str) -> dict[str, float]:
    """The first data row of a `# Results` table, as {column: number}.

    A query with no breakdowns still comes back with an OTHER row and a TOTAL
    row underneath the one real row, so the first row is the answer. Cells
    that are not numbers are dropped, which is how a missing calculation
    surfaces as a failed check rather than a wrong number.
    """
    table = fmt.parse_results_table(text)
    if table is None:
        raise ValueError("the query result has no '# Results' table")
    headers, rows = table
    if not rows:
        raise ValueError("the query returned no rows")
    values: dict[str, float] = {}
    for header, cell in zip(headers, rows[0], strict=False):
        try:
            values[header] = float(cell)
        except ValueError:
            continue
    return values


def _result_text(result: ToolResult) -> str:
    if result.is_error:
        raise RuntimeError(result.text)
    return result.raw if isinstance(result.raw, str) else result.text


# --------------------------------------------------------------------------
# Decisions, with no network in sight
# --------------------------------------------------------------------------


def _ratio(after: float, before: float) -> float:
    if before:
        return after / before
    return float("inf") if after else 0.0


def _is_full_population(scenario: Scenario) -> bool:
    """True when the fault's `where` selects the whole run: every clause names
    `service.component`, so there is no "outside" population to compare
    against.

    Decided from the shape of `where`, not from `ground_truth.affected_share`,
    so a mistyped share cannot flip which checks the verifier runs out from
    under it; `_check_affected_share` is what catches that mismatch.
    """
    return scenario.fault is not None and set(scenario.fault.where) == {"service.component"}


def decide(scenario: Scenario, manifest: EmitResult, measurement: Measurement) -> list[Check]:
    """Every check for this scenario, from numbers that are already measured."""
    checks: list[Check] = [
        _check_ingest(manifest, measurement),
        _check_rows(measurement),
    ]
    if scenario.fault is None:
        checks += _control_checks(measurement)
        checks += _burst_checks(measurement)
    else:
        checks.append(_check_affected_share(scenario, measurement))
        full_population = _is_full_population(scenario)
        effect = scenario.fault.effect
        if effect.latency_add_ms or effect.timeout_ms:
            if full_population:
                checks += _latency_checks_full_population(measurement)
            else:
                checks += _latency_checks(measurement)
        if effect.error_rate:
            if full_population:
                checks += _error_checks_full_population(scenario, measurement)
            else:
                checks += _error_checks(scenario, measurement)
        if full_population:
            checks.append(_dependency_negation_check(measurement))
        checks += _herring_survives_checks(scenario, measurement)
        checks += _check_exception(scenario, manifest, measurement)
    trigger_check = _check_trigger(scenario, measurement)
    if trigger_check is not None:
        checks.append(trigger_check)
    return checks


def _check_ingest(manifest: EmitResult, measurement: Measurement) -> Check:
    """Every root span the manifest says was sent is queryable.

    Exact, on purpose. The window edges sit on whole seconds, so a shortfall
    of even one request means Honeycomb dropped it, and a run with dropped
    spans is not ground truth.
    """
    expected = float(manifest.requests)
    seen = measurement.total_root_spans
    return Check(
        name="ingest",
        ok=seen == expected,
        detail=f"{seen:.0f} of {expected:.0f} root spans queryable",
    )


def _check_rows(measurement: Measurement) -> Check:
    counts = {
        "before inside": measurement.before.inside_count,
        "after inside": measurement.after.inside_count,
    }
    if measurement.before.outside_count or measurement.after.outside_count:
        counts["before outside"] = measurement.before.outside_count
        counts["after outside"] = measurement.after.outside_count
    for burst in measurement.bursts:
        label = ", ".join(f"{k}={v}" for k, v in burst.herring.where.items())
        counts[f"burst window ({label})"] = burst.burst.inside_count
    thin = [f"{label} {count:.0f}" for label, count in counts.items() if count < MIN_ROWS]
    return Check(
        name="row counts",
        ok=not thin,
        detail=(
            f"every window has at least {MIN_ROWS} rows: "
            + ", ".join(f"{label} {count:.0f}" for label, count in counts.items())
            if not thin
            else f"too few rows to judge: {', '.join(thin)}"
        ),
    )


def _check_affected_share(scenario: Scenario, measurement: Measurement) -> Check:
    expected = scenario.ground_truth.affected_share or 0.0
    measured = measurement.measured_share
    delta = abs(measured - expected)
    return Check(
        name="affected share",
        ok=delta <= SHARE_TOLERANCE,
        detail=(
            f"{measured:.3f} of root spans in the fault population, ground truth "
            f"{expected:.3f}, off by {delta:.3f} (tolerance {SHARE_TOLERANCE})"
        ),
    )


def _latency_checks(measurement: Measurement) -> list[Check]:
    before, after = measurement.before, measurement.after
    inside = _ratio(after.inside_p99, before.inside_p99)
    outside = _ratio(after.outside_p99, before.outside_p99)
    return [
        Check(
            name="latency step inside the population",
            ok=inside >= LATENCY_STEP_MIN,
            detail=(
                f"P99 {before.inside_p99:.1f}ms before onset, {after.inside_p99:.1f}ms after, "
                f"{inside:.2f}x (need at least {LATENCY_STEP_MIN}x)"
            ),
        ),
        Check(
            name="no latency step outside the population",
            ok=outside < OUTSIDE_STEP_MAX,
            detail=(
                f"P99 {before.outside_p99:.1f}ms before onset, {after.outside_p99:.1f}ms after, "
                f"{outside:.2f}x (must stay under {OUTSIDE_STEP_MAX}x)"
            ),
        ),
    ]


def _error_checks(scenario: Scenario, measurement: Measurement) -> list[Check]:
    assert scenario.fault is not None
    injected = scenario.fault.effect.error_rate
    before, after = measurement.before, measurement.after
    inside_step = _ratio(after.inside_error_rate, max(before.inside_error_rate, ERROR_RATE_FLOOR))
    outside_step = _ratio(
        after.outside_error_rate, max(before.outside_error_rate, ERROR_RATE_FLOOR)
    )
    floor = injected * ERROR_RATE_FRACTION_MIN
    return [
        Check(
            name="error rate inside the population reaches the injected rate",
            ok=after.inside_error_rate >= floor,
            detail=(
                f"{after.inside_error_rate:.3f} after onset against an injected {injected:.3f} "
                f"(need at least {floor:.3f})"
            ),
        ),
        Check(
            name="error step inside the population",
            ok=inside_step >= ERROR_STEP_MIN,
            detail=(
                f"error rate {before.inside_error_rate:.4f} before onset, "
                f"{after.inside_error_rate:.4f} after, {inside_step:.1f}x "
                f"(need at least {ERROR_STEP_MIN}x)"
            ),
        ),
        Check(
            name="no error step outside the population",
            ok=outside_step < OUTSIDE_ERROR_STEP_MAX,
            detail=(
                f"error rate {before.outside_error_rate:.4f} before onset, "
                f"{after.outside_error_rate:.4f} after, {outside_step:.1f}x "
                f"(must stay under {OUTSIDE_ERROR_STEP_MAX}x)"
            ),
        ),
    ]


def _control_checks(measurement: Measurement) -> list[Check]:
    before, after = measurement.before, measurement.after
    latency = _ratio(after.inside_p99, before.inside_p99)
    errors = _ratio(after.inside_error_rate, max(before.inside_error_rate, ERROR_RATE_FLOOR))
    return [
        Check(
            name="no latency step",
            ok=latency < OUTSIDE_STEP_MAX,
            detail=(
                f"root P99 {before.inside_p99:.1f}ms in the first half, "
                f"{after.inside_p99:.1f}ms in the second, {latency:.2f}x "
                f"(must stay under {OUTSIDE_STEP_MAX}x)"
            ),
        ),
        Check(
            name="no error step",
            ok=errors < OUTSIDE_ERROR_STEP_MAX,
            detail=(
                f"root error rate {before.inside_error_rate:.4f} in the first half, "
                f"{after.inside_error_rate:.4f} in the second, {errors:.1f}x "
                f"(must stay under {OUTSIDE_ERROR_STEP_MAX}x)"
            ),
        ),
    ]


def _latency_checks_full_population(measurement: Measurement) -> list[Check]:
    """The inside step, same as `_latency_checks`. `decide` adds the
    negation-by-a-different-span check separately, once, since it stands in
    for the "outside the population" half of both the latency and error
    checks, once, not once per effect kind."""
    before, after = measurement.before, measurement.after
    inside = _ratio(after.inside_p99, before.inside_p99)
    return [
        Check(
            name="latency step inside the population",
            ok=inside >= LATENCY_STEP_MIN,
            detail=(
                f"P99 {before.inside_p99:.1f}ms before onset, {after.inside_p99:.1f}ms after, "
                f"{inside:.2f}x (need at least {LATENCY_STEP_MIN}x)"
            ),
        ),
    ]


def _dependency_negation_check(measurement: Measurement) -> Check:
    """Stands in for "no latency step outside the population" and "no error
    step outside the population": the fault's population is the whole run
    (a `service.component` clause), so there is no outside population for
    either of those to compare against. This is the verify-by-negation query
    for that case instead: a span the fault does not touch should not move.
    """
    before_p99 = measurement.negation_before_p99
    after_p99 = measurement.negation_after_p99
    if before_p99 is None or after_p99 is None:
        return Check(
            name=f"no latency step on {DEPENDENCY_NEGATION_SPAN} (verify by negation)",
            ok=False,
            detail="the negation query did not run",
        )
    step = _ratio(after_p99, before_p99)
    return Check(
        name=f"no latency step on {DEPENDENCY_NEGATION_SPAN} (verify by negation)",
        ok=step < OUTSIDE_STEP_MAX,
        detail=(
            f"{DEPENDENCY_NEGATION_SPAN} P99 {before_p99:.1f}ms before onset, "
            f"{after_p99:.1f}ms after, {step:.2f}x (must stay under {OUTSIDE_STEP_MAX}x); "
            "stands in for the outside-the-population checks, which have no population to "
            "compare against here"
        ),
    )


def _error_checks_full_population(scenario: Scenario, measurement: Measurement) -> list[Check]:
    """`_error_checks` minus the "outside" comparison, which does not exist
    when the fault's population is the whole run."""
    assert scenario.fault is not None
    injected = scenario.fault.effect.error_rate
    before, after = measurement.before, measurement.after
    inside_step = _ratio(after.inside_error_rate, max(before.inside_error_rate, ERROR_RATE_FLOOR))
    floor = injected * ERROR_RATE_FRACTION_MIN
    return [
        Check(
            name="error rate inside the population reaches the injected rate",
            ok=after.inside_error_rate >= floor,
            detail=(
                f"{after.inside_error_rate:.3f} after onset against an injected {injected:.3f} "
                f"(need at least {floor:.3f})"
            ),
        ),
        Check(
            name="error step inside the population",
            ok=inside_step >= ERROR_STEP_MIN,
            detail=(
                f"error rate {before.inside_error_rate:.4f} before onset, "
                f"{after.inside_error_rate:.4f} after, {inside_step:.1f}x "
                f"(need at least {ERROR_STEP_MIN}x)"
            ),
        ),
    ]


def _burst_checks(measurement: Measurement) -> list[Check]:
    """Two checks per red herring with a `duration_min`: the burst happened,
    and it stopped on its own. `measurement.bursts` is empty for a scenario
    with no such herring, so this is a no-op for control-quiet."""
    checks: list[Check] = []
    for burst in measurement.bursts:
        checks.extend(_one_burst_checks(burst))
    return checks


def _one_burst_checks(burst: BurstMeasurement) -> list[Check]:
    effect = burst.herring.effect
    label = ", ".join(f"{k}={v}" for k, v in burst.herring.where.items())
    if effect.error_rate:
        rest_errors = burst.before.inside_errors + burst.after.inside_errors
        rest_count = burst.before.inside_count + burst.after.inside_count
        rest_rate = rest_errors / rest_count if rest_count else 0.0
        burst_rate = burst.burst.inside_error_rate
        floor = effect.error_rate * ERROR_RATE_FRACTION_MIN
        step = _ratio(burst_rate, max(rest_rate, ERROR_RATE_FLOOR))
        decay = _ratio(
            burst.after.inside_error_rate, max(burst.before.inside_error_rate, ERROR_RATE_FLOOR)
        )
        return [
            Check(
                name=f"red herring burst reaches its rate and steps above the rest ({label})",
                ok=(burst_rate >= floor) and (step >= ERROR_STEP_MIN),
                detail=(
                    f"burst error rate {burst_rate:.3f} (floor {floor:.3f}), {step:.1f}x the "
                    f"rest-of-window rate {rest_rate:.4f} (need at least {ERROR_STEP_MIN}x)"
                ),
            ),
            Check(
                name=f"red herring resolves after the burst ({label})",
                ok=decay < OUTSIDE_ERROR_STEP_MAX,
                detail=(
                    f"error rate {burst.before.inside_error_rate:.4f} before the burst, "
                    f"{burst.after.inside_error_rate:.4f} after, {decay:.2f}x "
                    f"(must stay under {OUTSIDE_ERROR_STEP_MAX}x)"
                ),
            ),
        ]
    # A latency burst: the same shape, on P99 instead of the error rate.
    rest_p99 = _weighted_p99(burst.before, burst.after)
    step = _ratio(burst.burst.inside_p99, rest_p99)
    decay = _ratio(burst.after.inside_p99, burst.before.inside_p99)
    return [
        Check(
            name=f"red herring burst steps above the rest of the window ({label})",
            ok=step >= LATENCY_STEP_MIN,
            detail=(
                f"burst P99 {burst.burst.inside_p99:.1f}ms, {step:.2f}x the rest-of-window "
                f"P99 {rest_p99:.1f}ms (need at least {LATENCY_STEP_MIN}x)"
            ),
        ),
        Check(
            name=f"red herring resolves after the burst ({label})",
            ok=decay < OUTSIDE_STEP_MAX,
            detail=(
                f"P99 {burst.before.inside_p99:.1f}ms before the burst, "
                f"{burst.after.inside_p99:.1f}ms after, {decay:.2f}x "
                f"(must stay under {OUTSIDE_STEP_MAX}x)"
            ),
        ),
    ]


def _weighted_p99(before: WindowStats, after: WindowStats) -> float:
    """A rough P99 for "the rest of the window": the two halves' P99s, weighted
    by row count. Approximate on purpose, it only needs to be a baseline the
    burst clearly steps above."""
    total = before.inside_count + after.inside_count
    if not total:
        return 0.0
    return (before.inside_p99 * before.inside_count + after.inside_p99 * after.inside_count) / total


def _herring_survives_checks(scenario: Scenario, measurement: Measurement) -> list[Check]:
    """One check per red herring on an incident scenario: excluding the
    herring's population, the fault's own step still holds. Proves the
    incident is not an artifact of the herring's population, the way
    `herring-customer-whale`'s ground truth depends on it: the whale holds
    30% of all errors over the window, but adyen's step is what survives
    with the whale excluded."""
    if scenario.fault is None:
        return []
    return [
        _one_herring_survives_check(scenario, survival) for survival in measurement.herring_survival
    ]


def _one_herring_survives_check(scenario: Scenario, survival: HerringSurvivalMeasurement) -> Check:
    assert scenario.fault is not None
    effect = scenario.fault.effect
    label = ", ".join(f"{k}={v}" for k, v in survival.herring.where.items())
    name = f"fault step survives excluding the red herring ({label})"
    before, after = survival.before, survival.after
    if effect.error_rate:
        step = _ratio(after.outside_error_rate, max(before.outside_error_rate, ERROR_RATE_FLOOR))
        return Check(
            name=name,
            ok=step >= ERROR_STEP_MIN,
            detail=(
                f"excluding {label}: error rate {before.outside_error_rate:.4f} before onset, "
                f"{after.outside_error_rate:.4f} after, {step:.1f}x (need at least "
                f"{ERROR_STEP_MIN}x)"
            ),
        )
    step = _ratio(after.outside_p99, before.outside_p99)
    return Check(
        name=name,
        ok=step >= LATENCY_STEP_MIN,
        detail=(
            f"excluding {label}: P99 {before.outside_p99:.1f}ms before onset, "
            f"{after.outside_p99:.1f}ms after, {step:.2f}x (need at least {LATENCY_STEP_MIN}x)"
        ),
    )


def _check_trigger(scenario: Scenario, measurement: Measurement) -> Check | None:
    if scenario.trigger is None:
        return None
    text = measurement.trigger_text
    if not text:
        return Check(
            name="trigger fired",
            ok=False,
            detail=f"get_triggers did not run for trigger {scenario.trigger.id}",
        )
    table = fmt.parse_results_table(text, heading="# Triggers")
    if table is None:
        return Check(
            name="trigger fired",
            ok=False,
            detail="could not parse a triggers table out of get_triggers' result",
        )
    headers, rows = table
    if "id" not in headers or "triggered" not in headers:
        return Check(
            name="trigger fired",
            ok=False,
            detail=f"the triggers table has no id/triggered column: {headers}",
        )
    id_i, triggered_i = headers.index("id"), headers.index("triggered")
    row = next((r for r in rows if len(r) > id_i and r[id_i] == scenario.trigger.id), None)
    if row is None:
        return Check(
            name="trigger fired",
            ok=False,
            detail=f"trigger {scenario.trigger.id} ({scenario.trigger.name}) not in get_triggers",
        )
    triggered = row[triggered_i].strip().lower() == "true"
    return Check(
        name="trigger fired",
        ok=triggered,
        detail=(
            f"trigger {scenario.trigger.id} ({scenario.trigger.name}): "
            f"triggered={row[triggered_i]!r}. This reads the trigger's current state, not its "
            "history: it passes if any run fired it, this one included, and fails if verify "
            "runs after the triggered window has passed."
        ),
    )


def _check_exception(
    scenario: Scenario, manifest: EmitResult, measurement: Measurement
) -> list[Check]:
    """Two checks, only when the fault's effect carries an `exception`.

    The first reads the one trace fetched with `get_trace(show_events=true)`:
    Honeycomb renders a span event as its own row, with `name` the event's
    name and `annotation_type = span_event`, hanging off the failed span by
    `parent_id`, but carrying none of the event's own attributes in that
    table. So this check can only confirm the event exists, not its type;
    the second check, a `run_query` filtered on `exception.type`, is what
    proves the type and is exact against `manifest.span_events`, the same
    way the ingest check is exact.
    """
    if scenario.fault is None or scenario.fault.effect.exception is None:
        return []
    exc = scenario.fault.effect.exception
    return [
        _check_exception_event(scenario, measurement),
        _check_exception_count(manifest, measurement, exc),
    ]


def _check_exception_event(scenario: Scenario, measurement: Measurement) -> Check:
    assert scenario.fault is not None
    trace_id = measurement.exception_trace_id
    text = measurement.exception_trace_text
    span_name = scenario.fault.effect.span
    if not text:
        return Check(
            name="exception event recorded",
            ok=False,
            detail="no failed trace in the fault's population was found to inspect",
        )
    table = fmt.parse_results_table(text, heading=None)
    if table is None:
        return Check(
            name="exception event recorded",
            ok=False,
            detail=f"trace {trace_id}: could not parse a span table out of get_trace's result",
        )
    headers, rows = table
    needed = {"span_id", "parent_id", "name", "error", "annotation_type"}
    if not needed <= set(headers):
        return Check(
            name="exception event recorded",
            ok=False,
            detail=f"trace {trace_id}: get_trace's table is missing columns from {sorted(needed)}",
        )
    index = {name: i for i, name in enumerate(headers)}

    def cell(row: list[str], key: str) -> str:
        i = index[key]
        return row[i] if i < len(row) else ""

    failed_span_ids = {
        cell(row, "span_id")
        for row in rows
        if cell(row, "name") == span_name and cell(row, "error").strip().lower() == "true"
    }
    hit = next(
        (
            row
            for row in rows
            if cell(row, "annotation_type") == "span_event"
            and cell(row, "name") == "exception"
            and cell(row, "parent_id") in failed_span_ids
        ),
        None,
    )
    return Check(
        name="exception event recorded",
        ok=hit is not None,
        detail=(
            f"trace {trace_id}: "
            + (
                f"a span_event named 'exception' hangs off a failed {span_name} span"
                if hit is not None
                else f"no span_event named 'exception' under a failed {span_name} span"
            )
        ),
    )


def _check_exception_count(
    manifest: EmitResult, measurement: Measurement, exc: ExceptionEffect
) -> Check:
    expected = float(manifest.span_events)
    measured = measurement.exception_event_count
    return Check(
        name="exception event count matches the manifest",
        ok=measured is not None and measured == expected,
        detail=(
            f"{measured if measured is not None else '?'} rows named exception with "
            f"exception.type={exc.type!r}, manifest says {expected:.0f} span events"
        ),
    )


# --------------------------------------------------------------------------
# Measuring, over MCP
# --------------------------------------------------------------------------


def split_points(scenario: Scenario, manifest: EmitResult) -> tuple[str, str, str]:
    """The window start, the split, and the window end, as ISO-8601 strings.

    A scenario with a fault splits at onset. A control has no onset, so it
    splits down the middle, which is the comparison an investigator would make
    when told to look for a change.

    The hosted MCP truncates query time bounds to whole seconds. The start is
    floored and the end is ceilinged so the queried window covers every span
    the manifest says was emitted, and the split is floored so the two halves
    meet where the MCP will actually cut them.
    """
    split = manifest.onset_s
    if split is None:
        split = (manifest.window_start_s + manifest.window_end_s) / 2.0
    return (
        iso(math.floor(manifest.window_start_s)),
        iso(math.floor(split)),
        iso(math.ceil(manifest.window_end_s)),
    )


async def _run(mcp: HoneycombMCP, settings: Settings, spec: dict[str, Any]) -> ToolResult:
    return await mcp.call(
        "run_query",
        {
            "environment_slug": settings.honeycomb_env,
            "dataset_slug": settings.honeycomb_dataset,
            "query_spec": spec,
        },
    )


async def measure(
    scenario: Scenario,
    manifest: EmitResult,
    mcp: HoneycombMCP,
    settings: Settings,
) -> Measurement:
    """Read every number the checks need out of Honeycomb."""
    where = dict(scenario.fault.where) if scenario.fault else {}
    span_name = scenario.fault.effect.span if scenario.fault else topology.ROOT_SPAN
    start, split, end = split_points(scenario, manifest)
    full_population = _is_full_population(scenario)
    wants_outside_errors = (
        bool(where)
        and not full_population
        and bool(scenario.fault and scenario.fault.effect.error_rate)
    )

    windows: list[WindowStats] = []
    for label, (from_time, to_time) in (
        ("before", (start, split)),
        ("after", (split, end)),
    ):
        stats = WindowStats(label=label)
        result = await _run(
            mcp,
            settings,
            window_query_spec(manifest.run_id, span_name, where, start=from_time, end=to_time),
        )
        row = read_row(_result_text(result))
        stats.inside_count = row.get("inside_count", 0.0)
        stats.inside_p99 = row.get("inside_p99", 0.0)
        stats.inside_errors = row.get("inside_errors", 0.0)
        stats.outside_count = row.get("outside_count", 0.0)
        stats.outside_p99 = row.get("outside_p99", 0.0)
        _record_ids(stats.query_ids, stats.permalinks, result)

        if wants_outside_errors:
            errors = await _run(
                mcp,
                settings,
                outside_errors_query_spec(
                    manifest.run_id, span_name, where, start=from_time, end=to_time
                ),
            )
            stats.outside_errors = read_row(_result_text(errors)).get("outside_errors", 0.0)
            _record_ids(stats.query_ids, stats.permalinks, errors)

        windows.append(stats)

    measurement = Measurement(before=windows[0], after=windows[1])

    if full_population:
        assert scenario.fault is not None
        population_spec = full_population_query_spec(
            manifest.run_id,
            topology.ROOT_SPAN,
            scenario.fault.effect.span,
            topology.SPAN_SERVICE[scenario.fault.effect.span],
            start=start,
            end=end,
        )
    else:
        population_spec = population_query_spec(manifest.run_id, where, start=start, end=end)
    population = await _run(mcp, settings, population_spec)
    row = read_row(_result_text(population))
    measurement.total_root_spans = row.get("total_count", 0.0)
    measurement.inside_root_spans = row.get("inside_count", 0.0)
    _record_ids(measurement.query_ids, measurement.permalinks, population)

    if full_population:
        await _measure_negation(measurement, manifest, mcp, settings, start, split, end)

    for herring in scenario.red_herrings:
        if scenario.fault is not None:
            survival = await _measure_herring_survival(
                scenario, herring, manifest, mcp, settings, start, split, end, measurement
            )
            measurement.herring_survival.append(survival)
        if herring.duration_min is not None:
            burst = await _measure_burst(herring, manifest, mcp, settings, measurement)
            measurement.bursts.append(burst)

    if scenario.trigger is not None:
        result = await mcp.call("get_triggers", {"environment_slug": settings.honeycomb_env})
        measurement.trigger_text = _result_text(result)
        _record_ids(measurement.query_ids, measurement.permalinks, result)

    if scenario.fault is not None and scenario.fault.effect.exception is not None:
        await _measure_exception(measurement, scenario, manifest, mcp, settings, start, split, end)

    return measurement


async def _measure_negation(
    measurement: Measurement,
    manifest: EmitResult,
    mcp: HoneycombMCP,
    settings: Settings,
    start: str,
    split: str,
    end: str,
) -> None:
    """P99 of a span the fault does not touch, before and after onset: the
    verify-by-negation query for a fault whose population is the whole run."""
    for attr, from_time, to_time in (
        ("negation_before_p99", start, split),
        ("negation_after_p99", split, end),
    ):
        result = await _run(
            mcp,
            settings,
            window_query_spec(
                manifest.run_id, DEPENDENCY_NEGATION_SPAN, {}, start=from_time, end=to_time
            ),
        )
        setattr(measurement, attr, read_row(_result_text(result)).get("inside_p99", 0.0))
        _record_ids(measurement.query_ids, measurement.permalinks, result)


def herring_windows(manifest: EmitResult, herring: RedHerring) -> tuple[str, str, str, str]:
    """Before-start, burst-start, burst-end, and window-end, floored/ceiled to
    whole seconds the way `split_points` does, for one red herring's burst."""
    assert herring.duration_min is not None
    burst_start = manifest.window_start_s + herring.onset_min * 60.0
    burst_end = burst_start + herring.duration_min * 60.0
    return (
        iso(math.floor(manifest.window_start_s)),
        iso(math.floor(burst_start)),
        iso(math.floor(burst_end)),
        iso(math.ceil(manifest.window_end_s)),
    )


async def _measure_burst(
    herring: RedHerring,
    manifest: EmitResult,
    mcp: HoneycombMCP,
    settings: Settings,
    measurement: Measurement,
) -> BurstMeasurement:
    window_start, burst_start, burst_end, window_end = herring_windows(manifest, herring)
    stats: dict[str, WindowStats] = {}
    for label, (from_time, to_time) in (
        ("before", (window_start, burst_start)),
        ("burst", (burst_start, burst_end)),
        ("after", (burst_end, window_end)),
    ):
        result = await _run(
            mcp,
            settings,
            window_query_spec(
                manifest.run_id, herring.effect.span, herring.where, start=from_time, end=to_time
            ),
        )
        row = read_row(_result_text(result))
        stats[label] = WindowStats(
            label=label,
            inside_count=row.get("inside_count", 0.0),
            inside_p99=row.get("inside_p99", 0.0),
            inside_errors=row.get("inside_errors", 0.0),
        )
        _record_ids(measurement.query_ids, measurement.permalinks, result)
    return BurstMeasurement(
        herring=herring, before=stats["before"], burst=stats["burst"], after=stats["after"]
    )


async def _measure_herring_survival(
    scenario: Scenario,
    herring: RedHerring,
    manifest: EmitResult,
    mcp: HoneycombMCP,
    settings: Settings,
    start: str,
    split: str,
    end: str,
    measurement: Measurement,
) -> HerringSurvivalMeasurement:
    """The fault's span, filtered to the herring's `where`, before and after
    onset: the OUTSIDE numbers (the complement of the herring's population)
    prove the fault's own step survives with the herring excluded. Two
    `run_query` calls, or four when the fault raises the error rate (the
    outside error count needs its own query per window, the same way
    `measure`'s own `wants_outside_errors` does)."""
    assert scenario.fault is not None
    span = scenario.fault.effect.span
    wants_errors = bool(scenario.fault.effect.error_rate)
    stats: dict[str, WindowStats] = {}
    for label, (from_time, to_time) in (("before", (start, split)), ("after", (split, end))):
        result = await _run(
            mcp,
            settings,
            window_query_spec(manifest.run_id, span, herring.where, start=from_time, end=to_time),
        )
        row = read_row(_result_text(result))
        ws = WindowStats(
            label=label,
            outside_count=row.get("outside_count", 0.0),
            outside_p99=row.get("outside_p99", 0.0),
        )
        _record_ids(measurement.query_ids, measurement.permalinks, result)

        if wants_errors:
            errors = await _run(
                mcp,
                settings,
                outside_errors_query_spec(
                    manifest.run_id, span, herring.where, start=from_time, end=to_time
                ),
            )
            ws.outside_errors = read_row(_result_text(errors)).get("outside_errors", 0.0)
            _record_ids(measurement.query_ids, measurement.permalinks, errors)

        stats[label] = ws
    return HerringSurvivalMeasurement(herring=herring, before=stats["before"], after=stats["after"])


def _first_breakdown_value(text: str, column: str) -> str | None:
    """The first row's value for a breakdown column, from a `# Results` table.

    `read_row` only keeps numeric cells; a trace id is not one.
    """
    table = fmt.parse_results_table(text)
    if table is None:
        return None
    headers, rows = table
    if column not in headers or not rows:
        return None
    return rows[0][headers.index(column)]


def exception_count_query_spec(
    run_id: str,
    exception_type: str,
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    """How many `exception` span_event rows this run has, of the configured type.

    Honeycomb copies a span event's own attributes onto the row it lands as
    (`name = exception`, no `scenario.run_id`, see the module docstring), but
    it also copies them onto the parent span's row, so `exception.type` is a
    real filter on the event rows themselves. Compared against
    `manifest.span_events` for exactness, the same way the ingest check is
    exact.
    """
    return {
        "calculations": [{"op": "COUNT", "name": "exception_count"}],
        "filters": [
            {"column": "scenario.run_id", "op": "=", "value": run_id},
            {"column": "name", "op": "=", "value": "exception"},
            {"column": "exception.type", "op": "=", "value": exception_type},
        ],
        "from": start,
        "to": end,
    }


async def _measure_exception(
    measurement: Measurement,
    scenario: Scenario,
    manifest: EmitResult,
    mcp: HoneycombMCP,
    settings: Settings,
    start: str,
    split: str,
    end: str,
) -> None:
    """Find one trace the fault failed after onset and fetch it with events,
    then count every exception row of the configured type over the run."""
    assert scenario.fault is not None
    exc = scenario.fault.effect.exception
    assert exc is not None
    spec = {
        "calculations": [{"op": "COUNT"}],
        "filters": [
            {"column": "scenario.run_id", "op": "=", "value": manifest.run_id},
            {"column": "name", "op": "=", "value": scenario.fault.effect.span},
            {"column": "error", "op": "=", "value": True},
            *_eq_filters(scenario.fault.where),
        ],
        "breakdowns": ["trace.trace_id"],
        "orders": [{"op": "COUNT", "order": "descending"}],
        "limit": 1,
        "from": split,
        "to": end,
    }
    result = await _run(mcp, settings, spec)
    _record_ids(measurement.query_ids, measurement.permalinks, result)
    trace_id = _first_breakdown_value(_result_text(result), "trace.trace_id")
    if trace_id is not None:
        measurement.exception_trace_id = trace_id
        trace_result = await mcp.call(
            "get_trace",
            {
                "environment_slug": settings.honeycomb_env,
                "trace_id": trace_id,
                "show_events": True,
                "view_mode": "full",
                "from": split,
                "to": end,
            },
        )
        measurement.exception_trace_text = _result_text(trace_result)
        _record_ids(measurement.query_ids, measurement.permalinks, trace_result)

    # Events sit at their span's end time, which can fall a fraction of a
    # second past the window's own ceiled end (a span that starts just before
    # the window closes still runs its full duration). Ten seconds of slack
    # on this one query keeps a right-at-the-edge exception from landing
    # outside `to` and being missed.
    count_end = iso(math.ceil(manifest.window_end_s) + 10.0)
    count_result = await _run(
        mcp,
        settings,
        exception_count_query_spec(manifest.run_id, exc.type, start=start, end=count_end),
    )
    measurement.exception_event_count = read_row(_result_text(count_result)).get(
        "exception_count", 0.0
    )
    _record_ids(measurement.query_ids, measurement.permalinks, count_result)


def _record_ids(query_ids: list[str], permalinks: list[str], result: ToolResult) -> None:
    if result.query_id:
        query_ids.append(result.query_id)
    if result.permalink:
        permalinks.append(result.permalink)


async def verify(
    scenario: Scenario,
    manifest: EmitResult,
    settings: Settings | None = None,
) -> VerifyResult:
    """Measure the run and decide. One MCP session.

    Five `run_query` calls at most for a plain fault or control. A control
    with a burst herring adds three; a dependency fault (a full-population
    `where`) adds two for its negation query and drops two by skipping the
    now-meaningless outside-errors query; a trigger scenario adds one
    `get_triggers` call; a fault whose effect carries an `exception` adds one
    `run_query` and one `get_trace`. All inside the MCP client's pacing.
    """
    settings = settings or Settings()
    async with HoneycombMCP(settings=settings) as mcp:
        measurement = await measure(scenario, manifest, mcp, settings)
    return VerifyResult(
        scenario_id=scenario.id,
        run_id=manifest.run_id,
        checks=decide(scenario, manifest, measurement),
        measurement=measurement,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render(result: VerifyResult, manifest: EmitResult) -> str:
    """The report one verification prints."""
    lines = [
        f"scenario: {result.scenario_id}",
        f"run_id:   {result.run_id}",
        f"window:   {manifest.window_start} .. {manifest.window_end}",
    ]
    if manifest.onset is not None:
        lines.append(f"onset:    {manifest.onset}")
    lines.append("")

    measurement = result.measurement
    for stats in (measurement.before, measurement.after):
        lines.append(
            f"{stats.label:>6}: inside count {stats.inside_count:>8.0f} "
            f"P99 {stats.inside_p99:>8.1f}ms errors {stats.inside_error_rate:.4f}"
        )
        if stats.outside_count:
            lines.append(
                f"        outside count {stats.outside_count:>7.0f} "
                f"P99 {stats.outside_p99:>8.1f}ms errors {stats.outside_error_rate:.4f}"
            )
    lines.append("")

    for check in result.checks:
        lines.append(f"[{'pass' if check.ok else 'FAIL'}] {check.name}: {check.detail}")

    permalinks = [
        link
        for source in (measurement.before, measurement.after, measurement)
        for link in source.permalinks
    ]
    if permalinks:
        lines.append("")
        lines.append("queries:")
        lines.extend(f"  {link}" for link in permalinks)

    lines.append("")
    lines.append("VERIFIED" if result.ok else "NOT VERIFIED")
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m gen.verify",
        description="Assert an emitted scenario's fault is visible in Honeycomb.",
    )
    parser.add_argument("--scenario", required=True, help=f"one of: {available_scenarios()}")
    parser.add_argument("--run-id", required=True, help="the run id gen/emit.py printed")
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=RUNS_DIR,
        help="where to find the run manifest, default gen/runs",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        scenario = load_scenario(args.scenario)
        manifest = load_manifest(args.run_id, args.runs_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if manifest.scenario_id != scenario.id:
        print(
            f"error: run {args.run_id} is a {manifest.scenario_id!r} run, not {scenario.id!r}",
            file=sys.stderr,
        )
        return 2

    try:
        result = asyncio.run(verify(scenario, manifest))
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(render(result, manifest))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
