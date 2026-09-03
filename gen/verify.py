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

  control
    No latency step and no error step on the root span between the first half
    of the window and the second.

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
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent import format as fmt
from agent.mcp_client import HoneycombMCP, ToolResult
from gen import topology
from gen.emit import RUNS_DIR, EmitResult, load_manifest
from gen.scenario import Scenario, available_scenarios, load_scenario
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
class Measurement:
    """Everything one verification read out of Honeycomb."""

    before: WindowStats
    after: WindowStats
    total_root_spans: float = 0.0
    inside_root_spans: float = 0.0
    query_ids: list[str] = field(default_factory=list)
    permalinks: list[str] = field(default_factory=list)

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


def _eq_filters(where: Mapping[str, str]) -> list[dict[str, Any]]:
    return [{"column": column, "op": "=", "value": value} for column, value in where.items()]


def _ne_filters(where: Mapping[str, str]) -> list[dict[str, Any]]:
    """The complement of `where`, by De Morgan: NOT(a AND b) is (not a) OR (not b)."""
    return [{"column": column, "op": "!=", "value": value} for column, value in where.items()]


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
        "start_time": start,
        "end_time": end,
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
        "start_time": start,
        "end_time": end,
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
        "start_time": start,
        "end_time": end,
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


def decide(scenario: Scenario, manifest: EmitResult, measurement: Measurement) -> list[Check]:
    """Every check for this scenario, from numbers that are already measured."""
    checks: list[Check] = [
        _check_ingest(manifest, measurement),
        _check_rows(measurement),
    ]
    if scenario.fault is None:
        checks += _control_checks(measurement)
    else:
        checks.append(_check_affected_share(scenario, measurement))
        if scenario.fault.effect.latency_add_ms:
            checks += _latency_checks(measurement)
        if scenario.fault.effect.error_rate:
            checks += _error_checks(scenario, measurement)
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
    import math

    from gen.emit import iso

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
    wants_outside_errors = bool(where) and bool(scenario.fault and scenario.fault.effect.error_rate)

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

    population = await _run(
        mcp, settings, population_query_spec(manifest.run_id, where, start=start, end=end)
    )
    row = read_row(_result_text(population))
    measurement.total_root_spans = row.get("total_count", 0.0)
    measurement.inside_root_spans = row.get("inside_count", 0.0)
    _record_ids(measurement.query_ids, measurement.permalinks, population)

    return measurement


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
    """Measure the run and decide. One MCP session, at most five queries."""
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
