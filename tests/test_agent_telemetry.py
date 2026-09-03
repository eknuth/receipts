"""Tests for agent/telemetry.py: the OTel GenAI spans the investigator emits about itself.

No network, ever. Real spans come from `Telemetry(exporter=...)`, an
in-memory exporter wired through a `SimpleSpanProcessor` so a span is visible
the moment it ends, no flush timing to race. The loop itself is driven the
same way `test_agent_loop.py` drives it: a `FakeProvider` replaying scripted
completions and a `FakeMCP` answering tool calls, so the whole thing runs
without a model or the network.
"""

from __future__ import annotations

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
from agent.telemetry import Telemetry
from receipts.settings import Settings

FAKE_INGEST_KEY = "fake-ingest-key-do-not-leak"
FAKE_MCP_KEY = "fake-key-id:fake-secret-do-not-leak"


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
    """A conversation id shaped like the ones evals/run.py and agent/__main__.py build:
    the run id plus something that makes this investigation unique, never the
    run id alone (see the module docstring in agent/telemetry.py)."""
    return f"{RUN.run_id}/full/{suffix}"


async def run_full_investigation(
    telemetry: Telemetry,
    *,
    mcp: FakeMCP | None = None,
    settings: Settings | None = None,
    conversation_id: str | None = None,
):
    """One complete investigation under a real (in-memory) trace. Returns (report, run_trace)."""
    run_trace = telemetry.start_run(
        RUN.run_id,
        RUN.scenario_id,
        conversation_id=conversation_id or make_conversation_id(),
        config_label="full",
        provider="fake",
    )
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
    # a root span, three chat spans (one per model turn), and four tool calls
    # (get_workspace_context, then three run_query calls), per good_script().
    assert len(spans) == 1 + 3 + 4
    for span in spans:
        assert span.attributes["gen_ai.conversation.id"] == conversation_id
        assert span.attributes["gen_ai.agent.name"] == "receipts-investigator"
        assert span.attributes["gen_ai.operation.name"] in {"invoke_agent", "chat", "execute_tool"}

    root = next(s for s in spans if s.name == "invoke_agent receipts-investigator")
    assert root.attributes["receipts.run_id"] == RUN.run_id


def test_the_root_span_names_and_operation() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    conversation_id = make_conversation_id("1")
    run_trace = telemetry.start_run(
        RUN.run_id,
        RUN.scenario_id,
        conversation_id=conversation_id,
        config_label="full",
        provider="anthropic",
    )
    run_trace.end()
    telemetry.flush()

    (span,) = exporter.get_finished_spans()
    assert span.name == "invoke_agent receipts-investigator"
    assert span.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert span.attributes["gen_ai.conversation.id"] == conversation_id
    assert span.attributes["receipts.run_id"] == RUN.run_id
    assert span.attributes["scenario.id"] == RUN.scenario_id
    assert span.attributes["agent.config"] == "full"
    assert span.attributes["agent.provider"] == "anthropic"


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


async def test_a_crash_ends_the_root_span_with_an_error_and_no_evaluation_result() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = telemetry.start_run(
        RUN.run_id,
        RUN.scenario_id,
        conversation_id=make_conversation_id("1"),
        config_label="full",
        provider="fake",
    )
    run_trace.end_with_error("RuntimeError", "the provider fell over")
    telemetry.flush()

    (root,) = exporter.get_finished_spans()
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["error.type"] == "RuntimeError"
    assert "gen_ai.evaluation.result" not in root.attributes


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


async def test_content_capture_turns_on_with_the_flag() -> None:
    exporter = InMemorySpanExporter()
    settings = make_settings(capture_content=True)
    telemetry = Telemetry(settings, exporter=exporter)
    assert telemetry.capture_content is True
    _, run_trace = await run_full_investigation(telemetry, settings=settings)
    run_trace.end()
    telemetry.flush()

    chat_spans = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    assert chat_spans
    captured = False
    for span in chat_spans:
        events = {event.name: event for event in span.events}
        if "gen_ai.input.messages" in events:
            captured = True
            raw = events["gen_ai.input.messages"].attributes["gen_ai.input.messages"]
            payload = json.loads(raw)
            assert isinstance(payload, list)
            assert payload[0]["role"] == "system"
        if "gen_ai.output.messages" in events:
            raw = events["gen_ai.output.messages"].attributes["gen_ai.output.messages"]
            payload = json.loads(raw)
            assert isinstance(payload, list)
    assert captured


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
# Tool failures set ERROR status and propagate it to the root span
# --------------------------------------------------------------------------


async def test_a_tool_not_allowed_sets_error_status_and_propagates_to_the_root() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    report, run_trace = await run_full_investigation(telemetry, mcp=NotAllowedMCP())
    run_trace.end()
    telemetry.flush()

    spans = exporter.get_finished_spans()
    tool_span = next(s for s in spans if s.name.startswith("execute_tool "))
    assert tool_span.status.status_code == StatusCode.ERROR
    assert tool_span.attributes["error.type"] == "ToolNotAllowed"

    root = next(s for s in spans if s.name == "invoke_agent receipts-investigator")
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["error.type"] == "ToolNotAllowed"
    assert report.validation_failed is True  # nothing was ever queried successfully


async def test_a_server_side_tool_error_sets_error_status() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    _, run_trace = await run_full_investigation(telemetry, mcp=ServerErrorMCP())
    run_trace.end()
    telemetry.flush()

    spans = exporter.get_finished_spans()
    tool_span = next(s for s in spans if s.name.startswith("execute_tool "))
    assert tool_span.status.status_code == StatusCode.ERROR
    assert tool_span.attributes["error.type"] == "tool_error"
    assert tool_span.attributes["gen_ai.tool.call.result"] == "bad filter column"


# --------------------------------------------------------------------------
# Truncation
# --------------------------------------------------------------------------


def test_tool_call_arguments_are_truncated_at_2kb() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = telemetry.start_run(
        RUN.run_id,
        RUN.scenario_id,
        conversation_id=make_conversation_id("1"),
        config_label="full",
        provider="fake",
    )
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
    run_trace = telemetry.start_run(
        RUN.run_id,
        RUN.scenario_id,
        conversation_id=make_conversation_id("1"),
        config_label="full",
        provider="fake",
    )
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
    run_trace = telemetry.start_run(
        RUN.run_id,
        RUN.scenario_id,
        conversation_id=make_conversation_id("1"),
        config_label="full",
        provider="fake",
    )
    with run_trace.tool_span("run_query", "tu1", {"a": 1}) as handle:
        handle.record_result("short", is_error=False)
    run_trace.end()
    telemetry.flush()

    tool_span = next(s for s in exporter.get_finished_spans() if s.name.startswith("execute_tool "))
    expected_args = json.dumps({"a": 1}, sort_keys=True)
    assert tool_span.attributes["gen_ai.tool.call.arguments"] == expected_args
    assert tool_span.attributes["gen_ai.tool.call.result"] == "short"


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
