"""Tests for agent/handoff.py: the Canvas message, the poll loop, and the
keyword classifier. No test here makes a network call; `FakeCanvasMCP`
stands in for a `HoneycombMCP` opened with `allow_write=True`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agent.handoff import DEFAULT_DEADLINE_S, Handoff, classify, hand_off, render_message
from agent.report import Evidence, Hypothesis, Report
from agent.telemetry import Telemetry

FIXTURES = Path(__file__).parent / "fixtures" / "mcp"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def text_of(fixture: dict) -> str:
    return "\n".join(fixture["content_texts"])


def make_report(**overrides: Any) -> Report:
    base: dict[str, Any] = {
        "run_id": "run-abcdef123456",
        "scenario_id": "payments-stripe-v251-uswest",
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "incident_present": True,
        "hypotheses": [
            Hypothesis(
                claim="stripe calls in payments gained about 800ms in us-west-2 on 2.5.1",
                dims={"deployment.version": "2.5.1", "cloud.region": "us-west-2"},
                slow_or_failing_span="payments.charge",
                confidence="high",
                evidence=[
                    Evidence(
                        query_id="Q1",
                        summary="P99(duration_ms) went from 180ms to 980ms",
                        permalink="https://ui.honeycomb.io/team/environments/receipts-demo/result/Q1",
                    )
                ],
                negation=Evidence(
                    query_id="Q2",
                    summary="P99 flat outside deployment.version=2.5.1",
                    permalink="https://ui.honeycomb.io/team/environments/receipts-demo/result/Q2",
                ),
            )
        ],
        "not_checked": ["inventory-db timeouts", "the eu-west-1 window before onset"],
    }
    base.update(overrides)
    return Report(**base)


# --------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------


def test_message_carries_the_hypothesis_evidence_negation_not_checked_and_ends_with_the_ask() -> (
    None
):
    text = render_message(make_report())
    assert "stripe calls in payments gained about 800ms" in text
    assert "https://ui.honeycomb.io/team/environments/receipts-demo/result/Q1" in text
    assert "https://ui.honeycomb.io/team/environments/receipts-demo/result/Q2" in text
    assert "P99 flat outside deployment.version=2.5.1" in text
    assert "inventory-db timeouts" in text
    assert "the eu-west-1 window before onset" in text
    assert text.rstrip().endswith("Do you agree? What would you check next?")


def test_message_with_no_hypothesis_says_so_instead_of_being_silent() -> None:
    text = render_message(make_report(incident_present=False, hypotheses=[], not_checked=[]))
    assert "No incident found" in text
    assert "Not checked: nothing recorded." in text
    assert text.rstrip().endswith("Do you agree? What would you check next?")


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Yes, I agree with this finding.", "agree"),
        ("That looks correct to me.", "agree"),
        ("I disagree, the region looks wrong.", "disagree"),
        ("This is incorrect: the true cause is the eu-west-1 herring.", "disagree"),
        ("I'd also check the error rate for other providers.", "extend"),
        ("Worth checking the control window too.", "extend"),
        ("", "no_response"),
        ("   ", "no_response"),
        # A negated agreement word reads as disagree, not agree or extend:
        # `_AGREE` alone finds "agree" inside "do not agree" and would have
        # inflated exactly the "Canvas agreed with N of M" count the README
        # quotes, one direction only.
        ("I do not agree with this hypothesis.", "disagree"),
        ("I don't agree; the region is not correct.", "disagree"),
        ("That does not check out", "disagree"),
        ("I can't confirm this", "disagree"),
        ("doesn't make sense to me", "disagree"),
        # The second review (finding 1 of the re-review on 20b09e4): a
        # negation word that does not sit immediately next to the agreement
        # word used to defeat the old adjacency pattern entirely and read
        # as `agree`, one direction only, inflating the "Canvas agreed with
        # N of M" count. None of these have the negation word touching the
        # agreement word.
        ("I wouldn't agree", "disagree"),
        ("I'm not sure I agree", "disagree"),
        ("I don't think that checks out", "disagree"),
        ("I don't fully agree", "disagree"),
        ("I would not say this is correct", "disagree"),
        ("This is not entirely correct", "disagree"),
        ("hardly correct", "disagree"),
        ("isn't correct", "disagree"),
        ("Partially correct", "disagree"),
        # A disagreement word negated is a double negative, not a clean
        # agreement: a keyword rule cannot honestly resolve it, so it reads
        # as `extend` rather than guessing the literal double-negative
        # meaning (which would be `agree`).
        ("not incorrect", "extend"),
        ("I do not disagree", "extend"),
    ],
)
def test_classify_matches_the_documented_keywords(text: str, expected: str) -> None:
    assert classify(text) == expected


def test_classify_with_both_agree_and_disagree_words_returns_disagree() -> None:
    """Disagree is checked first: missing a disagreement is the worse mistake."""
    text = "I agree the span is right, but I disagree with the region you named."
    assert classify(text) == "disagree"


def test_classify_a_reply_that_answers_only_what_to_check_next_is_extend_not_agree() -> None:
    text = "Next I'd check whether eu-west-1 was already slow before onset."
    assert classify(text) == "extend"


# --------------------------------------------------------------------------
# The poll loop
# --------------------------------------------------------------------------


@dataclass
class _Result:
    raw: Any
    text: str = ""
    is_error: bool = False


@dataclass
class FakeCanvasMCP:
    """Records every call and answers from a queue keyed by tool name."""

    queued: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def queue(self, name: str, *results: Any) -> None:
        self.queued.setdefault(name, []).extend(results)

    async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls.append((name, dict(args or {})))
        pending = self.queued.get(name)
        if not pending:
            raise AssertionError(f"no queued response for {name!r}")
        result = pending.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def clock_sequence(*values: float) -> Any:
    """A clock that returns `values` in order, one per call, and raises
    `StopIteration` if called more times than the test expects."""
    it: Iterator[float] = iter(values)
    return lambda: next(it)


INVOKE_RUNNING = _Result(
    raw={
        "status": "running",
        "session_id": "sess-1",
        "investigation_id": "hcciv_abc123",
        "investigation_url": "https://ui.honeycomb.io/team/environments/receipts-demo/canvas/abc123",
    }
)


async def test_completed_on_first_poll() -> None:
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue(
        "canvas_agent_poll_response",
        _Result(raw={"status": "completed", "response": "I agree."}),
    )

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0, 0.0))

    assert result.status == "completed"
    assert result.classification == "agree"
    assert result.raw_text == "I agree."
    assert result.session_id == "sess-1"
    assert result.investigation_id == "hcciv_abc123"
    assert result.investigation_url.endswith("/canvas/abc123")


async def test_the_reply_field_is_chat_first_not_response() -> None:
    """The live server's completed reply carries the field `chat`, not
    `response` (verified 2026-09-07; see tests/fixtures/mcp/canvas_agent_poll_response.json).
    An earlier version of `hand_off` tried `response` first and would have
    read every real reply as empty; `chat` must win even when both are present."""
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue(
        "canvas_agent_poll_response",
        _Result(raw={"status": "completed", "chat": "from chat", "response": "from response"}),
    )

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0, 0.0))

    assert result.raw_text == "from chat"


async def test_the_default_title_carries_no_scenario_id() -> None:
    """CLAUDE.md keeps `scenario.id` off the wire because its values read
    as answers; Canvas is the one agent whose reply gets scored as a
    verdict on this investigation, so it is the last place that answer
    should leak. The default title used to be
    f"receipts {report.scenario_id} {report.run_id}"."""
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue("canvas_agent_poll_response", _Result(raw={"status": "completed", "chat": "ok"}))
    report = make_report(scenario_id="payments-stripe-v251-uswest")

    await hand_off(report, mcp, clock=clock_sequence(0.0, 0.0))

    _, invoke_args = next(call for call in mcp.calls if call[0] == "canvas_agent_invoke")
    assert invoke_args["title"] == f"receipts {report.run_id}"
    assert "payments-stripe-v251-uswest" not in invoke_args["title"]
    assert report.scenario_id not in invoke_args["title"]


async def test_an_explicit_title_is_still_used_as_given() -> None:
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue("canvas_agent_poll_response", _Result(raw={"status": "completed", "chat": "ok"}))

    await hand_off(make_report(), mcp, title="a custom title", clock=clock_sequence(0.0, 0.0))

    _, invoke_args = next(call for call in mcp.calls if call[0] == "canvas_agent_invoke")
    assert invoke_args["title"] == "a custom title"


async def test_the_real_captured_completed_reply_parses_and_classifies_as_extend() -> None:
    """Drives the poll step from the real (sanitized) live capture rather
    than a hand-built dict, so the test pins the server's actual shape."""
    invoke_fixture = load("canvas_agent_invoke")
    poll_fixture = load("canvas_agent_poll_response")
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", _Result(raw=text_of(invoke_fixture)))
    mcp.queue("canvas_agent_poll_response", _Result(raw=text_of(poll_fixture)))

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0, 0.0))

    assert result.status == "completed"
    assert result.classification != "no_response"
    assert "smoke test" in (result.raw_text or "")
    assert result.investigation_created is True
    assert result.investigation_id == "hcaiv_00fake0000000000000000000"
    assert result.session_id == "00000000-0000-4000-8000-000000000000"


async def test_investigation_created_defaults_to_none_when_the_field_is_absent() -> None:
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)  # no investigation_created key at all
    mcp.queue("canvas_agent_poll_response", _Result(raw={"status": "completed", "chat": "ok"}))

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0, 0.0))

    assert result.investigation_created is None


async def test_running_then_completed_polls_again_with_the_same_ids() -> None:
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue(
        "canvas_agent_poll_response",
        _Result(raw={"status": "running"}),
        _Result(raw={"status": "completed", "response": "Also check the eu-west-1 window."}),
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    result = await hand_off(
        make_report(),
        mcp,
        clock=clock_sequence(0.0, 0.0, 0.0, 40.0),
        sleep=no_sleep,
    )

    assert result.status == "completed"
    assert result.classification == "extend"
    poll_calls = [args for name, args in mcp.calls if name == "canvas_agent_poll_response"]
    assert len(poll_calls) == 2
    assert poll_calls[0] == poll_calls[1]  # same investigation_id and session_id both times


async def test_an_unrecognized_poll_status_sleeps_between_polls_instead_of_spinning() -> None:
    """A status the docs do not name (`queued`, say) used to be polled again
    immediately, with no client-side wait at all: a fake server answering it
    every time produced 200 polls inside one 300s budget, driven purely by
    however fast this loop and the caller's own MCP pacing could go, which
    on a live run burned through the whole team-wide rate limit on a single
    hand-off. Sleeping `wait_seconds` between such polls, the same amount a
    real "running" reply's own long poll would have spent, caps it at up to
    six for the 300s default (50s per poll)."""
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue(
        "canvas_agent_poll_response",
        *[_Result(raw={"status": "queued"}) for _ in range(6)],
    )

    state = {"t": 0.0}

    def clock() -> float:
        return state["t"]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        state["t"] += seconds

    result = await hand_off(make_report(), mcp, deadline_s=300.0, clock=clock, sleep=fake_sleep)

    assert result.status == "timeout"
    poll_calls = [name for name, _ in mcp.calls if name == "canvas_agent_poll_response"]
    assert len(poll_calls) == 6  # not 200
    assert sleep_calls == [50.0] * 6


async def test_a_running_status_answered_instantly_still_sleeps_instead_of_spinning() -> None:
    """The documented status, not just an unnamed one, has the same bug: a
    server that answers "running" without holding the connection for
    `wait_seconds` (a fake, or a real one under load) used to skip the sleep
    entirely, since the old code only slept when the status was something
    other than "running". Reproduced live at 199 polls and 0 sleeps in one
    300s budget. Measuring the elapsed time around the call, instead of
    trusting the server to have spent it, means an instant "running" reply
    still costs a sleep, the same as an unnamed status does."""
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue(
        "canvas_agent_poll_response",
        *[_Result(raw={"status": "running"}) for _ in range(6)],
    )

    state = {"t": 0.0}

    def clock() -> float:
        return state["t"]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        state["t"] += seconds

    result = await hand_off(make_report(), mcp, deadline_s=300.0, clock=clock, sleep=fake_sleep)

    assert result.status == "timeout"
    poll_calls = [name for name, _ in mcp.calls if name == "canvas_agent_poll_response"]
    assert len(poll_calls) == 6  # not 199
    assert sleep_calls == [50.0] * 6


async def test_a_slow_running_reply_sleeps_only_what_is_left_of_the_wait() -> None:
    """When the server does hold the connection for part of `wait_seconds`
    before answering "running", the sleep should make up only the
    difference, not the full `wait_seconds` again on top of it."""
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue(
        "canvas_agent_poll_response",
        _Result(raw={"status": "running"}),
        _Result(raw={"status": "completed", "chat": "ok"}),
    )

    state = {"t": 0.0}

    def clock() -> float:
        return state["t"]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        state["t"] += seconds

    async def call(name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if name == "canvas_agent_poll_response":
            state["t"] += 35.0  # the server actually held the connection for 35s
        return await FakeCanvasMCP.call(mcp, name, args, **kwargs)

    mcp.call = call  # type: ignore[method-assign]

    result = await hand_off(make_report(), mcp, deadline_s=300.0, clock=clock, sleep=fake_sleep)

    assert result.status == "completed"
    assert sleep_calls == [15.0]  # 50s wait_seconds minus the 35s the server already spent


async def test_the_deadline_expiring_stops_polling_and_records_a_timeout() -> None:
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    # No canvas_agent_poll_response queued at all: the deadline must be hit
    # before the loop tries to poll, or FakeCanvasMCP raises.

    # An explicit budget, so this pins the timeout behaviour and not whatever
    # DEFAULT_DEADLINE_S happens to be.
    result = await hand_off(make_report(), mcp, deadline_s=120.0, clock=clock_sequence(0.0, 130.0))

    assert result.status == "timeout"
    assert result.classification == "no_response"
    assert result.raw_text is None
    assert result.session_id == "sess-1"  # kept from the invoke, even though nothing came back


async def test_a_poll_error_is_recorded_and_never_raises() -> None:
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue("canvas_agent_poll_response", _Result(raw={"status": "error", "message": "boom"}))

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0, 0.0))

    assert result.status == "error"
    assert result.classification == "no_response"
    assert result.error == "boom"


async def test_a_poll_busy_is_recorded_distinctly_from_error() -> None:
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue(
        "canvas_agent_poll_response",
        _Result(raw={"status": "busy", "message": "another invocation is contending"}),
    )

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0, 0.0))

    assert result.status == "busy"
    assert result.classification == "no_response"


async def test_invoke_busy_never_polls_at_all() -> None:
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", _Result(raw={"status": "busy", "message": "mid-turn"}))

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0))

    assert result.status == "busy"
    assert result.classification == "no_response"
    assert not any(name == "canvas_agent_poll_response" for name, _ in mcp.calls)


async def test_an_invoke_that_raises_is_recorded_and_never_propagates() -> None:
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", RuntimeError("network is down"))

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0))

    assert result.status == "error"
    assert "network is down" in (result.error or "")


async def test_a_missing_reply_never_classifies_as_agreement() -> None:
    """The rule item 2 asks for by name: every no-response path is `no_response`,
    never silently `agree`."""
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue("canvas_agent_poll_response", _Result(raw={"status": "busy"}))

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0, 0.0))

    assert result.classification == "no_response"
    assert result.classification != "agree"


def test_default_deadline_covers_a_real_canvas_investigation() -> None:
    """The issue asked for 120s. A live run on 2026-09-07 timed out at 120
    with the investigation still going; the same handoff answered in 182s,
    so the budget has to clear that with room."""
    assert DEFAULT_DEADLINE_S >= 182.0


async def test_handoff_carries_the_board_fields_through_unchanged() -> None:
    """hand_off does not create the board; whatever the caller passes rides
    straight onto the Handoff it returns."""
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue("canvas_agent_poll_response", _Result(raw={"status": "completed", "response": "ok"}))

    result = await hand_off(
        make_report(),
        mcp,
        board_id="brd-1",
        board_url="https://ui.honeycomb.io/team/boards/brd-1",
        clock=clock_sequence(0.0, 0.0),
    )

    assert result.board_id == "brd-1"
    assert result.board_url == "https://ui.honeycomb.io/team/boards/brd-1"


def test_handoff_is_a_pydantic_model_with_the_required_fields() -> None:
    fields = Handoff.model_fields
    for name in (
        "classification",
        "raw_text",
        "investigation_id",
        "investigation_url",
        "investigation_created",
        "session_id",
        "board_id",
        "board_url",
        "prompt",
        "status",
        "error",
    ):
        assert name in fields


# --------------------------------------------------------------------------
# Telemetry (R23, EDW-1370): the Canvas exchange as execute_tool spans
# --------------------------------------------------------------------------


def _handoff_trace(exporter: InMemorySpanExporter) -> Any:
    telemetry = Telemetry(exporter=exporter)
    return telemetry, telemetry.start_handoff("run-abcdef123456", conversation_id="conv-1")


async def test_hand_off_wraps_both_canvas_calls_in_execute_tool_spans() -> None:
    """`canvas_agent_invoke` and each `canvas_agent_poll_response` (one poll,
    here) get their own `execute_tool` span, the same shape `agent/loop.py`'s
    own calls get, carrying `gen_ai.tool.name` and the call's arguments."""
    exporter = InMemorySpanExporter()
    telemetry, run_trace = _handoff_trace(exporter)
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue(
        "canvas_agent_poll_response",
        _Result(raw={"status": "completed", "chat": "I agree."}),
    )

    result = await hand_off(make_report(), mcp, trace=run_trace, clock=clock_sequence(0.0, 0.0))
    run_trace.end_with_handoff(
        status=result.status,
        classification=result.classification,
        reply=result.raw_text,
        board_id=None,
        board_url=None,
    )
    telemetry.flush()

    spans = exporter.get_finished_spans()
    invoke_spans = [s for s in spans if s.name == "execute_tool canvas_agent_invoke"]
    poll_spans = [s for s in spans if s.name == "execute_tool canvas_agent_poll_response"]
    assert len(invoke_spans) == 1
    assert len(poll_spans) == 1
    for span in invoke_spans + poll_spans:
        assert span.attributes["gen_ai.operation.name"] == "execute_tool"
        assert span.attributes["gen_ai.conversation.id"] == "conv-1"
        assert span.attributes["gen_ai.tool.call.arguments"]
    assert invoke_spans[0].attributes["gen_ai.tool.name"] == "canvas_agent_invoke"
    assert poll_spans[0].attributes["gen_ai.tool.name"] == "canvas_agent_poll_response"
    assert "receipts oauth smoke" not in invoke_spans[0].attributes["gen_ai.tool.call.arguments"]


async def test_hand_off_with_multiple_polls_gets_one_span_per_poll() -> None:
    exporter = InMemorySpanExporter()
    telemetry, run_trace = _handoff_trace(exporter)
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue(
        "canvas_agent_poll_response",
        _Result(raw={"status": "running"}),
        _Result(raw={"status": "completed", "chat": "ok"}),
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    await hand_off(
        make_report(),
        mcp,
        trace=run_trace,
        clock=clock_sequence(0.0, 0.0, 0.0, 40.0),
        sleep=no_sleep,
    )
    telemetry.flush()

    spans = exporter.get_finished_spans()
    poll_spans = [s for s in spans if s.name == "execute_tool canvas_agent_poll_response"]
    assert len(poll_spans) == 2


async def test_hand_off_with_no_trace_still_completes() -> None:
    """The default (`trace=None`) is a disabled trace, the same fallback
    `agent/loop.py`'s `investigate` uses; every other test in this file
    calls `hand_off` this way, so this just names the contract."""
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue("canvas_agent_poll_response", _Result(raw={"status": "completed", "chat": "ok"}))

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0, 0.0))

    assert result.status == "completed"


async def test_a_canvas_error_marks_the_poll_span_but_never_raises() -> None:
    exporter = InMemorySpanExporter()
    telemetry, run_trace = _handoff_trace(exporter)
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue("canvas_agent_poll_response", RuntimeError("canvas is down"))

    result = await hand_off(make_report(), mcp, trace=run_trace, clock=clock_sequence(0.0, 0.0))
    run_trace.end_with_handoff(
        status=result.status,
        classification=result.classification,
        reply=result.raw_text,
        board_id=None,
        board_url=None,
        error=result.error,
    )
    telemetry.flush()

    assert result.status == "error"
    assert result.classification == "no_response"
    spans = exporter.get_finished_spans()
    poll_span = next(s for s in spans if s.name == "execute_tool canvas_agent_poll_response")
    assert poll_span.status.status_code == StatusCode.ERROR
    root = next(s for s in spans if s.name == "invoke_agent canvas")
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["receipts.handoff.status"] == "error"
