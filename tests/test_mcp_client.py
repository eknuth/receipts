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
    DATASET_SCOPED_TOOLS,
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


def test_dataset_scoped_tools_are_all_read_tools() -> None:
    assert DATASET_SCOPED_TOOLS <= READ_TOOLS


@pytest.mark.parametrize("tool", sorted(DATASET_SCOPED_TOOLS))
async def test_a_dataset_scoped_tool_without_dataset_slug_is_refused(
    tool: str, settings: Settings
) -> None:
    mcp = HoneycombMCP(settings=settings)  # never entered: refused before any session is needed
    with pytest.raises(ToolNotAllowed, match=settings.honeycomb_dataset):
        await mcp.call(tool, {})


@pytest.mark.parametrize("tool", sorted(DATASET_SCOPED_TOOLS))
async def test_a_dataset_scoped_tool_naming_a_different_dataset_is_refused(
    tool: str, settings: Settings
) -> None:
    mcp = HoneycombMCP(settings=settings)
    with pytest.raises(ToolNotAllowed, match=settings.honeycomb_dataset):
        await mcp.call(tool, {"dataset_slug": "receipts-investigator"})


def test_run_bubbleup_is_not_dataset_scoped() -> None:
    """The hosted schema has no `dataset_slug` parameter for it; it is
    scoped by provenance instead (see the wire-level tests below)."""
    assert "run_bubbleup" not in DATASET_SCOPED_TOOLS
    assert "run_bubbleup" in READ_TOOLS


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
        self.query_run_pks: list[str] = []
        self.bubbleup_calls: list[dict[str, Any]] = []
        self.bubbleup_result_ids: list[str] = []
        self._install_tools()

    def _install_tools(self) -> None:
        server = self.server

        @server.tool()
        async def get_workspace_context(ctx: Context) -> str:
            self.metas.append(ctx.request_context.meta)
            return "TEAM INFORMATION\nName: acme-team"

        @server.tool()
        async def run_query(
            dataset_slug: str, ctx: Context, query_spec: dict[str, Any] | None = None
        ) -> str:
            self.metas.append(ctx.request_context.meta)
            # The client's own dataset guard refuses a mismatched dataset_slug
            # before this ever runs, so a server-side failure is triggered a
            # different way here: a marker in the query spec.
            if (query_spec or {}).get("force_error"):
                raise ToolError("Invalid or missing dataset: does-not-exist")
            pk = f"qp-{len(self.query_run_pks) + 1}"
            self.query_run_pks.append(pk)
            return (
                f"# Results\n\n| COUNT |\n| --- |\n| 1 |\n\n---\nMetadata:\n  query_run_pk: {pk}\n"
            )

        @server.tool()
        async def run_bubbleup(
            ctx: Context,
            query_pk: str | None = None,
            bubbleup_result_id: str | None = None,
            dataset_slug: str | None = None,
            selection: dict[str, Any] | None = None,
        ) -> str:
            """No `dataset_slug` on the real schema; kept here, defaulted to
            None, only so a test can prove the client never sends one."""
            self.metas.append(ctx.request_context.meta)
            self.bubbleup_calls.append(
                {
                    "query_pk": query_pk,
                    "bubbleup_result_id": bubbleup_result_id,
                    "dataset_slug": dataset_slug,
                }
            )
            result_id = f"bu-{len(self.bubbleup_result_ids) + 1}"
            self.bubbleup_result_ids.append(result_id)
            source_pk = query_pk or bubbleup_result_id or "unknown"
            return (
                "# BubbleUp Analysis\n\n**1 significant columns**\n\n"
                "## Dimensions\n\n"
                "**duration_ms** (100% baseline / 100% selection populated)\n"
                "- 5: 0.0% → 100.0% (↑ 100.0%)\n\n\n"
                "---\nMetadata:\n"
                f"  query_run_pk: {source_pk}\n"
                f"  bubbleup_result_id: {result_id}\n"
            )

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
        bad = await mcp.call(
            "run_query",
            {"dataset_slug": "receipts-shop", "query_spec": {"force_error": True}},
        )

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


# --------------------------------------------------------------------------
# run_bubbleup: scoped by provenance, not dataset_slug
# --------------------------------------------------------------------------


async def test_bubbleup_on_a_query_pk_from_run_query_passes(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        rq = await mcp.call("run_query", {"dataset_slug": settings.honeycomb_dataset})
        result = await mcp.call("run_bubbleup", {"query_pk": rq.query_id})

    assert result.is_error is False
    assert server.bubbleup_calls[-1]["query_pk"] == rq.query_id


async def test_bubbleup_on_an_unknown_query_pk_is_refused(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        with pytest.raises(ToolNotAllowed, match="was not produced by a run_query"):
            await mcp.call("run_bubbleup", {"query_pk": "not-a-real-query-pk"})

    assert server.bubbleup_calls == []


async def test_bubbleup_on_a_bubbleup_result_id_from_an_earlier_bubbleup_passes(
    settings: Settings,
) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        rq = await mcp.call("run_query", {"dataset_slug": settings.honeycomb_dataset})
        await mcp.call("run_bubbleup", {"query_pk": rq.query_id})
        first_result_id = server.bubbleup_result_ids[0]

        # The drill-down call carries only the prior bubbleup's own id, not
        # the original query_pk.
        result = await mcp.call("run_bubbleup", {"bubbleup_result_id": first_result_id})

    assert result.is_error is False
    assert server.bubbleup_calls[-1]["bubbleup_result_id"] == first_result_id
    assert server.bubbleup_calls[-1]["query_pk"] is None


async def test_bubbleup_stray_dataset_slug_is_stripped_not_forwarded(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        rq = await mcp.call("run_query", {"dataset_slug": settings.honeycomb_dataset})
        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "dataset_slug": settings.honeycomb_dataset},
        )

    assert result.is_error is False
    assert server.bubbleup_calls[-1]["dataset_slug"] is None


async def test_ids_from_a_refused_run_query_are_not_recorded(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        with pytest.raises(ToolNotAllowed):
            await mcp.call("run_query", {"dataset_slug": "receipts-investigator"})

        assert mcp._produced_ids == set()

        with pytest.raises(ToolNotAllowed, match="was not produced by a run_query"):
            await mcp.call("run_bubbleup", {"query_pk": "anything"})

    assert server.query_run_pks == []
    assert server.bubbleup_calls == []


def test_meta_kwarg_serializes_to_wire_key_meta() -> None:
    """The SDK's own serialization: `meta=` on CallToolRequestParams lands at `_meta`."""
    params = types.CallToolRequestParams(
        name="get_trace",
        arguments={"trace_id": "abc"},
        meta={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
    )
    wire = params.model_dump(by_alias=True, exclude_none=True)
    assert wire["_meta"]["traceparent"] == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
