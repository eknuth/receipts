"""Tests for agent/telemetry.py: the OTel GenAI spans the investigator emits about itself.

No network, ever. Real spans come from `Telemetry(exporter=...)`, an
in-memory exporter wired through a `SimpleSpanProcessor` so a span is visible
the moment it ends, no flush timing to race. The loop itself is driven the
same way `test_agent_loop.py` drives it: a `FakeProvider` replaying scripted
completions and a `FakeMCP` answering tool calls, so the whole thing runs
without a model or the network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from test_agent_loop import (
    RUN,
    FakeMCP,
    FakeProvider,
    baseline_use,
    completion,
    negation_use,
    report_args,
    use,
)
from test_agent_loop import query_use as _query_use

from agent import telemetry as T
from agent.loop import SUBMIT_REPORT, AgentConfig, investigate
from agent.mcp_client import ToolNotAllowed, ToolResult
from agent.providers.base import Completion, Usage
from agent.report import Report
from agent.telemetry import Telemetry
from receipts.settings import Settings

FAKE_INGEST_KEY = "fake-ingest-key-do-not-leak"
FAKE_MCP_KEY = "fake-key-id:fake-secret-do-not-leak"

# root + 3 chat spans (one per model turn) + 4 MCP tool calls
# (get_workspace_context, then three run_query calls) + 1 submit_report span.
EXPECTED_SPAN_COUNT = 1 + 3 + 4 + 1


def make_settings(*, capture_content: bool = False, ingest_key: str | None = "k") -> Settings:
    return Settings(
        _env_file=None,
        honeycomb_ingest_key=ingest_key,
        honeycomb_mcp_key=FAKE_MCP_KEY,
        anthropic_api_key="fake-anthropic-key",
        anthropic_workspace_id="fake-workspace-id",
        receipts_capture_content=capture_content,
    )


def good_script() -> list[Any]:
    return [
        completion(use("get_workspace_context", ident="a")),
        completion(_query_use("b"), negation_use("c"), baseline_use("d")),
        completion(use(SUBMIT_REPORT, report_args(), ident="d")),
    ]


def make_conversation_id(suffix: str = "1") -> str:
    """A conversation id shaped like the ones evals/run.py and agent/__main__.py
    build: the run id plus something that makes this investigation unique,
    joined with dots (a live check found a slash broke the Agent Timeline's
    Traces panel)."""
    return f"{RUN.run_id}.full.{suffix}"


def start_test_run(
    telemetry: Telemetry, *, conversation_id: str | None = None, provider: str = "fake"
) -> T.RunTrace:
    return telemetry.start_run(
        RUN.run_id,
        RUN.scenario_id,
        conversation_id=conversation_id or make_conversation_id(),
        config_label="full",
        provider=provider,
    )


async def run_full_investigation(
    telemetry: Telemetry,
    *,
    mcp: FakeMCP | None = None,
    settings: Settings | None = None,
    conversation_id: str | None = None,
):
    """One complete investigation under a real (in-memory) trace. Returns (report, run_trace)."""
    run_trace = start_test_run(telemetry, conversation_id=conversation_id)
    provider = FakeProvider(good_script())
    report = await investigate(
        RUN,
        AgentConfig(),
        settings=settings or make_settings(),
        mcp=mcp or FakeMCP(),
        provider=provider,
        trace=run_trace,
    )
    return report, run_trace


class RaisingExporter(SpanExporter):
    """An exporter that always blows up, to prove telemetry can't fail a run."""

    def export(self, spans: Any) -> SpanExportResult:
        raise RuntimeError("the collector is down")

    def shutdown(self) -> None:
        raise RuntimeError("shutdown is down too")

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        raise RuntimeError("flush is down too")


class RecordingMCP(FakeMCP):
    """A FakeMCP that also remembers the kwargs each call was made with."""

    def __init__(self) -> None:
        super().__init__()
        self.kwargs_seen: list[dict[str, Any]] = []

    async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.kwargs_seen.append(kwargs)
        return await super().call(name, args)


class NotAllowedMCP(FakeMCP):
    async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls.append((name, dict(args or {})))
        raise ToolNotAllowed(f"{name!r} is not allowed")


class ServerErrorMCP(FakeMCP):
    async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls.append((name, dict(args or {})))
        return ToolResult(
            raw="err", text="bad filter column", is_error=True, query_id=None, permalink=None
        )


def tool_spans(spans: list[Any]) -> list[Any]:
    """The MCP execute_tool spans, excluding submit_report's."""
    return [
        s
        for s in spans
        if s.name.startswith("execute_tool ") and s.name != "execute_tool submit_report"
    ]


# --------------------------------------------------------------------------
# The three required attributes, everywhere
# --------------------------------------------------------------------------


async def test_every_span_carries_the_three_required_attributes() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    conversation_id = make_conversation_id("1")
    report, run_trace = await run_full_investigation(telemetry, conversation_id=conversation_id)
    run_trace.end()
    telemetry.flush()

    spans = exporter.get_finished_spans()
    assert report.stop_reason == "report"
    assert len(spans) == EXPECTED_SPAN_COUNT
    for span in spans:
        assert span.attributes["gen_ai.conversation.id"] == conversation_id
        assert span.attributes["gen_ai.agent.name"] == "receipts-investigator"
        assert span.attributes["gen_ai.operation.name"] in {"invoke_agent", "chat", "execute_tool"}

    root = next(s for s in spans if s.name == "invoke_agent receipts-investigator")
    assert root.attributes["receipts.run_id"] == RUN.run_id
    assert root.attributes["gen_ai.provider.name"] == "fake"

    chat_spans = [s for s in spans if s.name.startswith("chat ")]
    for span in chat_spans:
        # AgentConfig() defaults provider to "anthropic"; run_full_investigation
        # swaps in a FakeProvider instance but leaves the config's provider
        # label alone, which is what the loop passes to chat_span.
        assert span.attributes["gen_ai.provider.name"] == "anthropic"
        assert span.attributes["gen_ai.request.max_tokens"] > 0


def test_the_root_span_names_and_operation() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    conversation_id = make_conversation_id("1")
    run_trace = start_test_run(telemetry, conversation_id=conversation_id, provider="anthropic")
    run_trace.end()
    telemetry.flush()

    (span,) = exporter.get_finished_spans()
    assert span.name == "invoke_agent receipts-investigator"
    assert span.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert span.attributes["gen_ai.conversation.id"] == conversation_id
    assert span.attributes["receipts.run_id"] == RUN.run_id
    assert span.attributes["scenario.id"] == RUN.scenario_id
    assert span.attributes["receipts.config"] == "full"
    assert span.attributes["gen_ai.provider.name"] == "anthropic"
    assert "agent.config" not in span.attributes
    assert "agent.provider" not in span.attributes


# --------------------------------------------------------------------------
# scenario.id is never exported while its own investigation could read it
# --------------------------------------------------------------------------


async def test_scenario_id_is_absent_until_the_root_span_ends() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    report, run_trace = await run_full_investigation(telemetry)
    telemetry.flush()

    # Every span exported while the investigation ran (everything but the
    # still-open root) must not carry scenario.id.
    for span in exporter.get_finished_spans():
        assert "scenario.id" not in span.attributes

    run_trace.end()
    telemetry.flush()

    root = next(
        s for s in exporter.get_finished_spans() if s.name == "invoke_agent receipts-investigator"
    )
    assert root.attributes["scenario.id"] == RUN.scenario_id
    assert report.run_id == RUN.run_id  # unaffected either way


# --------------------------------------------------------------------------
# The root span outlives investigate() and takes the grade
# --------------------------------------------------------------------------


async def test_the_root_span_outlives_investigate_and_takes_the_evaluation_result() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    report, run_trace = await run_full_investigation(telemetry)
    telemetry.flush()

    names = [span.name for span in exporter.get_finished_spans()]
    assert "invoke_agent receipts-investigator" not in names
    assert any(name.startswith("chat ") for name in names)

    run_trace.end_with_grade(0.735, {"dims": 1.0, "span": 0.0, "receipts": 1.0})
    telemetry.flush()

    root = next(
        span
        for span in exporter.get_finished_spans()
        if span.name == "invoke_agent receipts-investigator"
    )
    assert root.attributes["gen_ai.evaluation.result"] == 0.735
    assert root.attributes["receipts.grade.dims"] == 1.0
    assert root.attributes["receipts.grade.span"] == 0.0
    assert root.attributes["receipts.grade.receipts"] == 1.0
    assert report.run_id == RUN.run_id  # the report itself is untouched by any of this

    # Honeycomb's GenAI tab reads events, not attributes, for evaluations:
    # one gen_ai.evaluation.result event per score, total plus components.
    events = [e for e in root.events if e.name == "gen_ai.evaluation.result"]
    assert len(events) == 1 + 3  # total, dims, span, receipts
    by_name = {e.attributes["gen_ai.evaluation.name"]: e for e in events}
    assert set(by_name) == {"receipts.total", "receipts.dims", "receipts.span", "receipts.receipts"}

    total_event = by_name["receipts.total"]
    assert total_event.attributes["gen_ai.evaluation.score.value"] == 0.735
    assert total_event.attributes["gen_ai.evaluation.score.label"] == "pass"
    assert total_event.attributes["gen_ai.evaluation.explanation"]

    span_event = by_name["receipts.span"]  # 0.0, below the 0.5 pass line
    assert span_event.attributes["gen_ai.evaluation.score.value"] == 0.0
    assert span_event.attributes["gen_ai.evaluation.score.label"] == "fail"
    assert span_event.attributes["gen_ai.evaluation.explanation"]

    dims_event = by_name["receipts.dims"]  # 1.0, at or above the pass line
    assert dims_event.attributes["gen_ai.evaluation.score.value"] == 1.0
    assert dims_event.attributes["gen_ai.evaluation.score.label"] == "pass"


async def test_a_crash_ends_the_root_span_with_an_error_and_no_evaluation_result() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = start_test_run(telemetry, conversation_id=make_conversation_id("1"))
    run_trace.end_with_error("RuntimeError", "the provider fell over")
    telemetry.flush()

    (root,) = exporter.get_finished_spans()
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["error.type"] == "RuntimeError"
    assert "gen_ai.evaluation.result" not in root.attributes


# --------------------------------------------------------------------------
# record_outcome
# --------------------------------------------------------------------------


def test_record_outcome_sets_receipts_fields_from_the_report() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = start_test_run(telemetry, conversation_id=make_conversation_id("1"))
    report = Report(
        run_id=RUN.run_id,
        scenario_id=RUN.scenario_id,
        provider="fake",
        model="x",
        stop_reason="report",
        validation_failed=False,
        tool_calls=7,
        cost_usd=0.05,
        wall_s=12.3,
    )
    run_trace.record_outcome(report)
    run_trace.end()
    telemetry.flush()

    (root,) = exporter.get_finished_spans()
    assert root.attributes["receipts.stop_reason"] == "report"
    assert root.attributes["receipts.validation_failed"] is False
    assert root.attributes["receipts.tool_calls"] == 7
    assert root.attributes["receipts.cost_usd"] == 0.05
    assert root.attributes["receipts.wall_s"] == 12.3


# --------------------------------------------------------------------------
# Content capture
# --------------------------------------------------------------------------


async def test_content_capture_is_off_by_default() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)  # no settings: capture_content is False
    _, run_trace = await run_full_investigation(telemetry)
    run_trace.end()
    telemetry.flush()

    chat_spans = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    assert chat_spans
    for span in chat_spans:
        event_names = [event.name for event in span.events]
        assert "gen_ai.input.messages" not in event_names
        assert "gen_ai.output.messages" not in event_names
        assert "gen_ai.system_instructions" not in event_names


async def test_content_capture_turns_on_with_the_flag_in_the_semconv_message_shape() -> None:
    exporter = InMemorySpanExporter()
    settings = make_settings(capture_content=True)
    telemetry = Telemetry(settings, exporter=exporter)
    assert telemetry.capture_content is True
    _, run_trace = await run_full_investigation(telemetry, settings=settings)
    run_trace.end()
    telemetry.flush()

    chat_spans = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    assert chat_spans
    saw_input = saw_output = saw_system = False
    for span in chat_spans:
        events = {event.name: event for event in span.events}
        if "gen_ai.input.messages" in events:
            saw_input = True
            raw = events["gen_ai.input.messages"].attributes["gen_ai.input.messages"]
            payload = json.loads(raw)
            assert isinstance(payload, list)
            for message in payload:
                assert set(message) == {"role", "parts"}
                assert message["role"] != "system"  # the system prompt is its own event
                for part in message["parts"]:
                    assert part["type"] in {"text", "tool_call", "tool_call_response"}
        if "gen_ai.output.messages" in events:
            saw_output = True
            raw = events["gen_ai.output.messages"].attributes["gen_ai.output.messages"]
            payload = json.loads(raw)
            assert len(payload) == 1
            assert payload[0]["role"] == "assistant"
            assert "finish_reason" in payload[0]
            for part in payload[0]["parts"]:
                assert part["type"] in {"text", "tool_call"}
        if "gen_ai.system_instructions" in events:
            saw_system = True
            raw = events["gen_ai.system_instructions"].attributes["gen_ai.system_instructions"]
            payload = json.loads(raw)
            assert len(payload) == 1
            assert payload[0]["type"] == "text"
            assert payload[0]["content"]
    assert saw_input and saw_output and saw_system


# --------------------------------------------------------------------------
# Disabled telemetry
# --------------------------------------------------------------------------


async def test_no_ingest_key_means_no_spans_and_no_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = make_settings(ingest_key=None)
    telemetry = Telemetry(settings)  # no exporter, no ingest key
    assert telemetry.enabled is False

    with caplog.at_level("WARNING"):
        report, run_trace = await run_full_investigation(telemetry, settings=settings)
        run_trace.end()
        telemetry.flush()
        telemetry.shutdown()

    assert report.stop_reason == "report"  # the investigation itself is unaffected
    assert run_trace.trace_id is None
    assert not [r for r in caplog.records if "telemetry" in r.getMessage().lower()]


# --------------------------------------------------------------------------
# A broken exporter cannot fail an investigation
# --------------------------------------------------------------------------


async def test_an_exporter_that_raises_does_not_fail_the_run() -> None:
    telemetry = Telemetry(exporter=RaisingExporter())
    report, run_trace = await run_full_investigation(telemetry)
    run_trace.end_with_grade(0.5, {"dims": 0.5})
    telemetry.flush()
    telemetry.shutdown()

    assert report.stop_reason == "report"
    assert report.validation_failed is False


# --------------------------------------------------------------------------
# The traceparent reaches the MCP call
# --------------------------------------------------------------------------


async def test_the_traceparent_reaches_the_mcps_call_kwargs() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    mcp = RecordingMCP()
    report, run_trace = await run_full_investigation(telemetry, mcp=mcp)
    run_trace.end()
    telemetry.flush()

    assert report.tool_calls > 0
    traceparents = [kw.get("traceparent") for kw in mcp.kwargs_seen]
    assert all(tp is not None for tp in traceparents)
    # W3C traceparent: version-traceid-spanid-flags
    for tp in traceparents:
        parts = tp.split("-")
        assert len(parts) == 4
        assert len(parts[1]) == 32
        assert len(parts[2]) == 16


# --------------------------------------------------------------------------
# Tool failures stay on their own span and count, without marking the root
# --------------------------------------------------------------------------


async def test_a_tool_not_allowed_marks_its_span_counts_and_leaves_the_root_unset() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    report, run_trace = await run_full_investigation(telemetry, mcp=NotAllowedMCP())
    run_trace.end()
    telemetry.flush()

    spans = exporter.get_finished_spans()
    failed = tool_spans(spans)
    assert failed
    for span in failed:
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes["error.type"] == "ToolNotAllowed"

    root = next(s for s in spans if s.name == "invoke_agent receipts-investigator")
    assert root.status.status_code == StatusCode.UNSET
    assert "error.type" not in root.attributes
    assert root.attributes["receipts.tool_errors"] == len(failed)
    assert report.validation_failed is True  # nothing was ever queried successfully


async def test_a_server_side_tool_error_marks_its_span_counts_and_leaves_the_root_unset() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    _, run_trace = await run_full_investigation(telemetry, mcp=ServerErrorMCP())
    run_trace.end()
    telemetry.flush()

    spans = exporter.get_finished_spans()
    failed = tool_spans(spans)
    assert failed
    for span in failed:
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes["error.type"] == "tool_error"
        assert span.attributes["gen_ai.tool.call.result"] == "bad filter column"

    root = next(s for s in spans if s.name == "invoke_agent receipts-investigator")
    assert root.status.status_code == StatusCode.UNSET
    assert root.attributes["receipts.tool_errors"] == len(failed)


# --------------------------------------------------------------------------
# A cancelled chat span (the loop's wall-clock timeout) is marked, not lost
# --------------------------------------------------------------------------


async def test_a_cancelled_chat_span_is_marked_timeout_and_still_reraises() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = start_test_run(telemetry, conversation_id=make_conversation_id("1"))

    with pytest.raises(asyncio.CancelledError):
        with run_trace.chat_span("claude-x", provider_name="anthropic", max_tokens=100):
            raise asyncio.CancelledError()

    run_trace.end()
    telemetry.flush()

    (chat_span,) = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    assert chat_span.status.status_code == StatusCode.ERROR
    assert chat_span.attributes["error.type"] == "timeout"


# --------------------------------------------------------------------------
# submit_report gets its own span
# --------------------------------------------------------------------------


async def test_the_submit_report_span_records_acceptance() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    _, run_trace = await run_full_investigation(telemetry)
    run_trace.end()
    telemetry.flush()

    span = next(s for s in exporter.get_finished_spans() if s.name == "execute_tool submit_report")
    assert span.attributes["gen_ai.tool.name"] == "submit_report"
    assert span.attributes["gen_ai.tool.call.result"] == "accepted"
    assert span.status.status_code == StatusCode.UNSET


async def test_the_submit_report_span_records_a_rejection_before_the_acceptance() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = start_test_run(telemetry, conversation_id=make_conversation_id("1"))

    bad = report_args(hypotheses=[dict(report_args()["hypotheses"][0], evidence=[])])
    provider = FakeProvider(
        [
            completion(_query_use("b"), negation_use("c"), baseline_use("d")),
            completion(use(SUBMIT_REPORT, bad, ident="d")),
            completion(use(SUBMIT_REPORT, report_args(), ident="e")),
        ]
    )
    report = await investigate(
        RUN,
        AgentConfig(),
        settings=make_settings(),
        mcp=FakeMCP(),
        provider=provider,
        trace=run_trace,
    )
    run_trace.end()
    telemetry.flush()

    assert report.validation_failed is False
    submit_spans = [
        s for s in exporter.get_finished_spans() if s.name == "execute_tool submit_report"
    ]
    assert len(submit_spans) == 2
    rejected, accepted = submit_spans

    assert rejected.status.status_code == StatusCode.ERROR
    assert rejected.attributes["error.type"] == "validation_rejected"
    assert rejected.attributes["receipts.validation.rejected"] is True
    assert "carries no evidence" in rejected.attributes["gen_ai.tool.call.result"]

    assert accepted.status.status_code == StatusCode.UNSET
    assert accepted.attributes["gen_ai.tool.call.result"] == "accepted"
    assert "receipts.validation.rejected" not in accepted.attributes


# --------------------------------------------------------------------------
# Token usage and the response id
# --------------------------------------------------------------------------


def test_chat_span_records_response_id_and_cache_inclusive_input_tokens() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = start_test_run(
        telemetry, conversation_id=make_conversation_id("1"), provider="anthropic"
    )

    comp = Completion(
        text="ok",
        tool_uses=[],
        usage=Usage(
            input_tokens=1000, output_tokens=200, cache_read_tokens=50, cache_write_tokens=25
        ),
        stop_reason="end_turn",
        response_model="claude-sonnet-4-5",
        response_id="msg_abc",
    )
    with run_trace.chat_span(
        "claude-sonnet-4-5", provider_name="anthropic", max_tokens=8192
    ) as chat:
        chat.record(comp, system="be careful", turns=[])
    run_trace.end()
    telemetry.flush()

    (chat_span,) = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    assert chat_span.attributes["gen_ai.response.id"] == "msg_abc"
    assert chat_span.attributes["gen_ai.response.model"] == "claude-sonnet-4-5"
    assert chat_span.attributes["gen_ai.usage.input_tokens"] == 1000 + 50 + 25
    assert chat_span.attributes["gen_ai.usage.output_tokens"] == 200
    assert chat_span.attributes["gen_ai.usage.cache_read.input_tokens"] == 50
    assert chat_span.attributes["gen_ai.usage.cache_write.input_tokens"] == 25


def test_cache_attributes_are_absent_when_there_is_no_cache_usage() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = start_test_run(telemetry, conversation_id=make_conversation_id("1"))

    comp = Completion(text="ok", tool_uses=[], usage=Usage(input_tokens=10, output_tokens=5))
    with run_trace.chat_span("m", provider_name="anthropic", max_tokens=10) as chat:
        chat.record(comp, system="s", turns=[])
    run_trace.end()
    telemetry.flush()

    (chat_span,) = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    assert chat_span.attributes["gen_ai.usage.input_tokens"] == 10
    assert "gen_ai.usage.cache_read.input_tokens" not in chat_span.attributes
    assert "gen_ai.usage.cache_write.input_tokens" not in chat_span.attributes


# --------------------------------------------------------------------------
# Truncation
# --------------------------------------------------------------------------


def test_tool_call_arguments_are_truncated_at_2kb() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = start_test_run(telemetry, conversation_id=make_conversation_id("1"))
    huge_args = {"filters": ["x" * 100 for _ in range(50)]}
    with run_trace.tool_span("run_query", "tu1", huge_args) as handle:
        handle.record_result("ok", is_error=False)
    run_trace.end()
    telemetry.flush()

    tool_span = next(s for s in exporter.get_finished_spans() if s.name.startswith("execute_tool "))
    arguments = tool_span.attributes["gen_ai.tool.call.arguments"]
    raw_json_len = len(json.dumps(huge_args, sort_keys=True))
    assert raw_json_len > T.ARGUMENTS_TRUNCATE_CHARS
    assert len(arguments) <= T.ARGUMENTS_TRUNCATE_CHARS
    assert arguments.endswith("...(truncated)")


def test_tool_call_result_is_truncated_at_500_chars() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = start_test_run(telemetry, conversation_id=make_conversation_id("1"))
    long_result = "y" * 900
    with run_trace.tool_span("run_query", "tu1", {}) as handle:
        handle.record_result(long_result, is_error=False)
    run_trace.end()
    telemetry.flush()

    tool_span = next(s for s in exporter.get_finished_spans() if s.name.startswith("execute_tool "))
    result_attr = tool_span.attributes["gen_ai.tool.call.result"]
    assert len(result_attr) <= T.RESULT_TRUNCATE_CHARS
    assert result_attr.endswith("...(truncated)")


def test_short_arguments_and_results_are_not_truncated() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = start_test_run(telemetry, conversation_id=make_conversation_id("1"))
    with run_trace.tool_span("run_query", "tu1", {"a": 1}) as handle:
        handle.record_result("short", is_error=False)
    run_trace.end()
    telemetry.flush()

    tool_span = next(s for s in exporter.get_finished_spans() if s.name.startswith("execute_tool "))
    expected_args = json.dumps({"a": 1}, sort_keys=True)
    assert tool_span.attributes["gen_ai.tool.call.arguments"] == expected_args
    assert tool_span.attributes["gen_ai.tool.call.result"] == "short"


def test_arguments_that_will_not_serialize_fall_back_to_str_instead_of_raising() -> None:
    """Mixed-type dict keys make json.dumps(..., sort_keys=True) raise even
    with default=str, since the failure is in comparing keys to sort them,
    not in encoding a value. tool_span must not let that reach the loop."""
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = start_test_run(telemetry, conversation_id=make_conversation_id("1"))
    bad_args = {1: "a", "b": 2}
    with run_trace.tool_span("run_query", "tu1", bad_args) as handle:
        handle.record_result("ok", is_error=False)
    run_trace.end()
    telemetry.flush()

    tool_span = next(s for s in exporter.get_finished_spans() if s.name.startswith("execute_tool "))
    assert tool_span.attributes["gen_ai.tool.call.arguments"] == str(bad_args)


# --------------------------------------------------------------------------
# The two Honeycomb datasets stay apart
# --------------------------------------------------------------------------


async def test_the_resource_names_the_agent_not_the_shop_dataset() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    _, run_trace = await run_full_investigation(telemetry)
    run_trace.end()
    telemetry.flush()

    settings = make_settings()
    spans = exporter.get_finished_spans()
    assert spans
    for span in spans:
        assert span.resource.attributes["service.name"] == "receipts-investigator"
        assert span.resource.attributes["service.name"] != settings.honeycomb_dataset


# --------------------------------------------------------------------------
# Secrets never land in a span
# --------------------------------------------------------------------------


async def test_secrets_never_appear_in_any_span_attribute_or_event() -> None:
    exporter = InMemorySpanExporter()
    settings = make_settings(capture_content=True, ingest_key=FAKE_INGEST_KEY)
    telemetry = Telemetry(settings, exporter=exporter)
    _, run_trace = await run_full_investigation(telemetry, settings=settings)
    run_trace.end_with_grade(0.5, {"dims": 0.5})
    telemetry.flush()

    blob_parts: list[str] = []
    for span in exporter.get_finished_spans():
        blob_parts.append(json.dumps(dict(span.attributes)))
        for event in span.events:
            blob_parts.append(json.dumps(dict(event.attributes)))
    blob = "\n".join(blob_parts)
    assert FAKE_INGEST_KEY not in blob
    assert FAKE_MCP_KEY not in blob
