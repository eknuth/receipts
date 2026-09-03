"""Tests for agent/mcp_client.py: the allowlist, pacing, and traceparent
injection. No test in this file talks to the network; the live smoke test
(`uv run python -m agent.mcp_client get_workspace_context`) is run by hand
and its redacted output is pasted into the R3 report, not exercised here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anyio
import mcp.types as types
import pytest
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.memory import create_client_server_memory_streams

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


async def test_rate_window_holds_without_the_min_interval() -> None:
    """With min_interval=0 only the sliding window can slow calls down. A
    bucket with the window removed passes the other pacing tests, since
    1.5 s spacing alone implies 40 per minute; this one catches it.
    """
    fake = FakeClock()
    bucket = TokenBucket(rate=40, period=60.0, min_interval=0, clock=fake.clock, sleep=fake.sleep)

    call_times = []
    for _ in range(45):
        await bucket.wait()
        call_times.append(fake.now)

    assert fake.now >= 60.0
    for t in call_times:
        assert sum(1 for other in call_times if t - 60.0 < other <= t) <= 40


async def test_concurrent_waiters_are_serialized() -> None:
    """Five callers arriving at once with the real clock still leave
    min_interval between calls. Without the lock they all pass at once."""
    bucket = TokenBucket(rate=1000, min_interval=0.02)
    times: list[float] = []

    async def one() -> None:
        await bucket.wait()
        times.append(time.monotonic())

    await asyncio.gather(*(one() for _ in range(5)))

    times.sort()
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(gap >= 0.018 for gap in gaps), gaps


async def test_min_interval_is_respected_even_under_the_rate_cap() -> None:
    fake = FakeClock()
    bucket = TokenBucket(
        rate=1000, period=60.0, min_interval=1.5, clock=fake.clock, sleep=fake.sleep
    )

    await bucket.wait()
    await bucket.wait()

    assert fake.now >= 1.5


# --------------------------------------------------------------------------
# Wire-level tests against an in-process MCP server
# --------------------------------------------------------------------------


class _RecordingServer:
    """An in-process MCP server that records what each tool call carried.

    Wired to HoneycombMCP over memory streams, so the real ClientSession
    and the real request serialization are exercised: what the server sees
    in `ctx.request_context.meta` is what went over the wire as `_meta`.
    """

    def __init__(self) -> None:
        self.server = MCPServer("fake-honeycomb")
        self.metas: list[Any] = []
        self.list_tools_calls = 0
        self._install_tools()

    def _install_tools(self) -> None:
        server = self.server

        @server.tool()
        async def get_workspace_context(ctx: Context) -> str:
            self.metas.append(ctx.request_context.meta)
            return "TEAM INFORMATION\nName: acme-team"

        @server.tool()
        async def run_query(dataset_slug: str, ctx: Context) -> str:
            self.metas.append(ctx.request_context.meta)
            if dataset_slug == "does-not-exist":
                raise ToolError(f"Invalid or missing dataset: {dataset_slug}")
            return "# Results\n\n| COUNT |\n| --- |\n| 1 |\n"

        lowlevel = server._lowlevel_server
        original = lowlevel.get_request_handler("tools/list")

        async def counting_list_tools(*args: Any, **kwargs: Any) -> Any:
            self.list_tools_calls += 1
            return await original.handler(*args, **kwargs)

        lowlevel.add_request_handler(
            "tools/list", types.PaginatedRequestParams, counting_list_tools
        )


@asynccontextmanager
async def in_process_mcp(
    settings: Settings, **kwargs: Any
) -> AsyncIterator[tuple[HoneycombMCP, _RecordingServer]]:
    """A HoneycombMCP entered against an in-process server, no network."""
    recording = _RecordingServer()
    lowlevel = recording.server._lowlevel_server

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:

            async def serve() -> None:
                await lowlevel.run(
                    *server_streams,
                    lowlevel.create_initialization_options(),
                    raise_exceptions=False,
                )

            tg.start_soon(serve)

            class InProcessMCP(HoneycombMCP):
                async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
                    return client_streams

            fake = FakeClock()
            bucket = kwargs.pop(
                "bucket", TokenBucket(min_interval=0, clock=fake.clock, sleep=fake.sleep)
            )
            async with InProcessMCP(settings=settings, bucket=bucket, **kwargs) as mcp:
                yield mcp, recording
            tg.cancel_scope.cancel()


async def test_traceparent_and_tracestate_reach_the_server_as_meta(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call(
            "get_workspace_context",
            {},
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            tracestate="hc=1",
        )

    assert server.metas == [
        {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "hc=1",
        }
    ]


async def test_no_trace_context_means_no_traceparent_on_the_wire(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_workspace_context", {})

    assert not server.metas[0]


async def test_server_error_is_flagged_not_swallowed(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, _):
        ok = await mcp.call("run_query", {"dataset_slug": "receipts-shop"})
        bad = await mcp.call("run_query", {"dataset_slug": "does-not-exist"})

    assert ok.is_error is False
    assert bad.is_error is True
    assert bad.query_id is None
    assert "does-not-exist" in bad.text
    assert bad.text.startswith("run_query failed:")


async def test_tools_list_is_fetched_once_on_enter_and_paced(settings: Settings) -> None:
    """The SDK validates each tool's first result against tools/list. Fetching
    the list on enter means that request is paced with everything else and
    the first real call is not paired with a hidden second request.
    """
    fake = FakeClock()
    bucket = TokenBucket(rate=40, min_interval=1.5, clock=fake.clock, sleep=fake.sleep)
    async with in_process_mcp(settings, bucket=bucket) as (mcp, server):
        assert server.list_tools_calls == 1
        assert len(bucket._call_times) == 1
        await mcp.call("get_workspace_context", {})
        await mcp.call("run_query", {"dataset_slug": "receipts-shop"})

    assert server.list_tools_calls == 1
    assert len(bucket._call_times) == 3


def test_meta_kwarg_serializes_to_wire_key_meta() -> None:
    """The SDK's own serialization: `meta=` on CallToolRequestParams lands at `_meta`."""
    params = types.CallToolRequestParams(
        name="get_trace",
        arguments={"trace_id": "abc"},
        meta={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
    )
    wire = params.model_dump(by_alias=True, exclude_none=True)
    assert wire["_meta"]["traceparent"] == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
