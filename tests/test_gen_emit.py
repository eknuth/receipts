"""The emitter: time arithmetic, span construction, export accounting, manifests.

Nothing here sends anything. `NullExporter` collects the spans the SDK
produces, so the tests can assert on the same objects that would have gone on
the wire.
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import SpanKind, StatusCode

from gen import emit as E
from gen import topology
from gen.scenario import Scenario, load_scenario
from receipts.settings import Settings

NOW = 1_788_000_000.0  # a fixed "now" so every timestamp in these tests is exact


def shrink(scenario_id: str, minutes: float = 2.0, rps: float = 5.0) -> Scenario:
    scenario = load_scenario(scenario_id)
    scenario.baseline.minutes = minutes
    scenario.baseline.rps = rps
    return scenario


# --------------------------------------------------------------------------
# Time arithmetic
# --------------------------------------------------------------------------


def test_backdated_window_ends_before_now_and_runs_backwards_from_there() -> None:
    window = E.window_bounds(NOW, 20, backdate=True, lag_s=60.0)
    assert window.mode == "backdate"
    assert window.end_s == NOW - 60.0
    assert window.start_s == NOW - 60.0 - 1200.0
    assert window.duration_s == 1200.0
    assert window.end_s < NOW


def test_realtime_window_starts_now_and_ends_in_the_future() -> None:
    window = E.window_bounds(NOW, 20, backdate=False)
    assert window.mode == "realtime"
    assert window.start_s == NOW
    assert window.end_s == NOW + 1200.0


def test_lag_is_what_keeps_a_backdated_window_clear_of_the_present() -> None:
    assert E.window_bounds(NOW, 5, backdate=True, lag_s=0.0).end_s == NOW
    assert E.window_bounds(NOW, 5, backdate=True, lag_s=300.0).end_s == NOW - 300.0


def test_window_at_offsets_from_the_start() -> None:
    window = E.window_bounds(NOW, 20, backdate=True, lag_s=60.0)
    assert window.at(0.0) == window.start_s
    assert window.at(600.0) == window.start_s + 600.0


def test_span_time_is_nanoseconds_from_the_window_start() -> None:
    assert E.span_time_ns(NOW, 0.0) == int(NOW * 1_000_000_000)
    assert E.span_time_ns(NOW, 1.5) == int(NOW * 1_000_000_000) + 1_500_000
    assert E.span_time_ns(NOW, 1000.0) == int((NOW + 1.0) * 1_000_000_000)


def test_iso_is_utc_with_a_z() -> None:
    assert E.iso(0.0) == "1970-01-01T00:00:00Z"
    assert E.iso(NOW).endswith("Z")


def test_run_ids_are_fresh_every_time() -> None:
    assert E.new_run_id() != E.new_run_id()
    assert E.new_run_id().startswith("run-")


# --------------------------------------------------------------------------
# What actually gets built
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def emitted(settings_module: Settings) -> tuple[E.EmitResult, list]:
    """One dry run, with an exporter the test holds on to."""
    exporter = E.NullExporter()
    result = E.emit(
        shrink("payments-stripe-v251-uswest"),
        settings_module,
        dry_run=True,
        run_id="run-fixed",
        now_s=NOW,
        chunk_size=100,
        make_exporter=lambda: exporter,
    )
    return result, exporter.spans


def test_the_manifest_counts_match_the_generated_stream(
    emitted: tuple[E.EmitResult, list],
) -> None:
    result, spans = emitted
    assert result.requests == 600
    assert result.spans == 3000
    assert len(spans) == 3000
    assert result.exported == 3000
    assert result.failed_batches == 0
    assert result.run_id == "run-fixed"


def test_every_span_carries_the_run_id_and_the_scenario_id(
    emitted: tuple[E.EmitResult, list],
) -> None:
    _, spans = emitted
    assert all(s.attributes["scenario.run_id"] == "run-fixed" for s in spans)
    assert all(s.attributes["scenario.id"] == "payments-stripe-v251-uswest" for s in spans)


def test_every_span_lands_inside_the_declared_window(
    emitted: tuple[E.EmitResult, list],
) -> None:
    result, spans = emitted
    start_ns = int(result.window_start_s * 1_000_000_000)
    end_ns = int(result.window_end_s * 1_000_000_000)
    assert min(s.start_time for s in spans) >= start_ns
    # The last request starts just inside the window, so its root span may run
    # a few hundred milliseconds past the end. A second of slack covers it.
    assert max(s.end_time for s in spans) <= end_ns + 1_000_000_000


def test_a_backdated_run_is_entirely_in_the_past(emitted: tuple[E.EmitResult, list]) -> None:
    result, _ = emitted
    assert result.window_end_s < NOW
    assert result.mode.startswith("backdate")


def test_span_durations_survive_the_round_trip(emitted: tuple[E.EmitResult, list]) -> None:
    _, spans = emitted
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 600
    for span in roots[:20]:
        assert (span.end_time - span.start_time) > 0


def test_children_point_at_their_parent_and_share_a_trace(
    emitted: tuple[E.EmitResult, list],
) -> None:
    _, spans = emitted
    by_span_id = {s.context.span_id: s for s in spans}
    children = [s for s in spans if s.parent is not None]
    assert len(children) == 2400
    for child in children[:50]:
        parent = by_span_id[child.parent.span_id]
        assert parent.context.trace_id == child.context.trace_id
        assert child.start_time >= parent.start_time
        assert child.end_time <= parent.end_time


def test_span_kinds_follow_the_topology(emitted: tuple[E.EmitResult, list]) -> None:
    _, spans = emitted
    kinds = {s.name: s.kind for s in spans}
    assert kinds[topology.ROOT_SPAN] is SpanKind.SERVER
    assert kinds["payments.charge"] is SpanKind.CLIENT
    assert kinds["db.query"] is SpanKind.CLIENT
    assert kinds["checkout.process"] is SpanKind.INTERNAL


def test_a_failing_span_carries_an_error_status(emitted: tuple[E.EmitResult, list]) -> None:
    _, spans = emitted
    failed = [s for s in spans if s.attributes.get("error") is True]
    assert failed
    assert all(s.status.status_code is StatusCode.ERROR for s in failed)


def test_the_resource_names_the_dataset_not_the_service(
    emitted: tuple[E.EmitResult, list], settings_module: Settings
) -> None:
    """One dataset for all four services: service.name is the dataset, and the
    real service is on service.component."""
    _, spans = emitted
    resource = spans[0].resource
    assert resource.attributes["service.name"] == settings_module.honeycomb_dataset
    assert {s.attributes["service.component"] for s in spans} == set(topology.SERVICES)


def test_a_realtime_run_sleeps_between_requests(settings_module: Settings) -> None:
    slept: list[float] = []
    scenario = shrink("control-quiet", minutes=1, rps=2)
    E.emit(
        scenario,
        settings_module,
        dry_run=True,
        backdate=False,
        now_s=NOW,
        sleep=slept.append,
    )
    # now_s is far in the past, so no request is due later than the real clock
    # and nothing sleeps. The call still has to accept the injected sleep.
    assert slept == []


# --------------------------------------------------------------------------
# Export accounting
# --------------------------------------------------------------------------


class _FlakyExporter(E.NullExporter):
    """Fails every export, so the processor's failure path is exercised."""

    def export(self, spans):  # type: ignore[no-untyped-def]
        return SpanExportResult.FAILURE


def test_a_partial_final_batch_is_counted_by_its_real_size(
    settings_module: Settings,
) -> None:
    """250 spans at a chunk size of 100 is two full batches and one of fifty."""
    exporter = E.NullExporter()
    processor = E.ChunkedSpanProcessor(lambda: exporter, chunk_size=100, max_spans_per_second=0)
    for _ in range(250):
        processor.on_end(object())  # type: ignore[arg-type]
    processor.shutdown()
    assert processor.exported == 250
    assert processor.failed_batches == 0


def test_failed_batches_are_reported_not_swallowed() -> None:
    processor = E.ChunkedSpanProcessor(_FlakyExporter, chunk_size=10, max_spans_per_second=0)
    for _ in range(30):
        processor.on_end(object())  # type: ignore[arg-type]
    processor.shutdown()
    assert processor.exported == 0
    assert processor.failed_batches == 3
    assert processor.failed_spans == 30


def test_an_exporter_that_raises_counts_as_a_failure() -> None:
    class _Boom(E.NullExporter):
        def export(self, spans):  # type: ignore[no-untyped-def]
            raise RuntimeError("nope")

    processor = E.ChunkedSpanProcessor(_Boom, chunk_size=5, max_spans_per_second=0)
    for _ in range(5):
        processor.on_end(object())  # type: ignore[arg-type]
    processor.shutdown()
    assert processor.failed_batches == 1


def test_pacing_holds_the_submission_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A thousand spans at 500 per second waits about two seconds in total."""
    clock = {"now": 0.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    processor = E.ChunkedSpanProcessor(
        E.NullExporter,
        chunk_size=100,
        max_spans_per_second=500.0,
        clock=lambda: clock["now"],
        sleep=sleep,
    )
    for _ in range(1000):
        processor.on_end(object())  # type: ignore[arg-type]
    processor.shutdown()
    assert clock["now"] == pytest.approx(2.0)
    assert processor.exported == 1000


def test_pacing_off_never_sleeps() -> None:
    slept: list[float] = []
    processor = E.ChunkedSpanProcessor(
        E.NullExporter, chunk_size=10, max_spans_per_second=0, sleep=slept.append
    )
    for _ in range(100):
        processor.on_end(object())  # type: ignore[arg-type]
    processor.shutdown()
    assert slept == []


def test_a_future_that_returns_success_counts_its_own_size() -> None:
    processor = E.ChunkedSpanProcessor(E.NullExporter, chunk_size=10, max_spans_per_second=0)
    future: Future[SpanExportResult] = Future()
    future.set_result(SpanExportResult.SUCCESS)
    processor._collect(future, 7)
    assert processor.exported == 7
    processor.shutdown()


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------


def test_a_manifest_round_trips(tmp_path: Path, settings_module: Settings) -> None:
    result = E.emit(
        shrink("payments-stripe-v251-uswest", minutes=1, rps=2),
        settings_module,
        dry_run=True,
        run_id="run-manifest",
        now_s=NOW,
    )
    path = result.write(tmp_path)
    assert json.loads(path.read_text())["run_id"] == "run-manifest"
    assert E.load_manifest("run-manifest", tmp_path) == result


def test_a_missing_manifest_says_so(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="run-nope"):
        E.load_manifest("run-nope", tmp_path)


def test_the_manifest_records_the_onset_for_a_fault_and_none_for_a_control(
    settings_module: Settings,
) -> None:
    fault = E.emit(
        shrink("payments-stripe-v251-uswest", minutes=20, rps=1),
        settings_module,
        dry_run=True,
        now_s=NOW,
    )
    assert fault.onset_s == fault.window_start_s + 600.0
    assert fault.onset is not None

    control = E.emit(
        shrink("control-quiet", minutes=1, rps=1), settings_module, dry_run=True, now_s=NOW
    )
    assert control.onset_s is None
    assert control.onset is None
