"""Tests for agent/handoff.py: the Canvas message, the poll loop, and the
keyword classifier. No test here makes a network call; `FakeCanvasMCP`
stands in for a `HoneycombMCP` opened with `allow_write=True`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent.handoff import DEFAULT_DEADLINE_S, Handoff, classify, hand_off, render_message
from agent.report import Evidence, Hypothesis, Report


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


async def test_running_then_completed_polls_again_with_the_same_ids() -> None:
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    mcp.queue(
        "canvas_agent_poll_response",
        _Result(raw={"status": "running"}),
        _Result(raw={"status": "completed", "response": "Also check the eu-west-1 window."}),
    )

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0, 0.0, 40.0))

    assert result.status == "completed"
    assert result.classification == "extend"
    poll_calls = [args for name, args in mcp.calls if name == "canvas_agent_poll_response"]
    assert len(poll_calls) == 2
    assert poll_calls[0] == poll_calls[1]  # same investigation_id and session_id both times


async def test_the_deadline_expiring_stops_polling_and_records_a_timeout() -> None:
    mcp = FakeCanvasMCP()
    mcp.queue("canvas_agent_invoke", INVOKE_RUNNING)
    # No canvas_agent_poll_response queued at all: the deadline must be hit
    # before the loop tries to poll, or FakeCanvasMCP raises.

    result = await hand_off(make_report(), mcp, clock=clock_sequence(0.0, 130.0))

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


def test_default_deadline_is_120s() -> None:
    assert DEFAULT_DEADLINE_S == 120.0


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
        "session_id",
        "board_id",
        "board_url",
        "prompt",
        "status",
        "error",
    ):
        assert name in fields
