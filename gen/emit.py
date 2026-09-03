"""Emit one scenario's traces into Honeycomb over OTLP/HTTP.

    uv run python -m gen.emit --scenario payments-stripe-v251-uswest

Everything lands in one dataset. In a non-classic Honeycomb environment the
dataset comes from the resource's `service.name`, so the resource carries
`service.name = receipts-shop` and each span names its real service in
`service.component`. The consequence is written up in `gen/README.md`: one
dataset means one schema and one BubbleUp across all four services, and it
means Honeycomb's per-service views are not the way to slice this data.

Two timing modes:

  backdate (the default) sets span timestamps so a twenty minute window ends a
  minute before now, and the whole run ships in seconds. Honeycomb accepts
  timestamps in the recent past.

  realtime sleeps between requests so the window plays out at wall speed. It
  is the fallback if backdated spans ever land in the wrong place, and it is
  the mode to use against a live trigger, which fires on wall-clock time.

Spans are handed to the OpenTelemetry SDK with explicit start and end times
and exported in chunks by a small span processor. The stock BatchSpanProcessor
drops spans once its queue fills, and a generator that produces 90,000 spans in
under a second fills it immediately, so the chunked processor here exports
synchronously against a bounded pool of workers and reports any batch that
failed instead of losing it quietly.

Ingest is paced because of one experiment. The same 90,000 span run was
pushed twice: four concurrent posters at about 6,800 spans per second landed
57,500 of them, and one poster at about 3,300 spans per second landed all
90,000. Every request in both runs came back HTTP 200 with an empty body and
an empty OTLP `partial_success`. An hour later Honeycomb support emailed the
team's limit: 4,000 events per second, with 32,500 events dropped, which is
the shortfall exactly. The email comes at most once per 24 hours and there
is no in-band signal. The default is one poster and a cap of 2,500 spans per
second, and `gen/verify.py` counts what arrived against this manifest before
a run is used as ground truth.

The run writes a manifest to `gen/runs/<run_id>.json` with the time window and
the counts. `gen/verify.py` reads it so a verification does not have to be
handed a time window by hand.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import ValidationError

from gen import topology
from gen.scenario import Scenario, available_scenarios, load_scenario
from receipts.settings import Settings

logger = logging.getLogger(__name__)

RUNS_DIR = Path(__file__).resolve().parent / "runs"

DEFAULT_LAG_S = 60.0
DEFAULT_CHUNK_SIZE = 500
# One poster, paced. The team's ingest limit is 4,000 events per second and
# the only signal for going over it is an email; see the module docstring.
DEFAULT_CONCURRENCY = 1
DEFAULT_MAX_SPANS_PER_SECOND = 2500.0

# Span kinds by name. Kept here rather than in gen/topology.py so the topology
# stays free of OpenTelemetry imports and can be tested on its own.
SPAN_KINDS: dict[str, SpanKind] = {
    topology.ROOT_SPAN: SpanKind.SERVER,
    "checkout.process": SpanKind.INTERNAL,
    "payments.charge": SpanKind.CLIENT,
    "inventory.reserve": SpanKind.INTERNAL,
    "db.query": SpanKind.CLIENT,
}


@dataclass(frozen=True)
class Window:
    """The wall-clock window a run covers."""

    start_s: float
    end_s: float
    mode: str

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def at(self, offset_s: float) -> float:
        return self.start_s + offset_s


def window_bounds(
    now_s: float, minutes: float, *, backdate: bool, lag_s: float = DEFAULT_LAG_S
) -> Window:
    """The window a run covers, given the current time.

    Backdated runs end `lag_s` before now, so the last span is comfortably in
    the past and no clock skew between here and Honeycomb pushes a timestamp
    into the future. Real-time runs start now and end when the run ends.

    Both edges sit on a whole second. The hosted MCP truncates `start_time`
    and `end_time` to whole seconds, so a window that starts at 01:26:33.845
    and is queried from 01:26:33 picks up spans it should not, and a window
    that ends at 01:46:33.845 and is queried to 01:46:33 loses the last
    0.845 s of traffic, which at 15 rps is about a dozen requests.
    """
    span_s = minutes * 60.0
    if backdate:
        end = float(math.floor(now_s - lag_s))
        return Window(start_s=end - span_s, end_s=end, mode="backdate")
    start = float(math.floor(now_s))
    return Window(start_s=start, end_s=start + span_s, mode="realtime")


def span_time_ns(window_start_s: float, offset_ms: float) -> int:
    """Absolute nanoseconds for a span offset, in milliseconds, into the window.

    The window start and the offset are converted separately and added as
    integers. A float epoch in nanoseconds is around 1.8e18, past the 53 bits
    of a double, so multiplying the sum would quantise every timestamp to about
    a quarter of a microsecond and let a child span round outside its parent.
    """
    return int(round(window_start_s * 1_000_000_000)) + int(round(offset_ms * 1_000_000))


def iso(epoch_s: float) -> str:
    """An ISO-8601 UTC timestamp, which is what the Honeycomb query API wants."""
    return datetime.fromtimestamp(epoch_s, tz=UTC).isoformat().replace("+00:00", "Z")


def new_run_id() -> str:
    """A fresh run id. Every span in the run carries it, and every query filters on it."""
    return f"run-{uuid.uuid4().hex[:12]}"


class ChunkedSpanProcessor(SpanProcessor):
    """Exports finished spans in fixed chunks, without a drop-on-full queue.

    `on_end` appends to a buffer; a full buffer is submitted to a small pool of
    worker threads, each with its own exporter, because an OTLP HTTP exporter
    holds a session that is not meant to be shared. In-flight work is bounded,
    so a fast generator waits for the network instead of overrunning it.
    Failed batches are counted and reported; nothing is dropped silently.
    """

    def __init__(
        self,
        make_exporter: Callable[[], SpanExporter],
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        concurrency: int = DEFAULT_CONCURRENCY,
        max_spans_per_second: float = DEFAULT_MAX_SPANS_PER_SECOND,
        on_progress: Callable[[int], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._make_exporter = make_exporter
        self._chunk_size = chunk_size
        self._pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="otlp")
        self._max_in_flight = max(1, concurrency * 2)
        self._max_rate = max_spans_per_second
        self._local = threading.local()
        self._buffer: list[ReadableSpan] = []
        self._in_flight: deque[tuple[Future[SpanExportResult], int]] = deque()
        self._lock = threading.Lock()
        self._on_progress = on_progress
        self._clock = clock
        self._sleep = sleep
        self._started = clock()
        self._submitted = 0
        self.exported = 0
        self.failed_batches = 0
        self.failed_spans = 0

    def _exporter(self) -> SpanExporter:
        exporter = getattr(self._local, "exporter", None)
        if exporter is None:
            exporter = self._make_exporter()
            self._local.exporter = exporter
        return exporter

    def _export(self, batch: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._exporter().export(batch)

    def on_start(self, span: ReadableSpan, parent_context: Context | None = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        self._buffer.append(span)
        if len(self._buffer) >= self._chunk_size:
            self._submit()

    def _pace(self, batch_size: int) -> None:
        """Hold the submission rate under `max_spans_per_second`."""
        if self._max_rate <= 0:
            return
        self._submitted += batch_size
        delay = (self._started + self._submitted / self._max_rate) - self._clock()
        if delay > 0:
            self._sleep(delay)

    def _submit(self) -> None:
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        self._pace(len(batch))
        self._in_flight.append((self._pool.submit(self._export, batch), len(batch)))
        self._drain(self._max_in_flight)

    def _drain(self, keep: int) -> None:
        while len(self._in_flight) > keep:
            future, size = self._in_flight.popleft()
            self._collect(future, size)

    def _collect(self, future: Future[SpanExportResult], size: int) -> None:
        try:
            result = future.result()
        except Exception:
            logger.exception("otlp export raised")
            result = SpanExportResult.FAILURE
        with self._lock:
            if result is SpanExportResult.SUCCESS:
                self.exported += size
            else:
                self.failed_batches += 1
                self.failed_spans += size
        if self._on_progress is not None:
            self._on_progress(self.exported)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self._submit()
        self._drain(0)
        return self.failed_batches == 0

    def shutdown(self) -> None:
        self.force_flush()
        self._pool.shutdown(wait=True)


class NullExporter(SpanExporter):
    """Counts spans and sends nothing. Used by --dry-run and by the tests."""

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


@dataclass
class EmitResult:
    """What a run produced, written to `gen/runs/<run_id>.json`."""

    run_id: str
    scenario_id: str
    dataset: str
    environment: str
    mode: str
    seed: int
    rps: float
    minutes: float
    window_start_s: float
    window_end_s: float
    window_start: str
    window_end: str
    onset_s: float | None
    onset: str | None
    requests: int
    spans: int
    exported: int
    failed_batches: int
    fault_population: int
    faulted_requests: int
    errored_requests: int
    emitted_at: str = field(default_factory=lambda: iso(time.time()))

    def manifest_path(self, runs_dir: Path = RUNS_DIR) -> Path:
        return runs_dir / f"{self.run_id}.json"

    def write(self, runs_dir: Path = RUNS_DIR) -> Path:
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = self.manifest_path(runs_dir)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")
        return path


def load_manifest(run_id: str, runs_dir: Path = RUNS_DIR) -> EmitResult:
    """Read back the manifest a run wrote."""
    path = runs_dir / f"{run_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no manifest for run {run_id!r} at {path}")
    return EmitResult(**json.loads(path.read_text()))


def _make_otlp_exporter(settings: Settings) -> SpanExporter:
    endpoint = settings.honeycomb_otlp_endpoint.rstrip("/") + "/v1/traces"
    return OTLPSpanExporter(
        endpoint=endpoint,
        headers={"x-honeycomb-team": settings.honeycomb_ingest_key.get_secret_value()},
        timeout=60,
    )


def _emit_span(
    tracer: trace.Tracer,
    record: topology.SpanRecord,
    parent_context: Context | None,
    trace_start_s: float,
    parent_offset_ms: float,
    common: dict[str, object],
) -> None:
    """Turn one generated span and its children into real SDK spans."""
    start_ms = parent_offset_ms + record.start_offset_ms
    attributes: dict[str, object] = {**common, **record.attributes}
    span = tracer.start_span(
        record.name,
        context=parent_context if parent_context is not None else Context(),
        kind=SPAN_KINDS.get(record.name, SpanKind.INTERNAL),
        attributes=attributes,
        start_time=span_time_ns(trace_start_s, start_ms),
    )
    if record.error:
        span.set_status(Status(StatusCode.ERROR, "request failed"))
    child_context = trace.set_span_in_context(span)
    for child in record.children:
        _emit_span(tracer, child, child_context, trace_start_s, start_ms, common)
    span.end(end_time=span_time_ns(trace_start_s, start_ms + record.duration_ms))


def emit(
    scenario: Scenario,
    settings: Settings,
    *,
    seed: int = 0,
    backdate: bool = True,
    lag_s: float = DEFAULT_LAG_S,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_spans_per_second: float = DEFAULT_MAX_SPANS_PER_SECOND,
    dry_run: bool = False,
    make_exporter: Callable[[], SpanExporter] | None = None,
    run_id: str | None = None,
    now_s: float | None = None,
    on_progress: Callable[[int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> EmitResult:
    """Generate and ship one run of `scenario`. Returns the run's manifest."""
    run_id = run_id or new_run_id()
    now_s = time.time() if now_s is None else now_s
    window = window_bounds(now_s, scenario.baseline.minutes, backdate=backdate, lag_s=lag_s)

    requests = topology.generate_requests(scenario, seed=seed)

    resource = Resource.create({"service.name": settings.honeycomb_dataset})
    if make_exporter is None:
        make_exporter = NullExporter if dry_run else (lambda: _make_otlp_exporter(settings))
    processor = ChunkedSpanProcessor(
        make_exporter,
        chunk_size=chunk_size,
        concurrency=concurrency,
        max_spans_per_second=0.0 if dry_run else max_spans_per_second,
        on_progress=on_progress,
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("gen.emit")

    # scenario.id is already on every span from gen/topology.py; the run id is
    # the one attribute only this run knows.
    common: dict[str, object] = {"scenario.run_id": run_id}

    try:
        for request in requests:
            if window.mode == "realtime":
                due = window.at(request.offset_s)
                delay = due - time.time()
                if delay > 0:
                    sleep(delay)
            _emit_span(tracer, request.root, None, window.at(request.offset_s), 0.0, common)
    finally:
        processor.shutdown()

    onset_s = None if scenario.fault is None else window.at(scenario.fault.onset_min * 60.0)
    result = EmitResult(
        run_id=run_id,
        scenario_id=scenario.id,
        dataset=settings.honeycomb_dataset,
        environment=settings.honeycomb_env,
        mode=window.mode + (" (dry run, nothing sent)" if dry_run else ""),
        seed=seed,
        rps=scenario.baseline.rps,
        minutes=scenario.baseline.minutes,
        window_start_s=window.start_s,
        window_end_s=window.end_s,
        window_start=iso(window.start_s),
        window_end=iso(window.end_s),
        onset_s=onset_s,
        onset=None if onset_s is None else iso(onset_s),
        requests=len(requests),
        spans=topology.span_count(requests),
        exported=processor.exported,
        failed_batches=processor.failed_batches,
        fault_population=sum(1 for r in requests if r.in_fault_population),
        faulted_requests=sum(1 for r in requests if r.faulted),
        errored_requests=sum(1 for r in requests if r.root.error),
    )
    return result


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m gen.emit",
        description="Emit one scenario's traces into Honeycomb over OTLP/HTTP.",
    )
    parser.add_argument("--scenario", required=True, help=f"one of: {available_scenarios()}")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed, default 0")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--backdate",
        dest="backdate",
        action="store_true",
        default=True,
        help="timestamp the window so it ends just before now (the default)",
    )
    mode.add_argument(
        "--realtime",
        dest="backdate",
        action="store_false",
        help="play the window out at wall speed instead",
    )
    parser.add_argument("--lag-seconds", type=float, default=DEFAULT_LAG_S)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="parallel OTLP posters; more than one has cost spans, see the module docstring",
    )
    parser.add_argument(
        "--max-spans-per-second",
        type=float,
        default=DEFAULT_MAX_SPANS_PER_SECOND,
        help="ingest pacing; 0 turns it off",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build every span and print the counts, send nothing",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        scenario = load_scenario(args.scenario)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        settings = Settings()
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]) for err in exc.errors())
        print(f"error: missing or invalid in .env: {missing}", file=sys.stderr)
        return 2
    started = time.time()
    result = emit(
        scenario,
        settings,
        seed=args.seed,
        backdate=args.backdate,
        lag_s=args.lag_seconds,
        chunk_size=args.chunk_size,
        concurrency=args.concurrency,
        max_spans_per_second=args.max_spans_per_second,
        dry_run=args.dry_run,
    )
    elapsed = time.time() - started

    if not args.dry_run:
        path = result.write()
        print(f"manifest: {path}")

    print(f"run_id:      {result.run_id}")
    print(f"scenario:    {result.scenario_id}")
    print(f"environment: {result.environment}")
    print(f"dataset:     {result.dataset}")
    print(f"mode:        {result.mode}")
    print(f"window:      {result.window_start} .. {result.window_end}")
    if result.onset is not None:
        print(f"onset:       {result.onset}")
    print(f"requests:    {result.requests}")
    print(f"spans:       {result.spans}")
    print(f"exported:    {result.exported} in {elapsed:.1f}s")
    print(f"in fault population: {result.fault_population}")
    print(f"faulted requests:    {result.faulted_requests}")
    print(f"errored requests:    {result.errored_requests}")

    if result.failed_batches:
        print(f"error: {result.failed_batches} export batches failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
