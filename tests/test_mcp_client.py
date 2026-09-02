"""Tests for agent/mcp_client.py: the allowlist, pacing, and traceparent
injection. No test in this file talks to the network; the live smoke test
(`uv run python -m agent.mcp_client get_workspace_context`) is run by hand
and its redacted output is pasted into the R3 report, not exercised here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import mcp.types as types
import pytest

from agent.mcp_client import (
    READ_TOOLS,
    WRITE_TOOLS,
    HoneycombMCP,
    TokenBucket,
    ToolNotAllowed,
)
from receipts.settings import Settings

# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------


async def test_write_tool_without_allow_write_raises(settings: Settings) -> None:
    mcp = HoneycombMCP(settings=settings, allow_write=False)  # never entered: no session needed
    with pytest.raises(ToolNotAllowed):
        await mcp.call("create_board", {})


async def test_write_tool_with_allow_write_is_not_rejected_by_the_allowlist(
    settings: Settings,
) -> None:
    """allow_write=True clears the allowlist check; it still needs a session,
    so this asserts the *later* failure is the missing session, not ToolNotAllowed.
    """
    mcp = HoneycombMCP(settings=settings, allow_write=True)
    with pytest.raises(RuntimeError, match="must be entered"):
        await mcp.call("create_board", {})


async def test_unknown_tool_is_not_allowed(settings: Settings) -> None:
    mcp = HoneycombMCP(settings=settings)
    with pytest.raises(ToolNotAllowed):
        await mcp.call("delete_everything", {})


def test_read_tools_and_write_tools_are_disjoint() -> None:
    assert READ_TOOLS & WRITE_TOOLS == set()


# --------------------------------------------------------------------------
# Pacing: token bucket against a fake clock
# --------------------------------------------------------------------------


class FakeClock:
    """A clock whose `sleep` advances `now` instead of actually waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


async def test_45_calls_take_at_least_60s_simulated_and_none_fail() -> None:
    fake = FakeClock()
    bucket = TokenBucket(rate=40, period=60.0, min_interval=1.5, clock=fake.clock, sleep=fake.sleep)

    for _ in range(45):
        await bucket.wait()  # must never raise

    assert fake.now >= 60.0


async def test_pacing_never_exceeds_the_rate_in_any_60s_window() -> None:
    fake = FakeClock()
    bucket = TokenBucket(rate=40, period=60.0, min_interval=1.5, clock=fake.clock, sleep=fake.sleep)

    call_times = []
    for _ in range(90):
        await bucket.wait()
        call_times.append(fake.now)

    for t in call_times:
        in_window = sum(1 for other in call_times if t - 60.0 < other <= t)
        assert in_window <= 40


async def test_min_interval_is_respected_even_under_the_rate_cap() -> None:
    fake = FakeClock()
    bucket = TokenBucket(
        rate=1000, period=60.0, min_interval=1.5, clock=fake.clock, sleep=fake.sleep
    )

    await bucket.wait()
    await bucket.wait()

    assert fake.now >= 1.5


# --------------------------------------------------------------------------
# traceparent / tracestate injection
# --------------------------------------------------------------------------


@dataclass
class _StubTextContent:
    text: str
    type: str = "text"


@dataclass
class _StubCallToolResult:
    content: list[Any] = field(default_factory=list)
    structured_content: Any = None


class _StubSession:
    """Records every call_tool invocation; mimics ClientSession's call surface."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, *, meta=None):
        self.calls.append({"name": name, "arguments": arguments, "meta": meta})
        return _StubCallToolResult(content=[_StubTextContent(text="ok")])


async def test_call_forwards_traceparent_and_tracestate_into_session_meta(
    settings: Settings,
) -> None:
    mcp = HoneycombMCP(settings=settings)
    stub = _StubSession()
    mcp._session = stub  # bypass __aenter__: no real transport needed for this test

    await mcp.call(
        "get_workspace_context",
        {},
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        tracestate="hc=1",
    )

    assert len(stub.calls) == 1
    meta = stub.calls[0]["meta"]
    assert meta["traceparent"] == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert meta["tracestate"] == "hc=1"


async def test_call_omits_meta_when_no_trace_context_given(settings: Settings) -> None:
    mcp = HoneycombMCP(settings=settings)
    stub = _StubSession()
    mcp._session = stub

    await mcp.call("get_workspace_context", {})

    assert stub.calls[0]["meta"] is None


def test_meta_kwarg_serializes_to_wire_key_meta() -> None:
    """The mcp SDK's own serialization: `meta=` on CallToolRequestParams
    reaches the wire at the `_meta` key. This is what the mocked-session
    test above feeds into `session.call_tool(..., meta=...)`; together they
    show traceparent lands in `params._meta` on the wire.
    """
    params = types.CallToolRequestParams(
        name="get_trace",
        arguments={"trace_id": "abc"},
        meta={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
    )
    wire = params.model_dump(by_alias=True, exclude_none=True)
    assert wire["_meta"]["traceparent"] == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
