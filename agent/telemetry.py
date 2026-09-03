"""Self-telemetry for the investigator: OTel GenAI semconv spans, exported to Honeycomb.

Attribute names come from https://docs.honeycomb.io/send-data/use-cases/agents
and the OTel GenAI semantic conventions it links
(https://github.com/open-telemetry/semantic-conventions-genai, `docs/gen-ai/`).
One root span per run, `invoke_agent receipts-investigator`, with `chat {model}`
and `execute_tool {tool}` children.

Two Honeycomb environments meet in this project: `receipts-shop` is the
synthetic traffic the agent investigates, and this module's spans are the
agent's own work investigating it. The resource here carries
`service.name = receipts-investigator`, a different dataset from
`receipts-shop`, on purpose. If the agent's own tool calls and chat turns
landed in the dataset it queries, a BubbleUp over "all requests in the window"
would count the agent's own MCP traffic as if it were shop traffic, and an
investigation would very occasionally find itself as the incident.

Ownership of the root span. `agent/loop.py` never opens or closes it: the
`RunTrace` is opened by whichever caller knows the scenario id and the
config, before `investigate()` runs, and is passed in as `trace=`.
`agent/__main__.py` calls `investigate()` and then `RunTrace.end()`, because a
single CLI run is never graded. `evals/run.py` calls `investigate()`, grades
the report, and only then calls `end_with_grade()` or `end_with_error()`, so
the root span stays open across the gap between the investigation finishing
and the grade landing. `agent/loop.py` only opens the `chat_span` and
`tool_span` children on the `RunTrace` it is handed; `scenario.id` is set once,
by `start_run`, and the loop never touches it, which is also why the model
never sees it: nothing in `agent/loop.py` reads it back out of the trace to
put in a message.

`gen_ai.conversation.id` is not the emitted run id. Since R8, one emit
serves every config and repeat, so the run id names the traffic under
investigation, not the investigation itself: two investigations of the same
run id (a CLI run and an eval repeat, or two eval repeats) are two separate
conversations, and Honeycomb's Agent Timeline groups by this id, so giving
it the run id merges them into one conversation with duplicated tool calls
and chat turns. `Telemetry.start_run` takes a `conversation_id` the caller
builds to be unique per investigation (`evals/run.py` uses
`f"{run_id}/{config}/{repeat}"`, `agent/__main__.py` uses
`f"{run_id}/cli/{timestamp}"`) and sets it as `gen_ai.conversation.id` on
every span. The run id itself still lands on the root span, as
`receipts.run_id`, so a reader can join the investigation back to the
receipts-shop traffic it queried.

Disabled by construction, not by luck. `Telemetry()` with no settings and no
exporter is inert: a genuine OTel no-op tracer, not a stub of this module's
own. Every span operation it hands out is a true no-op at the SDK boundary,
so a caller that forgets to wire telemetry gets silence, not a network call
with a fake key. Tests that want to see spans pass `exporter=` explicitly
(an in-memory exporter); tests that pass a `Settings` with a real-looking
ingest key but no `exporter=` still get nothing, because a `Telemetry` is
only ever built where the caller means it (`agent/__main__.py`,
`evals/run.py`'s CLI), never as a silent default from a `settings` fixture.

Never raises. Every method that touches an OTel span catches its own
exceptions and logs a warning instead: a broken exporter, an exporter that
raises on flush, or a value that will not serialize must not turn a paid
investigation into a crash row. OTel's own `Span.set_attribute` already
degrades a bad value to a dropped attribute rather than raising; the extra
care here is for the code in this module that builds JSON, truncates it, and
walks the trace context, none of which OTel guards for us.

Content capture. `gen_ai.input.messages` and `gen_ai.output.messages` carry
the full prompt and completion and are opt-in
(`RECEIPTS_CAPTURE_CONTENT=1`), same as the semconv's own guidance for these
two attributes. `gen_ai.tool.call.arguments` and `gen_ai.tool.call.result`
are not gated the same way: the semconv marks them opt-in too, but here they
are Honeycomb query specs and truncated result text, not user data, and
seeing them is most of the point of the Agent Timeline, so they are always
on, truncated rather than hidden.

Recording content on span events. The semconv wants structured (nested)
content recorded in structured form and allows a JSON string only as a
fallback for signals that cannot carry structure. OTel span event attributes
cannot carry nested objects at all (only strings, numbers, bools, and flat
sequences of those), so the fallback is what this module uses: one string
attribute on the event, holding the JSON.

The evaluation result is written twice, on purpose. `end_with_grade` sets
`gen_ai.evaluation.result` and `receipts.grade.<component>` as plain
attributes on the root span, which is what a `run_query` breaks down on; it
also adds one `gen_ai.evaluation.result` span event per score (the total and
each component), with `gen_ai.evaluation.name`, `.score.value`,
`.score.label`, and `.explanation`, because Honeycomb's guide reads events,
not attributes, for the GenAI tab: "Attach gen_ai.evaluation.result events
to the GenAI operation span to review evaluations in the GenAI tab." Both
come from the same numbers, so they cannot disagree.

The Honeycomb MCP question, answered. A live check on 2026-09-03 found no
spans from Honeycomb's hosted MCP in the `receipts-demo` environment for
either run's trace: the hosted MCP does not link its own spans to ours
today. `agent/mcp_client.py` keeps sending `traceparent` in `_meta` on every
call regardless, because the OTel MCP semantic conventions say a client
should, and a server that starts reading it later should not need a code
change here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping, Sequence
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

logger = logging.getLogger(__name__)

SERVICE_NAME = "receipts-investigator"
AGENT_NAME = "receipts-investigator"
_TRACER_NAME = "receipts.agent"

# The grader's own line between a right and a wrong top hypothesis, reused
# here so the pass/fail label on an evaluation event agrees with the grader
# by construction rather than by a second copy of the number.
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


def _mark_error(span: Span | None, root_span: Span | None, error_type: str) -> None:
    """ERROR status and `error.type` on `span`, propagated to `root_span` too.

    The Honeycomb agent guide asks that a tool failure's error status
    propagate to the parent span. `chat_span` and `tool_span` are both
    direct children of the root `invoke_agent` span rather than nested in
    each other, so "the parent" here is the root span itself; marking it
    from here, on every child failure, is what makes an error visible on the
    run as a whole and not only on the one span that hit it.
    """
    try:
        if span is not None:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", error_type)
        if root_span is not None:
            root_span.set_status(Status(StatusCode.ERROR))
            root_span.set_attribute("error.type", error_type)
    except Exception:
        logger.warning("telemetry: could not mark an error on a span", exc_info=True)


def _input_messages(system: str, turns: Sequence[Turn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for turn in turns:
        entry: dict[str, Any] = {"role": turn.role}
        if turn.text:
            entry["content"] = turn.text
        if turn.tool_uses:
            entry["tool_uses"] = [
                {"id": use.id, "name": use.name, "args": use.args} for use in turn.tool_uses
            ]
        if turn.tool_results:
            entry["tool_results"] = [
                {"tool_use_id": r.tool_use_id, "content": r.content, "is_error": r.is_error}
                for r in turn.tool_results
            ]
        messages.append(entry)
    return messages


def _output_messages(completion: Completion) -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": completion.text,
            "tool_uses": [
                {"id": use.id, "name": use.name, "args": use.args} for use in completion.tool_uses
            ],
        }
    ]


class _ChatSpan:
    """The handle `RunTrace.chat_span` yields, live for one `provider.complete` call."""

    def __init__(self, span: Span | None, root_span: Span | None, *, capture_content: bool) -> None:
        self._span = span
        self._root_span = root_span
        self._capture_content = capture_content

    def record(self, completion: Completion, *, system: str, turns: Sequence[Turn]) -> None:
        """Everything the span needs once the completion is in hand."""
        if self._span is None:
            return
        try:
            if completion.response_model:
                self._span.set_attribute("gen_ai.response.model", completion.response_model)
            self._span.set_attribute("gen_ai.usage.input_tokens", completion.usage.input_tokens)
            self._span.set_attribute("gen_ai.usage.output_tokens", completion.usage.output_tokens)
            if completion.stop_reason:
                self._span.set_attribute("gen_ai.response.finish_reasons", [completion.stop_reason])
            if self._capture_content:
                input_json = json.dumps(_input_messages(system, turns), default=str)
                self._span.add_event("gen_ai.input.messages", {"gen_ai.input.messages": input_json})
                output_json = json.dumps(_output_messages(completion), default=str)
                self._span.add_event(
                    "gen_ai.output.messages", {"gen_ai.output.messages": output_json}
                )
        except Exception:
            logger.warning("telemetry: could not record a chat completion", exc_info=True)

    def _mark_error(self, error_type: str) -> None:
        _mark_error(self._span, self._root_span, error_type)


class ToolSpanHandle:
    """The handle `RunTrace.tool_span` yields, live for one `mcp.call`.

    `traceparent` and `tracestate` are the W3C trace context for this span,
    ready to pass straight to `HoneycombMCP.call(..., traceparent=..., tracestate=...)`.
    Both are None when telemetry is disabled or the context could not be built.
    """

    def __init__(
        self,
        span: Span | None,
        root_span: Span | None,
        *,
        traceparent: str | None,
        tracestate: str | None,
    ) -> None:
        self._span = span
        self._root_span = root_span
        self.traceparent = traceparent
        self.tracestate = tracestate

    def record_result(self, result_text: str, *, is_error: bool) -> None:
        if self._span is None:
            return
        try:
            self._span.set_attribute(
                "gen_ai.tool.call.result", _truncate(result_text, RESULT_TRUNCATE_CHARS)
            )
        except Exception:
            logger.warning("telemetry: could not record a tool result", exc_info=True)
        if is_error:
            _mark_error(self._span, self._root_span, "tool_error")

    def record_exception(self, exc: BaseException) -> None:
        if self._span is None:
            return
        try:
            self._span.record_exception(exc)
        except Exception:
            logger.warning("telemetry: could not record a tool exception", exc_info=True)
        _mark_error(self._span, self._root_span, type(exc).__name__)


class RunTrace:
    """The root span for one run, plus what its children need to attach to it.

    See the module docstring for who opens and closes this and why. Every
    public method is safe to call when telemetry is disabled (spans are
    None) and safe to call more than once (`end*` is idempotent).
    """

    def __init__(
        self,
        tracer: trace.Tracer,
        span: Span | None,
        conversation_id: str,
        *,
        capture_content: bool,
    ) -> None:
        self._tracer = tracer
        self._span = span
        self._context = trace.set_span_in_context(span) if span is not None else None
        self._conversation_id = conversation_id
        self._capture_content = capture_content
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

    @contextmanager
    def chat_span(
        self, requested_model: str, *, provider_name: str, max_tokens: int
    ) -> Iterator[_ChatSpan]:
        """`chat {model}` around one `provider.complete` call.

        `provider_name` is the same string `Telemetry.start_run` was given
        for `agent.provider`, passed through by the loop's `AgentConfig`, so
        Bedrock and Ollama get `gen_ai.provider.name` for free once they set
        `config.provider` to their own name.
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
        handle = _ChatSpan(span, self._span, capture_content=self._capture_content)
        try:
            yield handle
        except Exception as exc:
            handle._mark_error(type(exc).__name__)
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
            yield ToolSpanHandle(span, self._span, traceparent=traceparent, tracestate=tracestate)
        finally:
            self._end_span(span)

    # -- ending the root span -------------------------------------------------

    def end(self) -> None:
        """End the root span with no evaluation result: nothing graded it."""
        if self._ended or self._span is None:
            self._ended = True
            return
        self._ended = True
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

        The attributes (`gen_ai.evaluation.result` and one
        `receipts.grade.<component>` per component) are what a `run_query`
        breaks down on. They are not what Honeycomb's GenAI tab reads, so
        this also adds one `gen_ai.evaluation.result` span event per score,
        the total and each component, with the semconv's event attributes.
        Both are written from the same numbers.
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

        There is no `grade.json` for a crash, so nothing sets
        `gen_ai.evaluation.result` here; a reader of the Agent Timeline sees
        the error status instead of a grade.
        """
        if not self._ended and self._span is not None:
            try:
                self._span.set_status(Status(StatusCode.ERROR, message))
                self._span.set_attribute("error.type", error_type)
            except Exception:
                logger.warning("telemetry: could not mark the run's error", exc_info=True)
        self.end()


def disabled_run_trace(conversation_id: str = "") -> RunTrace:
    """A `RunTrace` that does nothing, for a caller that never wired telemetry.

    Used by `agent/loop.py` when `investigate()` is called without a `trace`,
    which is every existing test and any script that only wants a report.
    Genuinely inert: built from a no-op OTel tracer, not a hand-rolled stub,
    so it degrades exactly the way a disabled `Telemetry` does. `conversation_id`
    is never actually attached to anything here (there is no span to attach
    it to); it exists only so the signature matches a real `RunTrace`.
    """
    tracer, _ = build_tracer()
    return RunTrace(tracer, None, conversation_id, capture_content=False)


class Telemetry:
    """One tracer for a process, and the root span it opens per run.

    `Telemetry()` with no arguments is disabled: no `Settings`, no exporter,
    no network, ever. `Telemetry(settings)` is real when `settings` carries
    an ingest key; `Telemetry(exporter=...)` is real regardless of settings,
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
        run (see the module docstring). `run_id` still lands here, as
        `receipts.run_id`, so a reader can join this investigation back to
        the receipts-shop traffic it queried. `scenario.id` is set here,
        once, by the caller, and nowhere else: `agent/loop.py` never touches
        this trace's attributes, only its `chat_span` and `tool_span`
        methods, so there is no path from the model's context to either
        value.
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
                    "scenario.id": scenario_id,
                    "agent.config": config_label,
                    "agent.provider": provider,
                },
            )
        except Exception:
            logger.warning("telemetry: could not start the root span", exc_info=True)
            span = None
        return RunTrace(self._tracer, span, conversation_id, capture_content=self.capture_content)

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
