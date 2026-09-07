"""Tests for agent/mcp_client.py: the allowlist, pacing, and traceparent
injection. No test in this file talks to the network; the live smoke test
(`uv run python -m agent.mcp_client get_workspace_context`) is run by hand
and its redacted output is pasted into the R3 report, not exercised here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
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
    GROUP_INDICES_ERROR,
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


def test_write_tools_is_exactly_the_two_r12_uses() -> None:
    assert WRITE_TOOLS == {"create_board", "canvas_agent_invoke"}


# The management key was widened to `mcp:write` on 2026-09-07 (see CLAUDE.md's
# Honeycomb facts) and the hosted server now also serves these five tools.
# None of them is in scope for R12, and a wider key must not silently widen
# what this client will call, so each one must stay refused even with
# allow_write=True.
_WIDER_KEY_ONLY_TOOLS = (
    "create_trigger",
    "create_slo",
    "create_recipient",
    "create_marker",
    "update_board",
)


@pytest.mark.parametrize("tool", _WIDER_KEY_ONLY_TOOLS)
async def test_a_tool_the_wider_key_serves_but_r12_does_not_use_stays_refused(
    tool: str, settings: Settings
) -> None:
    mcp = HoneycombMCP(settings=settings, allow_write=True)  # never entered: refused pre-session
    with pytest.raises(ToolNotAllowed):
        await mcp.call(tool, {})


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


async def test_run_query_with_environment_wide_query_is_refused(settings: Settings) -> None:
    """`environment_wide_query=true` makes the server query every dataset in
    the environment regardless of `dataset_slug`, so a correct `dataset_slug`
    alongside it does not save the call."""
    mcp = HoneycombMCP(settings=settings)  # never entered: refused before any session is needed
    with pytest.raises(ToolNotAllowed, match="environment_wide_query"):
        await mcp.call(
            "run_query",
            {"dataset_slug": settings.honeycomb_dataset, "environment_wide_query": True},
        )


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
        # The `breakdowns` each `run_query` call carried, keyed by the
        # `query_run_pk` it returned. `run_bubbleup` below reads this to
        # decide whether the source query supports the group selection.
        self.breakdowns_by_pk: dict[str, list[str]] = {}
        # The `selection` argument of every `run_bubbleup` call, in order,
        # whatever the client did to it (dropped `dataset_slug`, retyped
        # group values). What is asserted on is what went on the wire.
        self.bubbleup_selections: list[Any] = []
        # Set by a test to make the next `run_bubbleup` fail with the group
        # indices text whatever its selection, for the hint's edge cases.
        self.fail_next_bubbleup = False
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
            self.breakdowns_by_pk[pk] = [str(c) for c in (query_spec or {}).get("breakdowns") or []]
            return (
                f"# Results\n\n| COUNT |\n| --- |\n| 1 |\n\n---\nMetadata:\n  query_run_pk: {pk}\n"
            )

        @server.tool()
        async def get_dataset_columns(dataset_slug: str, ctx: Context) -> str:
            self.metas.append(ctx.request_context.meta)
            return (
                "# Columns\n\n"
                "| Name | Type | Description | LastWritten |\n"
                "| --- | --- | --- | --- |\n"
                "| error | boolean |  | 2026-09-04 00:00:00 |\n"
                "| cart.size | integer |  | 2026-09-04 00:00:00 |\n"
                "| duration_ms | float |  | 2026-09-04 00:00:00 |\n"
                "| name | string |  | 2026-09-04 00:00:00 |\n"
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
            self.bubbleup_selections.append(selection)
            if self.fail_next_bubbleup:
                self.fail_next_bubbleup = False
                raise ToolError(GROUP_INDICES_ERROR)
            group = selection.get("group") if isinstance(selection, dict) else None
            if isinstance(group, dict) and group:
                # The live behavior this stands in for: a group value of the
                # wrong JSON type, or a source query that never broke down
                # on the group column, both come back as this exact text.
                carries_a_string = any(isinstance(v, str) for v in group.values())
                breakdowns = self.breakdowns_by_pk.get(query_pk or "", [])
                if carries_a_string or not breakdowns:
                    raise ToolError(GROUP_INDICES_ERROR)
            result_id = f"bu-{len(self.bubbleup_result_ids) + 1}"
            self.bubbleup_result_ids.append(result_id)
            source_pk = query_pk or bubbleup_result_id or "unknown"
            # The live shape (tests/fixtures/mcp/run_bubbleup.json): the
            # result's own id is a query parameter on bubble_up_url, not a
            # plain `bubbleup_result_id:` Metadata line.
            url = (
                "https://ui.honeycomb.io/acme-team/environments/receipts-demo/datasets/"
                f"receipts-shop/result/{source_pk}?tab=bubbleup&bubbleup_result={result_id}"
            )
            return (
                "# BubbleUp Analysis\n\n**1 significant columns**\n\n"
                "## Dimensions\n\n"
                "**duration_ms** (100% baseline / 100% selection populated)\n"
                "- 5: 0.0% → 100.0% (↑ 100.0%)\n\n\n"
                "---\nMetadata:\n"
                f'  bubble_up_url: "{url}"\n'
                f"  query_run_pk: {source_pk}\n"
            )

        # Stub tools the server advertises but this module's other tests
        # never call: registered so a `list_tools` call has something to
        # filter, for the allowlist test that checks the filtering rather
        # than the wire behavior of any one of these.
        for stub_name in (
            "canvas_agent_invoke",
            "canvas_agent_poll_response",
            "create_board",
            "list_boards",
            "create_trigger",
            "create_slo",
            "create_recipient",
            "create_marker",
            "update_board",
        ):

            def make_stub(name: str) -> Any:
                async def stub() -> str:
                    return f"{name} stub"

                return stub

            server.tool(name=stub_name)(make_stub(stub_name))

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
    settings: Settings,
    *,
    pre_enter: Callable[[HoneycombMCP], None] | None = None,
    **kwargs: Any,
) -> AsyncIterator[tuple[HoneycombMCP, _RecordingServer]]:
    """A HoneycombMCP entered against an in-process server, no network.

    `pre_enter`, when given, runs on the constructed-but-not-yet-entered
    client, so a test can seed state (e.g. `_produced_ids`) and assert on
    what `__aenter__` does to it.
    """
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
            unentered = InProcessMCP(settings=settings, bucket=bucket, **kwargs)
            if pre_enter is not None:
                pre_enter(unentered)
            async with unentered as mcp:
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


async def test_list_tools_admits_the_in_scope_write_tools_and_filters_the_rest(
    settings: Settings,
) -> None:
    """The server (stubbed above) advertises `create_board` and
    `canvas_agent_invoke` alongside the five the wider key serves but R12
    does not use; `list_tools` with `allow_write=True` returns the former
    and drops the latter, the same as it always dropped an unknown name."""
    async with in_process_mcp(settings, allow_write=True) as (mcp, _):
        names = {spec.name for spec in await mcp.list_tools()}

    in_scope = {"create_board", "canvas_agent_invoke", "list_boards", "canvas_agent_poll_response"}
    assert in_scope <= names
    assert names.isdisjoint(_WIDER_KEY_ONLY_TOOLS)


async def test_list_tools_without_allow_write_omits_every_write_tool(settings: Settings) -> None:
    async with in_process_mcp(settings, allow_write=False) as (mcp, _):
        names = {spec.name for spec in await mcp.list_tools()}

    assert names.isdisjoint(WRITE_TOOLS)
    assert "list_boards" in names  # a read tool, present either way


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


async def test_bubbleup_on_an_unknown_bubbleup_result_id_is_refused(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        with pytest.raises(ToolNotAllowed, match="was not produced by a BubbleUp"):
            await mcp.call("run_bubbleup", {"bubbleup_result_id": "not-a-real-result-id"})

    assert server.bubbleup_calls == []


async def test_bubbleup_known_query_pk_with_a_foreign_bubbleup_result_id_is_refused(
    settings: Settings,
) -> None:
    """The live schema gives `bubbleup_result_id` precedence: when both are
    present the server pages the named result and ignores `query_pk`. A
    known `query_pk` must not vouch for a `bubbleup_result_id` this session
    never produced."""
    async with in_process_mcp(settings) as (mcp, server):
        rq = await mcp.call("run_query", {"dataset_slug": settings.honeycomb_dataset})
        with pytest.raises(ToolNotAllowed, match="was not produced by a BubbleUp"):
            await mcp.call(
                "run_bubbleup",
                {"query_pk": rq.query_id, "bubbleup_result_id": "a-foreign-result-id"},
            )

    assert server.bubbleup_calls == []


async def test_bubbleup_known_result_id_with_a_foreign_query_pk_passes(
    settings: Settings,
) -> None:
    """The other side of the same precedence rule: a known
    `bubbleup_result_id` is enough on its own, even paired with a `query_pk`
    this session never produced, because the server ignores that `query_pk`."""
    async with in_process_mcp(settings) as (mcp, server):
        rq = await mcp.call("run_query", {"dataset_slug": settings.honeycomb_dataset})
        await mcp.call("run_bubbleup", {"query_pk": rq.query_id})
        known_result_id = server.bubbleup_result_ids[0]

        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": "a-foreign-query-pk", "bubbleup_result_id": known_result_id},
        )

    assert result.is_error is False
    assert server.bubbleup_calls[-1]["bubbleup_result_id"] == known_result_id
    assert server.bubbleup_calls[-1]["query_pk"] == "a-foreign-query-pk"


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


async def test_ids_from_an_environment_wide_run_query_are_not_recorded(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        with pytest.raises(ToolNotAllowed, match="environment_wide_query"):
            await mcp.call(
                "run_query",
                {"dataset_slug": settings.honeycomb_dataset, "environment_wide_query": True},
            )

        assert mcp._produced_ids == set()

    assert server.query_run_pks == []


async def test_produced_ids_are_cleared_on_enter(settings: Settings) -> None:
    """A re-entered client (a fresh `async with` on the same instance) must
    not carry `run_bubbleup` provenance over from a prior session."""

    def seed_with_a_leftover_id(mcp: HoneycombMCP) -> None:
        mcp._produced_ids.add("leftover-from-a-prior-session")

    async with in_process_mcp(settings, pre_enter=seed_with_a_leftover_id) as (mcp, server):
        assert mcp._produced_ids == set()

        with pytest.raises(ToolNotAllowed, match="was not produced by a run_query"):
            await mcp.call("run_bubbleup", {"query_pk": "leftover-from-a-prior-session"})

    assert server.bubbleup_calls == []


# --------------------------------------------------------------------------
# EDW-1362: a run_bubbleup group selection retyped from the column schema
# --------------------------------------------------------------------------


async def test_group_boolean_string_is_retyped_after_a_columns_call(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_dataset_columns", {"dataset_slug": settings.honeycomb_dataset})
        rq = await mcp.call(
            "run_query",
            {"dataset_slug": settings.honeycomb_dataset, "query_spec": {"breakdowns": ["error"]}},
        )
        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "group", "group": {"error": "true"}}},
        )

    assert result.is_error is False
    assert server.bubbleup_selections[-1]["group"] == {"error": True}
    assert result.coerced == ("selection.group.error",)


async def test_group_integer_and_float_strings_are_retyped(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_dataset_columns", {"dataset_slug": settings.honeycomb_dataset})
        rq = await mcp.call(
            "run_query",
            {
                "dataset_slug": settings.honeycomb_dataset,
                "query_spec": {"breakdowns": ["cart.size", "duration_ms"]},
            },
        )

        by_size = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "group", "group": {"cart.size": "8"}}},
        )
        assert server.bubbleup_selections[-1]["group"] == {"cart.size": 8}
        assert by_size.coerced == ("selection.group.cart.size",)

        by_duration = await mcp.call(
            "run_bubbleup",
            {
                "query_pk": rq.query_id,
                "selection": {"type": "group", "group": {"duration_ms": "1.5"}},
            },
        )
        assert server.bubbleup_selections[-1]["group"] == {"duration_ms": 1.5}
        assert by_duration.coerced == ("selection.group.duration_ms",)


async def test_group_boolean_string_is_case_insensitive(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_dataset_columns", {"dataset_slug": settings.honeycomb_dataset})
        rq = await mcp.call(
            "run_query",
            {"dataset_slug": settings.honeycomb_dataset, "query_spec": {"breakdowns": ["error"]}},
        )
        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "group", "group": {"error": "TRUE"}}},
        )

    assert result.is_error is False
    assert server.bubbleup_selections[-1]["group"] == {"error": True}


async def test_only_json_numeric_literals_are_retyped(settings: Settings) -> None:
    """Python's own parsers take `"1_000"`, `" 8 "`, `"+8"`, and `"nan"`, and
    a `nan` would go on the wire as `null`. The server speaks JSON, so only
    a JSON numeric literal is retyped; everything else stays the string the
    model wrote."""
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_dataset_columns", {"dataset_slug": settings.honeycomb_dataset})
        rq = await mcp.call(
            "run_query",
            {
                "dataset_slug": settings.honeycomb_dataset,
                "query_spec": {"breakdowns": ["cart.size", "duration_ms"]},
            },
        )
        for column, value in [
            ("cart.size", "1_000"),
            ("cart.size", " 8 "),
            ("cart.size", "+8"),
            ("cart.size", "8.0"),
            ("cart.size", "0x10"),
            ("duration_ms", "nan"),
            ("duration_ms", "inf"),
            ("duration_ms", "1_5.0"),
        ]:
            result = await mcp.call(
                "run_bubbleup",
                {"query_pk": rq.query_id, "selection": {"type": "group", "group": {column: value}}},
            )
            assert server.bubbleup_selections[-1]["group"] == {column: value}, (column, value)
            assert result.coerced == (), (column, value)

        for column, value, wanted in [
            ("cart.size", "-8", -8),
            ("cart.size", "0", 0),
            ("duration_ms", "1e3", 1000.0),
            ("duration_ms", "-0.5", -0.5),
            ("duration_ms", "8", 8.0),
        ]:
            await mcp.call(
                "run_bubbleup",
                {"query_pk": rq.query_id, "selection": {"type": "group", "group": {column: value}}},
            )
            sent = server.bubbleup_selections[-1]["group"][column]
            assert sent == wanted and type(sent) is type(wanted), (column, value)


async def test_a_string_column_value_is_untouched(settings: Settings) -> None:
    """`name` is typed `string`, so its own string value is not a coercion target."""
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_dataset_columns", {"dataset_slug": settings.honeycomb_dataset})
        rq = await mcp.call(
            "run_query",
            {"dataset_slug": settings.honeycomb_dataset, "query_spec": {"breakdowns": ["name"]}},
        )
        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "group", "group": {"name": "true"}}},
        )

    assert server.bubbleup_selections[-1]["group"] == {"name": "true"}
    assert result.coerced == ()


async def test_a_column_absent_from_the_schema_is_untouched(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_dataset_columns", {"dataset_slug": settings.honeycomb_dataset})
        rq = await mcp.call("run_query", {"dataset_slug": settings.honeycomb_dataset})
        result = await mcp.call(
            "run_bubbleup",
            {
                "query_pk": rq.query_id,
                "selection": {"type": "group", "group": {"unmapped.col": "true"}},
            },
        )

    assert server.bubbleup_selections[-1]["group"] == {"unmapped.col": "true"}
    assert result.coerced == ()


async def test_a_value_that_is_not_true_or_false_is_untouched(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_dataset_columns", {"dataset_slug": settings.honeycomb_dataset})
        rq = await mcp.call(
            "run_query",
            {"dataset_slug": settings.honeycomb_dataset, "query_spec": {"breakdowns": ["error"]}},
        )
        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "group", "group": {"error": "yes"}}},
        )

    assert server.bubbleup_selections[-1]["group"] == {"error": "yes"}
    assert result.coerced == ()


async def test_a_value_that_does_not_parse_as_the_columns_type_is_untouched(
    settings: Settings,
) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_dataset_columns", {"dataset_slug": settings.honeycomb_dataset})
        rq = await mcp.call(
            "run_query",
            {
                "dataset_slug": settings.honeycomb_dataset,
                "query_spec": {"breakdowns": ["cart.size"]},
            },
        )
        result = await mcp.call(
            "run_bubbleup",
            {
                "query_pk": rq.query_id,
                "selection": {"type": "group", "group": {"cart.size": "eight"}},
            },
        )

    assert server.bubbleup_selections[-1]["group"] == {"cart.size": "eight"}
    assert result.coerced == ()


async def test_no_conversion_without_a_prior_get_dataset_columns_call(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        rq = await mcp.call(
            "run_query",
            {"dataset_slug": settings.honeycomb_dataset, "query_spec": {"breakdowns": ["error"]}},
        )
        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "group", "group": {"error": "true"}}},
        )

    assert result.coerced == ()
    assert server.bubbleup_selections[-1]["group"] == {"error": "true"}


async def test_column_types_are_cleared_on_enter(settings: Settings) -> None:
    """Like `_produced_ids`, a re-entered client starts with no schema
    carried over from a prior session."""

    def seed_with_a_leftover_type(mcp: HoneycombMCP) -> None:
        mcp.column_types["leftover"] = "string"

    async with in_process_mcp(settings, pre_enter=seed_with_a_leftover_type) as (mcp, _):
        assert mcp.column_types == {}


# --------------------------------------------------------------------------
# EDW-1362: the retry hint on "failed to calculate group indices"
# --------------------------------------------------------------------------


async def test_the_hint_names_the_type_and_the_missing_breakdown(settings: Settings) -> None:
    """A correctly-typed value against a query with no breakdowns still
    fails; the hint should say both what the type is and that the source
    query never broke down on the group column."""
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_dataset_columns", {"dataset_slug": settings.honeycomb_dataset})
        rq = await mcp.call("run_query", {"dataset_slug": settings.honeycomb_dataset})
        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "group", "group": {"error": "true"}}},
        )

    assert result.is_error is True
    assert result.hinted is True
    # The client already retyped "true" to true, so the model is not told
    # to fix a type it did not get wrong: only the breakdown is named.
    assert "so send true" not in result.text
    assert f"query {rq.query_id} breaks down on []" in result.text
    assert "\u2014" not in result.text


async def test_the_hint_names_the_type_only_for_a_value_still_a_string(
    settings: Settings,
) -> None:
    """A value the coercion could not retype (`"yes"` on a boolean column) is
    the one the model has to fix, and the hint says which type it wants."""
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_dataset_columns", {"dataset_slug": settings.honeycomb_dataset})
        rq = await mcp.call(
            "run_query",
            {"dataset_slug": settings.honeycomb_dataset, "query_spec": {"breakdowns": ["error"]}},
        )
        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "group", "group": {"error": "yes"}}},
        )

    assert result.hinted is True
    assert 'error is boolean, so send true, not "true".' in result.text
    assert f"query {rq.query_id} breaks down on ['error']" in result.text


async def test_the_hint_gives_string_and_unmapped_columns_their_own_sentence(
    settings: Settings,
) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        await mcp.call("get_dataset_columns", {"dataset_slug": settings.honeycomb_dataset})
        rq = await mcp.call("run_query", {"dataset_slug": settings.honeycomb_dataset})
        result = await mcp.call(
            "run_bubbleup",
            {
                "query_pk": rq.query_id,
                "selection": {"type": "group", "group": {"name": "x", "unmapped.col": "1"}},
            },
        )

    assert result.hinted is True
    assert "so send" not in result.text
    assert "unmapped.col is not in the columns this session fetched" in result.text
    assert "name is" not in result.text


async def test_the_hint_follows_the_server_precedence_for_a_paging_call(
    settings: Settings,
) -> None:
    """With `bubbleup_result_id` given the server ignores `query_pk`, so the
    hint does not claim what `query_pk` broke down on."""
    async with in_process_mcp(settings) as (mcp, server):
        rq = await mcp.call(
            "run_query",
            {"dataset_slug": settings.honeycomb_dataset, "query_spec": {"breakdowns": ["error"]}},
        )
        first = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "group", "group": {"error": True}}},
        )
        assert first.is_error is False
        result_id = server.bubbleup_result_ids[-1]
        server.fail_next_bubbleup = True
        result = await mcp.call(
            "run_bubbleup",
            {
                "query_pk": rq.query_id,
                "bubbleup_result_id": result_id,
                "selection": {"type": "group", "group": {"error": True}},
            },
        )

    assert result.hinted is True
    assert f"pages into BubbleUp result {result_id}" in result.text
    assert "breaks down on" not in result.text


async def test_a_group_indices_error_on_a_non_group_selection_gets_no_hint(
    settings: Settings,
) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        rq = await mcp.call("run_query", {"dataset_slug": settings.honeycomb_dataset})
        server.fail_next_bubbleup = True
        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "2d", "column": "duration_ms"}},
        )

    assert result.is_error is True
    assert result.hinted is False
    assert result.text.endswith("failed to calculate group indices")


async def test_the_hint_is_generic_without_a_column_types_map(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        rq = await mcp.call("run_query", {"dataset_slug": settings.honeycomb_dataset})
        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "group", "group": {"error": "true"}}},
        )

    assert result.is_error is True
    assert result.hinted is True
    assert 'error was sent as the string "true"' in result.text
    assert "needs a JSON boolean" in result.text
    assert f"query {rq.query_id} breaks down on []" in result.text


async def test_a_hinted_error_still_carries_the_group_indices_text(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, server):
        rq = await mcp.call("run_query", {"dataset_slug": settings.honeycomb_dataset})
        result = await mcp.call(
            "run_bubbleup",
            {"query_pk": rq.query_id, "selection": {"type": "group", "group": {"error": "true"}}},
        )

    assert GROUP_INDICES_ERROR in result.text
    assert result.text.startswith("run_bubbleup failed:")


async def test_an_error_unrelated_to_group_indices_is_not_hinted(settings: Settings) -> None:
    async with in_process_mcp(settings) as (mcp, _):
        result = await mcp.call(
            "run_query",
            {"dataset_slug": settings.honeycomb_dataset, "query_spec": {"force_error": True}},
        )

    assert result.hinted is False


def test_meta_kwarg_serializes_to_wire_key_meta() -> None:
    """The SDK's own serialization: `meta=` on CallToolRequestParams lands at `_meta`."""
    params = types.CallToolRequestParams(
        name="get_trace",
        arguments={"trace_id": "abc"},
        meta={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
    )
    wire = params.model_dump(by_alias=True, exclude_none=True)
    assert wire["_meta"]["traceparent"] == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
