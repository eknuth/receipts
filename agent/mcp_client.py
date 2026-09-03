"""Client for the hosted Honeycomb MCP.

Talks to `https://mcp.honeycomb.io/mcp` over streamable HTTP with a Bearer
management key. Read tools are allowed by default; write tools (board and
Canvas creation, R12) raise `ToolNotAllowed` unless the caller opts in with
`allow_write=True`.

Every tool result is reduced to compact text sized for a model, following
the convention in `hevy-mcp/src/tools.ts`: a `ToolResult` carries the raw
response, the compact text, the server's error flag, and any `query_id` or
`permalink` the tool call produced. Formatting lives in `agent/format.py`.

Calls are paced by a token bucket: at most 40 calls per rolling 60 second
window, at least 1.5 seconds between any two calls. A call over the cap
sleeps. It never raises for pacing reasons. The clock and sleep function are
injectable so a test can run 45 calls against a fake clock without a real
wait. A lock serializes concurrent waiters so the limit holds under
`asyncio.gather` as well as in a sequential loop.

`traceparent` and `tracestate` (W3C trace context) are injected into
`params._meta` on every `tools/call`. The pinned `mcp` package (2.x) puts
this on the wire via `CallToolRequestParams.meta`, aliased to `_meta`, and
the test suite proves it end to end with an in-process server. The OTel MCP
semantic conventions (SEP-414) say a client SHOULD write `traceparent` and
`tracestate` unprefixed into `params._meta` and a server SHOULD use it as
the remote parent; a live check on 2026-09-03 found no spans from
Honeycomb's hosted MCP in `receipts-demo` for either trace, so the hosted
MCP does not act on it today. The field goes out regardless, because it is
the standard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from agent import format as fmt
from receipts.settings import Settings

logger = logging.getLogger(__name__)

# Read tools, safe to call without any opt-in. The names follow
# https://docs.honeycomb.io/integrations/mcp/tools/ plus what the hosted
# server advertised on 2026-09-02 (`semconv`, `get_signals`). The server
# omits tools the team's plan does not include: `get_slos` on the Free plan,
# `get_service_map` below Enterprise. Being in this set only means a call is
# permitted; `list_tools` reports what the server actually serves.
READ_TOOLS: frozenset[str] = frozenset(
    {
        "get_workspace_context",
        "get_environment",
        "get_dataset",
        "get_dataset_columns",
        "find_columns",
        "find_queries",
        "run_query",
        "get_query_results",
        "run_bubbleup",
        "get_trace",
        "list_spans",
        "get_span_details",
        "get_signals",
        "get_slos",
        "get_triggers",
        "list_boards",
        "search_semconv",
        "semconv",
        "get_semconv_attribute",
        "list_semconv_namespaces",
        "list_aiconversations",
        "get_aiconversation",
        "canvas_agent_poll_response",
    }
)

# Write tools, R12 (boards and Canvas). Need the `mcp:write` key scope on
# the server side and `allow_write=True` here.
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_board",
        "update_board",
        "canvas_agent_invoke",
    }
)

# Tools that take a `dataset_slug` argument. `get_trace` looks a trace up by
# id across the environment and takes no `dataset_slug`, so it is not here.
# A call to any of these that omits `dataset_slug` or names a dataset other
# than `settings.honeycomb_dataset` is refused: this project's own telemetry
# lands in a second dataset (`receipts-investigator`, see agent/telemetry.py)
# in the same environment as the shop traffic, and an unscoped or
# cross-dataset query would let an investigation read a prior repeat's
# scenario id and grade off its own trace, which is the answer it is being
# graded on.
DATASET_SCOPED_TOOLS: frozenset[str] = frozenset(
    {
        "run_query",
        "run_bubbleup",
        "get_dataset_columns",
        "find_columns",
        "list_spans",
        "get_span_details",
    }
)

DEFAULT_RATE = 40
DEFAULT_PERIOD_S = 60.0
DEFAULT_MIN_INTERVAL_S = 1.5


class ToolNotAllowed(RuntimeError):
    """Raised when a tool outside the active allowlist is called.

    The read tools are always allowed. Write tools are allowed only when
    the client was constructed with `allow_write=True`. Any other name,
    including one the server does not advertise, is refused the same way:
    this check does not depend on `list_tools` having run first.
    """


@dataclass(frozen=True)
class ToolSpec:
    """One entry from `tools/list`, filtered to the active allowlist."""

    name: str
    description: str | None
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The result of one `tools/call`, reduced for a model to read.

    `raw` is the server's `structured_content` when it sends any, otherwise
    the joined text content. `text` is the compact rendering from
    `agent/format.py`. `is_error` mirrors the server's `isError` flag; the
    hosted MCP sets it for a bad dataset slug, a missing trace id, and the
    like, and `text` then carries the server's message. `query_id` and
    `permalink` are pulled out of the text when the tool result carries
    them (`run_query`, `run_bubbleup`, and `get_trace` do).
    """

    raw: Any
    text: str
    is_error: bool
    query_id: str | None
    permalink: str | None


ClockFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


class TokenBucket:
    """Paces calls to at most `rate` per `period` seconds, `min_interval` apart.

    Never raises: a call that would exceed either limit sleeps until it is
    back under both, then proceeds. `clock` and `sleep` are injectable as a
    matched pair so a test can simulate 60+ seconds of elapsed time without
    a real wait; production uses wall time (`time.monotonic`) and
    `asyncio.sleep`. An `asyncio.Lock` serializes waiters so concurrent
    callers cannot all pass the check at once.
    """

    def __init__(
        self,
        rate: int = DEFAULT_RATE,
        period: float = DEFAULT_PERIOD_S,
        min_interval: float = DEFAULT_MIN_INTERVAL_S,
        *,
        clock: ClockFn = time.monotonic,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._rate = rate
        self._period = period
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._call_times: list[float] = []
        self._last_call: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        """Block (by sleeping) until another call is allowed, then record it."""
        async with self._lock:
            if self._last_call is not None:
                since_last = self._clock() - self._last_call
                if since_last < self._min_interval:
                    gap = self._min_interval - since_last
                    logger.info("mcp pacing: waiting %.2fs for the minimum call interval", gap)
                    await self._sleep(gap)

            now = self._clock()
            cutoff = now - self._period
            self._call_times = [t for t in self._call_times if t > cutoff]
            if len(self._call_times) >= self._rate:
                gap = self._call_times[0] + self._period - now
                if gap > 0:
                    logger.info(
                        "mcp pacing: %d calls in the last %.0fs, waiting %.2fs",
                        len(self._call_times),
                        self._period,
                        gap,
                    )
                    await self._sleep(gap)

            now = self._clock()
            self._call_times.append(now)
            self._last_call = now


class HoneycombMCP:
    """Async context manager over one streamable-HTTP session to the hosted MCP."""

    READ_TOOLS = READ_TOOLS
    WRITE_TOOLS = WRITE_TOOLS

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        allow_write: bool = False,
        bucket: TokenBucket | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or Settings()
        self._allow_write = allow_write
        self._bucket = bucket or TokenBucket()
        self._http_client = http_client
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    def _allowed(self, name: str) -> bool:
        if name in READ_TOOLS:
            return True
        return self._allow_write and name in WRITE_TOOLS

    async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        """Open the transport and return its (read, write) streams.

        Split out so a test can subclass and hand back in-memory streams
        wired to an in-process server instead of the network.
        """
        http_client = self._http_client
        if http_client is None:
            key = self._settings.honeycomb_mcp_key.get_secret_value()
            http_client = httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {key}"},
                timeout=httpx2.Timeout(30.0, read=300.0),
            )
            await stack.enter_async_context(http_client)
        return await stack.enter_async_context(
            streamable_http_client(self._settings.honeycomb_mcp_url, http_client=http_client)
        )

    async def __aenter__(self) -> HoneycombMCP:
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await self._open_streams(stack)
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            # The SDK fetches tools/list on the first call of each tool name
            # to validate the result. Doing it once here, paced, keeps that
            # hidden request from slipping past the bucket mid-investigation.
            await self._bucket.wait()
            await session.list_tools()
        except BaseException:
            await stack.aclose()
            raise

        self._exit_stack = stack
        self._session = session
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._session = None

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("HoneycombMCP must be entered with 'async with' before use")
        return self._session

    async def list_tools(self) -> list[ToolSpec]:
        """List tools from the server, filtered to the active allowlist."""
        session = self._require_session()
        result = await session.list_tools()
        allowed = READ_TOOLS | (WRITE_TOOLS if self._allow_write else frozenset())
        return [
            ToolSpec(name=tool.name, description=tool.description, input_schema=tool.input_schema)
            for tool in result.tools
            if tool.name in allowed
        ]

    async def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        traceparent: str | None = None,
        tracestate: str | None = None,
    ) -> ToolResult:
        """Call one tool and return its compact result.

        Raises `ToolNotAllowed` before any network call if `name` is neither
        a read tool nor, with `allow_write=True`, a write tool, or if `name`
        is dataset-scoped and `args` names a dataset other than
        `settings.honeycomb_dataset`. A server-side error comes back as a
        `ToolResult` with `is_error=True` and the server's message in
        `text`, so the agent loop can show the model what went wrong and
        move on; the dataset check raises instead, because its message is
        instructive rather than diagnostic, and either way it becomes the
        tool result the model reads.
        """
        if not self._allowed(name):
            raise ToolNotAllowed(
                f"tool {name!r} is not allowed "
                f"(read tools are always allowed; write tools need allow_write=True)"
            )
        if name in DATASET_SCOPED_TOOLS:
            dataset = self._settings.honeycomb_dataset
            given = (args or {}).get("dataset_slug")
            if given != dataset:
                raise ToolNotAllowed(
                    f"{name} must be called with dataset_slug={dataset!r}; "
                    f"got {given!r}. This investigation is scoped to {dataset!r} only."
                )
        session = self._require_session()

        await self._bucket.wait()

        meta: dict[str, Any] = {}
        if traceparent:
            meta["traceparent"] = traceparent
        if tracestate:
            meta["tracestate"] = tracestate

        result = await session.call_tool(name, args or {}, meta=meta or None)

        text_parts = [
            block.text for block in result.content if getattr(block, "type", None) == "text"
        ]
        joined_text = "\n".join(text_parts)
        is_error = bool(getattr(result, "is_error", False))

        # The hosted Honeycomb MCP returns its read-tool results as
        # pre-formatted Markdown text: every tool captured in
        # tests/fixtures/mcp/ shows this. structured_content is carried
        # through in case the server ever sends it.
        payload: Any = (
            result.structured_content if result.structured_content is not None else joined_text
        )

        if is_error:
            text = fmt.format_error(name, joined_text)
            return ToolResult(raw=payload, text=text, is_error=True, query_id=None, permalink=None)

        query_id, permalink = fmt.extract_ids(joined_text)
        text = fmt.format_tool_result(name, payload, args=args)
        return ToolResult(
            raw=payload, text=text, is_error=False, query_id=query_id, permalink=permalink
        )


async def _run_cli(tool: str, args: dict[str, Any]) -> int:
    try:
        async with HoneycombMCP() as mcp:
            result = await mcp.call(tool, args)
    except ToolNotAllowed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except MCPError as exc:
        print(f"mcp error: {exc.error.message}", file=sys.stderr)
        return 1
    except httpx2.HTTPError as exc:
        print(f"http error: {exc}", file=sys.stderr)
        return 1
    if result.is_error:
        print(result.text, file=sys.stderr)
        return 1
    print(result.text)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m agent.mcp_client <tool> [json-args]", file=sys.stderr)
        return 2
    tool = argv[0]
    args: dict[str, Any] = {}
    if len(argv) > 1:
        try:
            args = json.loads(argv[1])
        except json.JSONDecodeError as exc:
            print(f"error: arguments must be a JSON object ({exc})", file=sys.stderr)
            return 2
        if not isinstance(args, dict):
            print("error: arguments must be a JSON object", file=sys.stderr)
            return 2
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(_run_cli(tool, args))


if __name__ == "__main__":
    raise SystemExit(main())
