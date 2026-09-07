"""Self-telemetry for the investigator: OTel GenAI semconv spans, exported to Honeycomb.

Attribute names come from https://docs.honeycomb.io/send-data/use-cases/agents
and the OTel GenAI semantic conventions it links
(https://github.com/open-telemetry/semantic-conventions-genai, `docs/gen-ai/`).
One root span per run, `invoke_agent receipts-investigator`, with `chat {model}`
and `execute_tool {tool}` children.

Two datasets share the `receipts-demo` environment: `receipts-shop` is the
synthetic traffic under investigation, `receipts-investigator` (this
module's `service.name`) is the agent's own work. Separating them keeps a
BubbleUp over "all requests in the window" from counting the agent's own
tool calls as shop traffic; `agent/mcp_client.py`'s dataset guard enforces
the boundary on every query.

`RunTrace` is opened by the caller that knows the scenario id and the
config, before `investigate()` runs, and passed in as `trace=`.
`agent/__main__.py` calls `end()`, since a CLI run is never graded;
`evals/run.py` grades the report first, then calls `end_with_grade()` or
`end_with_error()`, keeping the span open across that gap. `agent/loop.py`
opens only the `chat_span` and `tool_span` children and sets no attribute
on the root span itself, which is also why the scenario id cannot reach
the model.

`gen_ai.conversation.id` identifies one investigation. Since R8, one emit
serves every config and repeat, and Honeycomb's Agent Timeline groups by
this id, so two investigations sharing a run id would render as one
conversation with duplicated turns. `Telemetry.start_run`
takes a `conversation_id` the caller builds per investigation
(`evals/run.py` uses `f"{run_id}.{config}.{repeat}"`, `agent/__main__.py`
uses `f"{run_id}.cli.{timestamp}"`; a live check found a `/` broke the
Timeline's Traces panel, hence the dots). The run id lands on the root span
as `receipts.run_id`; `scenario.id` lands there too, but only once the span
ends (`RunTrace.end`), so this investigation cannot query for its own answer.

`Telemetry()`, or `Telemetry(settings)` with no ingest key, hands out a
genuine OTel no-op tracer: its spans are `NonRecordingSpan` objects that
accept every call and export nothing. `Telemetry(exporter=...)` is real
regardless of settings, which is how tests see their own spans without
touching the network. Production wires a `Telemetry` explicitly, in
`agent/__main__.py` and `evals/run.py`'s CLI.

Every method here catches its own exceptions and logs a warning. OTel's
`Span.set_attribute` already drops a bad value instead of raising; the
guard covers this module's own JSON building, truncation, and
trace-context work.

A failed tool span carries ERROR and `error.type`; the count lands on the
root as `receipts.tool_errors` when the span ends. A child span's failure
never marks the root, so a run that corrected one bad filter and went on
to file a good report still reads as a success; the root's own ERROR
status comes only from `end_with_error`.

Content capture (`RECEIPTS_CAPTURE_CONTENT=1`) writes the chat history as
`gen_ai.input.messages` and `gen_ai.output.messages` events and the system
prompt as `gen_ai.system_instructions`, following the semconv's message
schema: `{"role", "parts"}`, parts typed `text`, `tool_call`, or
`tool_call_response`, each event a single JSON string attribute since span
events cannot carry the semconv's nested form. `gen_ai.tool.call.arguments`
and `.result` are always on, truncated at 2 KB and 500 characters, since
seeing them is the point of the Agent Timeline.

`end_with_grade` writes `gen_ai.evaluation.result` and one
`receipts.grade.<component>` attribute for `run_query` to break down on,
and one `gen_ai.evaluation.result` span event per score, since Honeycomb's
GenAI tab reads evaluation events.

A live check on 2026-09-03 found no spans from Honeycomb's hosted MCP in
`receipts-demo` for either run's trace. `agent/mcp_client.py` sends
`traceparent` regardless, on the chance a server reads it later.

`Telemetry.start_handoff` (R23, EDW-1370) opens a second root span,
`invoke_agent canvas`, for the Canvas handoff `evals/run.py`'s
`_hand_off_cell` runs after a report is graded: `canvas_agent_invoke`,
`canvas_agent_poll_response`, `create_board`, and `list_boards` each get an
`execute_tool` child through the same `RunTrace.tool_span` the investigation
uses, so the exchange with Canvas shows up in Agent Timeline instead of
existing only in `handoff.json`. See `start_handoff` for why it is a second
root rather than a child of the investigation's own span.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Span, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import SpanKind, Status, StatusCode

from evals.grader import WRONG_BELOW
from receipts.settings import Settings

if TYPE_CHECKING:
    from agent.providers.base import Completion, Turn
    from agent.report import Report

logger = logging.getLogger(__name__)

SERVICE_NAME = "receipts-investigator"
AGENT_NAME = "receipts-investigator"
_TRACER_NAME = "receipts.agent"

# Reused as the pass line for every evaluation event's score.label, not just
# the top hypothesis it was defined for: it is the only threshold the
# grader itself defines, and reusing it keeps the label from disagreeing
# with the grader by carrying a second copy of the number.
_PASS_LINE = WRONG_BELOW

_TOTAL_EXPLANATION = "outcome-graded against scenario ground truth"
_COMPONENT_EXPLANATIONS: dict[str, str] = {
    "dims": "how closely the top hypothesis's dimensions match the ground truth",
    "span": "whether the reported slow or failing span matches the ground truth",
    "incident": "whether the incident_present verdict matches the ground truth",
    "onset": "whether the onset estimate is within tolerance of the true onset",
    "receipts": "whether every reported hypothesis carries evidence and a negation",
    "not_checked": "whether the not_checked list is non-empty and truthful against the tool log",
}

# "JSON, truncated 2 KB" is read as characters here, not exact byte
# accounting: query specs are ASCII JSON, so the two are the same in
# practice, and character truncation cannot split a multi-byte codepoint.
ARGUMENTS_TRUNCATE_CHARS = 2048
RESULT_TRUNCATE_CHARS = 500

# Canvas's reply is prose, not a tool result, and is worth more room on the
# span than RESULT_TRUNCATE_CHARS gives a query result: R23, EDW-1370.
REPLY_TRUNCATE_CHARS = 4096

_TRUNCATION_SUFFIX = "...(truncated)"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = max(0, limit - len(_TRUNCATION_SUFFIX))
    return text[:cut] + _TRUNCATION_SUFFIX


def build_tracer(
    settings: Settings | None = None,
    *,
    exporter: SpanExporter | None = None,
) -> tuple[trace.Tracer, TracerProvider | None]:
    """The tracer this run's spans attach to, and the provider to flush and shut down.

    Real (with `service.name=receipts-investigator`) when an exporter is
    handed in directly, or when `settings` carries an ingest key. A genuine
    OTel no-op tracer otherwise: `settings=None` (the default), or
    `settings.honeycomb_ingest_key is None`. The second return value is None
    exactly when the tracer is a no-op, which is what `Telemetry.enabled`
    reads.
    """
    if exporter is not None:
        processor = SimpleSpanProcessor(exporter)
    elif settings is not None and settings.honeycomb_ingest_key is not None:
        endpoint = settings.honeycomb_otlp_endpoint.rstrip("/") + "/v1/traces"
        real_exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers={"x-honeycomb-team": settings.honeycomb_ingest_key.get_secret_value()},
        )
        processor = BatchSpanProcessor(real_exporter)
    else:
        return trace.NoOpTracerProvider().get_tracer(_TRACER_NAME), None

    # shutdown_on_exit=False: `Telemetry.shutdown()` is the one place this
    # provider is shut down, called explicitly by the caller that owns it
    # (agent/__main__.py, evals/run.py) after the root span ends. Left at its
    # default, the SDK's own atexit hook would shut it down a second time,
    # unguarded by this module's try/except, which is exactly the kind of
    # exporter failure this module exists to keep out of the run.
    provider = TracerProvider(
        resource=Resource.create({"service.name": SERVICE_NAME}), shutdown_on_exit=False
    )
    provider.add_span_processor(processor)
    return provider.get_tracer(_TRACER_NAME), provider


def _mark_span_error(span: Span | None, error_type: str) -> None:
    """ERROR status and `error.type` on `span` only. Never the root: see the
    module docstring's "Errors" paragraph for why a child's failure stays local."""
    if span is None:
        return
    try:
        span.set_status(Status(StatusCode.ERROR))
        span.set_attribute("error.type", error_type)
    except Exception:
        logger.warning("telemetry: could not mark an error on a span", exc_info=True)


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "content": text}


def _input_messages(turns: Sequence[Turn]) -> list[dict[str, Any]]:
    """The transcript as the semconv's message schema: `{"role", "parts"}`,
    parts typed `text`, `tool_call`, or `tool_call_response`. The system
    prompt is not here; it is `gen_ai.system_instructions`, a separate
    event. This codebase does not have a distinct "tool" role: a tool
    result is a `tool_call_response` part on the user-role turn that
    carries it, which is how `agent/providers/base.Turn` already models it.
    """
    messages: list[dict[str, Any]] = []
    for turn in turns:
        parts: list[dict[str, Any]] = []
        if turn.text:
            parts.append(_text_part(turn.text))
        for use in turn.tool_uses:
            parts.append(
                {"type": "tool_call", "id": use.id, "name": use.name, "arguments": use.args}
            )
        for result in turn.tool_results:
            parts.append(
                {
                    "type": "tool_call_response",
                    "id": result.tool_use_id,
                    "result": result.content,
                }
            )
        messages.append({"role": turn.role, "parts": parts})
    return messages


def _output_messages(completion: Completion) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if completion.text:
        parts.append(_text_part(completion.text))
    for use in completion.tool_uses:
        parts.append({"type": "tool_call", "id": use.id, "name": use.name, "arguments": use.args})
    message: dict[str, Any] = {"role": "assistant", "parts": parts}
    if completion.stop_reason:
        message["finish_reason"] = completion.stop_reason
    return [message]


def _system_instructions(system: str) -> list[dict[str, Any]]:
    return [_text_part(system)]


class _ChatSpan:
    """The handle `RunTrace.chat_span` yields, live for one `provider.complete` call."""

    def __init__(self, span: Span | None, *, capture_content: bool) -> None:
        self._span = span
        self._capture_content = capture_content

    def record(self, completion: Completion, *, system: str, turns: Sequence[Turn]) -> None:
        """Everything the span needs once the completion is in hand."""
        if self._span is None:
            return
        try:
            if completion.response_model:
                self._span.set_attribute("gen_ai.response.model", completion.response_model)
            if completion.response_id:
                self._span.set_attribute("gen_ai.response.id", completion.response_id)
            usage = completion.usage
            # gen_ai.usage.input_tokens SHOULD include cached tokens per the
            # semconv; the cache reads and writes are broken out separately
            # under their own attributes as well.
            self._span.set_attribute(
                "gen_ai.usage.input_tokens",
                usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
            )
            self._span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
            if usage.cache_read_tokens:
                self._span.set_attribute(
                    "gen_ai.usage.cache_read.input_tokens", usage.cache_read_tokens
                )
            if usage.cache_write_tokens:
                self._span.set_attribute(
                    "gen_ai.usage.cache_write.input_tokens", usage.cache_write_tokens
                )
            if completion.stop_reason:
                self._span.set_attribute("gen_ai.response.finish_reasons", [completion.stop_reason])
            if self._capture_content:
                input_json = json.dumps(_input_messages(turns), default=str)
                self._span.add_event("gen_ai.input.messages", {"gen_ai.input.messages": input_json})
                output_json = json.dumps(_output_messages(completion), default=str)
                self._span.add_event(
                    "gen_ai.output.messages", {"gen_ai.output.messages": output_json}
                )
                system_json = json.dumps(_system_instructions(system), default=str)
                self._span.add_event(
                    "gen_ai.system_instructions", {"gen_ai.system_instructions": system_json}
                )
        except Exception:
            logger.warning("telemetry: could not record a chat completion", exc_info=True)

    def _mark_error(self, error_type: str) -> None:
        _mark_span_error(self._span, error_type)


class ToolSpanHandle:
    """The handle `RunTrace.tool_span` yields, live for one `mcp.call`.

    `traceparent` and `tracestate` are the W3C trace context for this span,
    ready to pass straight to `HoneycombMCP.call(..., traceparent=..., tracestate=...)`.
    Both are None when telemetry is disabled or the context could not be built.
    """

    def __init__(
        self,
        span: Span | None,
        *,
        traceparent: str | None,
        tracestate: str | None,
        on_error: Callable[[], None] | None,
    ) -> None:
        self._span = span
        self._on_error = on_error
        self.traceparent = traceparent
        self.tracestate = tracestate

    def record_result(self, result_text: str, *, is_error: bool) -> None:
        if self._span is not None:
            try:
                self._span.set_attribute(
                    "gen_ai.tool.call.result", _truncate(result_text, RESULT_TRUNCATE_CHARS)
                )
            except Exception:
                logger.warning("telemetry: could not record a tool result", exc_info=True)
            if is_error:
                _mark_span_error(self._span, "tool_error")
        if is_error and self._on_error is not None:
            self._on_error()

    def record_exception(self, exc: BaseException) -> None:
        if self._span is not None:
            try:
                self._span.record_exception(exc)
            except Exception:
                logger.warning("telemetry: could not record a tool exception", exc_info=True)
            _mark_span_error(self._span, type(exc).__name__)
        if self._on_error is not None:
            self._on_error()

    def record_validation_rejection(self, rejection_text: str) -> None:
        """`submit_report` rejected by the validator: a distinct `error.type`
        from a normal tool failure, since this is the model's own report
        being handed back, not an MCP call going wrong, and it does not
        count toward `receipts.tool_errors`.
        """
        if self._span is None:
            return
        try:
            self._span.set_attribute(
                "gen_ai.tool.call.result", _truncate(rejection_text, RESULT_TRUNCATE_CHARS)
            )
            self._span.set_attribute("receipts.validation.rejected", True)
        except Exception:
            logger.warning("telemetry: could not record a validation rejection", exc_info=True)
        _mark_span_error(self._span, "validation_rejected")


async def traced_call(
    trace: RunTrace, mcp: Any, name: str, args: dict[str, Any], call_id: str
) -> Any:
    """One MCP call wrapped in `execute_tool {name}`.

    The same shape `agent/loop.py`'s own `call_tool` gives the
    investigation's calls: a `RunTrace.tool_span` around `mcp.call`, with
    the span's own `traceparent`/`tracestate` passed straight through to
    `mcp.call` the same way `agent/loop.py` line 504 onward does (CLAUDE.md:
    every call carries it), and the exception or the result recorded on the
    span. Shared here rather than duplicated in `agent/handoff.py` and
    `agent/board.py` (R23, EDW-1370; both wrap `mcp.call` for tools the
    model itself never calls, so neither goes through `agent/loop.py`'s own
    `call_tool`), which is also why this lives in this module instead of
    either of theirs: one place that knows how to turn an MCP call into a
    span, reused by both callers rather than two copies drifting apart.
    `call_id` is synthesized by the caller, not read off a model's tool
    call, since these calls are made directly by code, not replayed from a
    model turn.
    """
    with trace.tool_span(name, call_id, args) as span:
        try:
            result = await mcp.call(
                name, args, traceparent=span.traceparent, tracestate=span.tracestate
            )
        except Exception as exc:
            span.record_exception(exc)
            raise
        span.record_result(
            getattr(result, "text", None) or "", is_error=bool(getattr(result, "is_error", False))
        )
        return result


class RunTrace:
    """The root span for one run, plus what its children need to attach to it.

    See the module docstring for who opens and closes this and why. Every
    public method is safe to call regardless of how telemetry got disabled:
    `disabled_run_trace` leaves `_span` as `None`, while a disabled
    `Telemetry` hands out `NonRecordingSpan` objects instead (a real OTel
    no-op span, not `None`) that silently absorb every call on their own.
    `end*` is idempotent, safe to call more than once.
    """

    def __init__(
        self,
        tracer: trace.Tracer,
        span: Span | None,
        conversation_id: str,
        scenario_id: str,
        *,
        capture_content: bool,
    ) -> None:
        self._tracer = tracer
        self._span = span
        self._context = trace.set_span_in_context(span) if span is not None else None
        self._conversation_id = conversation_id
        self._scenario_id = scenario_id
        self._capture_content = capture_content
        self._tool_errors = 0
        self._ended = False

    @property
    def trace_id(self) -> str | None:
        """The trace id as Honeycomb's UI shows it, or None with no telemetry."""
        if self._span is None:
            return None
        context = self._span.get_span_context()
        if context is None or not context.is_valid:
            return None
        return format(context.trace_id, "032x")

    # -- children -----------------------------------------------------------

    def _start_child(self, name: str, *, kind: SpanKind, attributes: dict[str, Any]) -> Span | None:
        if self._span is None:
            return None
        try:
            return self._tracer.start_span(
                name, context=self._context, kind=kind, attributes=attributes
            )
        except Exception:
            logger.warning("telemetry: could not start span %r", name, exc_info=True)
            return None

    def _end_span(self, span: Span | None) -> None:
        if span is None:
            return
        try:
            span.end()
        except Exception:
            logger.warning("telemetry: could not end a span", exc_info=True)

    def _count_tool_error(self) -> None:
        self._tool_errors += 1

    @contextmanager
    def chat_span(
        self, requested_model: str, *, provider_name: str, max_tokens: int
    ) -> Iterator[_ChatSpan]:
        """`chat {model}` around one `provider.complete` call.

        `provider_name` is the same string `Telemetry.start_run` was given,
        passed through by the loop's `AgentConfig.provider`, so Bedrock and
        Ollama get `gen_ai.provider.name` for free once they set it to their
        own name. A cancellation from the loop's wall-clock timeout
        (`asyncio.CancelledError`, a `BaseException`, not an `Exception`) is
        caught here too and recorded as `error.type=timeout`, on this span
        only; it is re-raised either way.
        """
        span = self._start_child(
            f"chat {requested_model}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.agent.name": AGENT_NAME,
                "gen_ai.conversation.id": self._conversation_id,
                "gen_ai.provider.name": provider_name,
                "gen_ai.request.model": requested_model,
                "gen_ai.request.max_tokens": max_tokens,
            },
        )
        handle = _ChatSpan(span, capture_content=self._capture_content)
        try:
            yield handle
        except BaseException as exc:
            is_cancelled = isinstance(exc, asyncio.CancelledError)
            handle._mark_error("timeout" if is_cancelled else type(exc).__name__)
            raise
        finally:
            self._end_span(span)

    @contextmanager
    def tool_span(
        self, tool_name: str, tool_call_id: str, args: dict[str, Any]
    ) -> Iterator[ToolSpanHandle]:
        """`execute_tool {tool}` around one `mcp.call`.

        The caller is expected to catch its own exceptions inside the `with`
        block (as `_RunState.call_tool` does, to turn them into a
        `ToolResultBlock`) and call `record_exception` or `record_result`
        itself; this context manager only starts and ends the span.
        """
        try:
            arguments_json = _truncate(
                json.dumps(args, default=str, sort_keys=True), ARGUMENTS_TRUNCATE_CHARS
            )
        except Exception:
            logger.warning("telemetry: could not serialize tool call arguments", exc_info=True)
            arguments_json = _truncate(str(args), ARGUMENTS_TRUNCATE_CHARS)
        span = self._start_child(
            f"execute_tool {tool_name}",
            kind=SpanKind.INTERNAL,
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.agent.name": AGENT_NAME,
                "gen_ai.conversation.id": self._conversation_id,
                "gen_ai.tool.name": tool_name,
                "gen_ai.tool.call.id": tool_call_id,
                "gen_ai.tool.call.arguments": arguments_json,
            },
        )
        traceparent = tracestate = None
        if span is not None:
            try:
                carrier: dict[str, str] = {}
                propagate.inject(carrier, context=trace.set_span_in_context(span))
                traceparent = carrier.get("traceparent")
                tracestate = carrier.get("tracestate")
            except Exception:
                logger.warning("telemetry: could not build a traceparent", exc_info=True)
        try:
            yield ToolSpanHandle(
                span,
                traceparent=traceparent,
                tracestate=tracestate,
                on_error=self._count_tool_error,
            )
        finally:
            self._end_span(span)

    # -- the report's outcome, and ending the root span ----------------------

    def record_outcome(self, report: Report) -> None:
        """`receipts.stop_reason`, `.validation_failed`, `.tool_calls`,
        `.cost_usd`, and `.wall_s` from the finished report.

        Called once by the caller, before `end`, `end_with_grade`, or
        `end_with_error`; `agent/loop.py` never calls this, since the loop
        does not hold a finished `Report` at any point it is handed a trace.
        """
        if self._span is None:
            return
        try:
            self._span.set_attribute("receipts.stop_reason", report.stop_reason)
            self._span.set_attribute("receipts.validation_failed", report.validation_failed)
            self._span.set_attribute("receipts.tool_calls", report.tool_calls)
            self._span.set_attribute("receipts.cost_usd", report.cost_usd)
            self._span.set_attribute("receipts.wall_s", report.wall_s)
        except Exception:
            logger.warning("telemetry: could not record the report's outcome", exc_info=True)

    def end(self) -> None:
        """End the root span with no evaluation result: nothing graded it.

        `scenario.id` and `receipts.tool_errors` are set here, when the span
        ends, so the investigation this span belongs to is over before its
        own answer becomes queryable. `agent/mcp_client.py`'s dataset guard
        is what stops a later investigation reading an earlier one's
        `scenario.id` or grade off the same `receipts.run_id`, and a reader
        of the Agent Timeline still sees the value once the run is over.
        """
        if self._ended or self._span is None:
            self._ended = True
            return
        self._ended = True
        try:
            self._span.set_attribute("scenario.id", self._scenario_id)
            self._span.set_attribute("receipts.tool_errors", self._tool_errors)
        except Exception:
            logger.warning("telemetry: could not set the root span's closing attributes")
        try:
            self._span.end()
        except Exception:
            logger.warning("telemetry: could not end the root span", exc_info=True)

    def end_with_grade(self, total: float, components: Mapping[str, float]) -> None:
        """`gen_ai.evaluation.result` attributes, plus one evaluation event per score, then end.

        Called by `evals/run.py` once grading finishes. The span has stayed
        open since `start_run`, across the whole gap between `investigate()`
        returning and the grade being computed, so the evaluation lands on
        the same span the investigation ran on.
        """
        if not self._ended and self._span is not None:
            try:
                self._span.set_attribute("gen_ai.evaluation.result", total)
                for name, value in components.items():
                    self._span.set_attribute(f"receipts.grade.{name}", value)
            except Exception:
                logger.warning("telemetry: could not record the evaluation result", exc_info=True)
            self._record_evaluation_event("receipts.total", total, _TOTAL_EXPLANATION)
            for name, value in components.items():
                explanation = _COMPONENT_EXPLANATIONS.get(name, name)
                self._record_evaluation_event(f"receipts.{name}", value, explanation)
        self.end()

    def _record_evaluation_event(self, name: str, value: float, explanation: str) -> None:
        if self._span is None:
            return
        try:
            self._span.add_event(
                "gen_ai.evaluation.result",
                {
                    "gen_ai.evaluation.name": name,
                    "gen_ai.evaluation.score.value": value,
                    "gen_ai.evaluation.score.label": "pass" if value >= _PASS_LINE else "fail",
                    "gen_ai.evaluation.explanation": explanation,
                },
            )
        except Exception:
            logger.warning("telemetry: could not record an evaluation event", exc_info=True)

    def end_with_error(self, error_type: str, message: str) -> None:
        """A crash row: ERROR status and `error.type`, no evaluation result.

        This is the only path that marks the root span's own status ERROR:
        a caller reaches it either because `investigate()` raised, or
        because the report it returned has `stop_reason == "error"`. There
        is no `grade.json` for a crash, so nothing sets
        `gen_ai.evaluation.result` here.
        """
        if not self._ended and self._span is not None:
            try:
                self._span.set_status(Status(StatusCode.ERROR, message))
                self._span.set_attribute("error.type", error_type)
            except Exception:
                logger.warning("telemetry: could not mark the run's error", exc_info=True)
        self.end()

    def end_with_handoff(
        self,
        *,
        status: str,
        classification: str,
        reply: str | None,
        board_id: str | None,
        board_url: str | None,
        error: str | None = None,
        board_error: str | None = None,
    ) -> None:
        """End a handoff root span (see `Telemetry.start_handoff`) with what
        Canvas and the board said back.

        Takes plain fields rather than an `agent.handoff.Handoff`, so this
        module does not need to import that one: `evals/run.py`'s
        `_hand_off_cell` is the only caller, and it already has both a
        `Handoff` and an `agent.board.BoardResult` in hand by the time it
        calls this.

        `status`, `classification`, and `reply` are `receipts.*` rather than
        `gen_ai.*`: the semconv's `gen_ai.evaluation.result` is for an
        automated grader's numeric score of this agent's own output (that
        already lives on the investigation's own root span, from
        `end_with_grade`), not a second agent's free-text opinion, and a
        `agree`/`disagree`/`extend`/`no_response` label read by a keyword
        classifier has no numeric score to report honestly. `reply` is
        truncated to `REPLY_TRUNCATE_CHARS` (4096, longer than
        `RESULT_TRUNCATE_CHARS`'s 500: Canvas's reply is prose someone reads,
        not a query result skimmed for a query id); `handoff.json`
        (`evals/run.py`'s `write_handoff`) holds the whole thing regardless
        of what fits on the span.
        """
        if self._ended or self._span is None:
            self._ended = True
            return
        self._ended = True
        try:
            self._span.set_attribute("receipts.handoff.status", status)
            self._span.set_attribute("receipts.handoff.classification", classification)
            if reply:
                self._span.set_attribute(
                    "receipts.handoff.reply", _truncate(reply, REPLY_TRUNCATE_CHARS)
                )
            if board_id:
                self._span.set_attribute("receipts.board.id", board_id)
            if board_url:
                self._span.set_attribute("receipts.board.url", board_url)
            if error:
                self._span.set_attribute(
                    "receipts.handoff.error", _truncate(error, RESULT_TRUNCATE_CHARS)
                )
            if board_error:
                self._span.set_attribute(
                    "receipts.board.error", _truncate(board_error, RESULT_TRUNCATE_CHARS)
                )
            if status != "completed":
                self._span.set_status(Status(StatusCode.ERROR))
                self._span.set_attribute("error.type", f"handoff_{status}")
        except Exception:
            logger.warning("telemetry: could not record the handoff outcome", exc_info=True)
        self._end_span(self._span)


def disabled_run_trace(conversation_id: str = "", scenario_id: str = "") -> RunTrace:
    """A `RunTrace` that does nothing, for a caller that never wired telemetry.

    Used by `agent/loop.py` when `investigate()` is called without a `trace`,
    which is every existing test and any script that only wants a report.
    Built from a genuine no-op OTel tracer, so it degrades exactly the way a
    disabled `Telemetry` does; `conversation_id` and `scenario_id` are never
    attached to anything here, since there is no span to attach them to.
    """
    tracer, _ = build_tracer()
    return RunTrace(tracer, None, conversation_id, scenario_id, capture_content=False)


class Telemetry:
    """One tracer for a process, and the root span it opens per run.

    `Telemetry()` with no arguments is disabled: no `Settings`, no exporter,
    no network. `Telemetry(settings)` is real when `settings` carries an
    ingest key; `Telemetry(exporter=...)` is real regardless of settings,
    which is how tests see their own spans without touching the network.
    Built once per process and shared across every run it opens, the way
    `agent/mcp_client.TokenBucket` is shared across the eval matrix: one
    export queue rather than one per run.
    """

    def __init__(
        self, settings: Settings | None = None, *, exporter: SpanExporter | None = None
    ) -> None:
        self.capture_content = bool(settings and settings.receipts_capture_content)
        self._tracer, self._provider = build_tracer(settings, exporter=exporter)

    @property
    def enabled(self) -> bool:
        return self._provider is not None

    def start_run(
        self,
        run_id: str,
        scenario_id: str,
        *,
        conversation_id: str,
        config_label: str,
        provider: str,
    ) -> RunTrace:
        """Open `invoke_agent receipts-investigator` for one investigation.

        `conversation_id` is what Honeycomb's Agent Timeline groups by, and
        the caller builds it to be unique per investigation, not per emitted
        run (see the module docstring). `run_id` lands on the span now, as
        `receipts.run_id`: it is bookkeeping the model is already told in
        the prompt, not an answer. `scenario_id` is different: it is held by
        the returned `RunTrace` and lands on the span only when it ends
        (`RunTrace.end`), because this span's own investigation runs while
        it is open and could otherwise read its own answer back.
        `agent/loop.py` never touches this trace's attributes, only its
        `chat_span` and `tool_span` methods, so there is no path from the
        model's context to either value regardless.
        """
        try:
            span = self._tracer.start_span(
                f"invoke_agent {AGENT_NAME}",
                kind=SpanKind.INTERNAL,
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": AGENT_NAME,
                    "gen_ai.conversation.id": conversation_id,
                    "gen_ai.provider.name": provider,
                    "receipts.run_id": run_id,
                    "receipts.config": config_label,
                },
            )
        except Exception:
            logger.warning("telemetry: could not start the root span", exc_info=True)
            span = None
        return RunTrace(
            self._tracer,
            span,
            conversation_id,
            scenario_id,
            capture_content=self.capture_content,
        )

    def start_handoff(
        self, run_id: str, *, conversation_id: str, config_label: str, provider: str
    ) -> RunTrace:
        """Open `invoke_agent canvas` for one Canvas handoff (R23, EDW-1370).

        A second root span, not a child of the investigation's own
        `invoke_agent receipts-investigator` span: by the time a handoff
        runs, `evals/run.py`'s `run_one` has already called `end_with_grade`
        (or `end_with_error`) on that span, closing it, so there is no open
        parent left to attach to. The alternative (option 1 in the issue)
        would instead move the handoff before that call, at the cost of
        changing when the grade is written and stretching the investigation's
        own root span by up to the handoff's budget (300s). `conversation_id`
        is what keeps this span in the same Agent Timeline conversation as
        the investigation that produced the report: `evals/run.py` passes the
        exact id `start_run` was given for that run
        (`f"{run_id}.{config_name}.{repeat}"`), not a new one. Checked live on
        2026-09-07 rather than assumed (the Timeline has surprised this
        project before: a `/` in a conversation id broke its Traces panel):
        conversation `run-livecheck23.full.1`, two root spans sharing one
        conversation id, rendered as one conversation with two agent lanes
        (`receipts-investigator` and `canvas`), confirming option 2.

        `run_id`, `config_label`, and `provider` land on the span as
        `receipts.run_id`, `receipts.config`, and `gen_ai.provider.name`, the
        same values the investigation's own root carries, so a query over
        `invoke_agent canvas` slices by config or provider without parsing
        the conversation id. There is no `scenario_id` parameter here, unlike
        `start_run`: the investigation's root has already closed with
        `scenario.id` on it by the time this trace opens, so a handoff span
        cannot expose anything the conversation does not already show, and
        `agent/mcp_client.py`'s dataset guard, not withholding the attribute
        here, is what stops a later cell from reading it back.
        """
        try:
            span = self._tracer.start_span(
                "invoke_agent canvas",
                kind=SpanKind.INTERNAL,
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    # The agent this span is about is Canvas, the one being
                    # invoked, not this agent's own name: `tool_span`'s
                    # children below still tag `gen_ai.agent.name` as
                    # AGENT_NAME regardless, since this agent is the one
                    # making those calls even though Canvas is who the root
                    # span itself describes.
                    "gen_ai.agent.name": "canvas",
                    "gen_ai.conversation.id": conversation_id,
                    "gen_ai.provider.name": provider,
                    "receipts.run_id": run_id,
                    "receipts.config": config_label,
                },
            )
        except Exception:
            logger.warning("telemetry: could not start the handoff root span", exc_info=True)
            span = None
        return RunTrace(
            self._tracer,
            span,
            conversation_id,
            "",
            capture_content=self.capture_content,
        )

    def flush(self, timeout_millis: int = 5000) -> None:
        """Force-flush pending spans.

        Called after the root span ends, outside the measured wall clock:
        exporting is I/O the investigation did not spend, and `Report.wall_s`
        must not carry it (the caller's timing already stopped by the time
        this runs). No-op when disabled; never raises.
        """
        if self._provider is None:
            return
        try:
            self._provider.force_flush(timeout_millis)
        except Exception:
            logger.warning("telemetry: flush failed", exc_info=True)

    def shutdown(self) -> None:
        if self._provider is None:
            return
        try:
            self._provider.shutdown()
        except Exception:
            logger.warning("telemetry: shutdown failed", exc_info=True)
