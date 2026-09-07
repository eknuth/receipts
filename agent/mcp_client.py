"""Client for the hosted Honeycomb MCP.

Talks to `https://mcp.honeycomb.io/mcp` over streamable HTTP, authenticated
either way `settings.honeycomb_auth` says: a Bearer management key (the
default, `"key"`) or an OAuth session built from `agent/auth.py` (`"oauth"`,
needed for Canvas; see `_open_streams`). Read tools are allowed by default;
write tools (board and Canvas creation, R12) raise `ToolNotAllowed` unless
the caller opts in with `allow_write=True`.

Tools that take a `dataset_slug` on the server (`DATASET_SCOPED_TOOLS`) are
refused unless it names `settings.honeycomb_dataset`, so an investigation
cannot read or query a different dataset in the same environment.
`run_query` is refused a second way too: `environment_wide_query=true` makes
the server query every dataset in the environment regardless of
`dataset_slug`, so it is refused outright. `run_bubbleup` has no
`dataset_slug` parameter at all, so it is scoped by provenance instead: it
is refused unless the id the server will key its lookup on (its
`bubbleup_result_id` when given, else its `query_pk`) is one this session
itself produced with a passing `run_query` or `run_bubbleup` call. See the
comment above `DATASET_SCOPED_TOOLS` for the history.

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
import re
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
from agent.auth import require_oauth_provider
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
# the server side and `allow_write=True` here. This is deliberately exactly
# the two tools R12 uses to write, not everything the key can now reach: the
# management key was widened to `mcp:write` on 2026-09-07 and the hosted
# server started serving `update_board`, `create_trigger`, `create_slo`,
# `create_recipient`, and `create_marker` alongside it. None of those five
# are here, and none are in READ_TOOLS either, so `_allowed` refuses every
# one of them regardless of `allow_write`: R12's scope is a board and a
# Canvas message, not triggers, SLOs, recipients, or markers, and a wider
# key must not silently widen what this client will call. `list_boards` and
# `canvas_agent_poll_response` are reads and live in READ_TOOLS instead.
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_board",
        "canvas_agent_invoke",
    }
)

# Tools that take a `dataset_slug` argument on the server. `get_trace` looks
# a trace up by id across the environment and takes no `dataset_slug`, so it
# is not here; neither is `run_bubbleup`, for the same reason (see below). A
# call to any of these that omits `dataset_slug` or names a dataset other
# than `settings.honeycomb_dataset` is refused: this project's own telemetry
# lands in a second dataset (`receipts-investigator`, see agent/telemetry.py)
# in the same environment as the shop traffic, and an unscoped or
# cross-dataset query would let an investigation read a prior repeat's
# scenario id and grade off its own trace, which is the answer it is being
# graded on.
DATASET_SCOPED_TOOLS: frozenset[str] = frozenset(
    {
        "run_query",
        "get_dataset_columns",
        "find_columns",
        "list_spans",
        "get_span_details",
    }
)

# `run_bubbleup` takes no `dataset_slug` at all: its inputs are `query_pk`,
# `selection`, `bubbleup_result_id`, `clause_name`, `items_per_page`,
# `max_columns`, `page`, and `team`, confirmed against the hosted schema.
# Before 2026-09-04 it was listed in DATASET_SCOPED_TOOLS above; since the
# server never sends a `dataset_slug` for it, the model never did either,
# and the guard refused every real `run_bubbleup` call since the guard
# landed on 2026-09-03, 32 of 32 in the stored runs. It is scoped by
# provenance instead: a `run_bubbleup` call is allowed only when the id the
# server will actually key its lookup on names something this session
# itself received from a `run_query` or `run_bubbleup` call that passed the
# dataset guard above. The live schema gives `bubbleup_result_id` priority
# over `query_pk` when both are present (paging into an existing analysis
# ignores which query built it), so the check does too: `bubbleup_result_id`
# must be in that set when it is given at all, and only otherwise does
# `query_pk` have to be. `HoneycombMCP` tracks the set in `_produced_ids`. A
# stray `dataset_slug` the model adds anyway is stripped before the call
# goes out, since the server does not define that parameter.

DEFAULT_RATE = 40
DEFAULT_PERIOD_S = 60.0
DEFAULT_MIN_INTERVAL_S = 1.5

# The hosted MCP's own error text for a `run_bubbleup` group selection whose
# value did not match its column's type, or whose source query never broke
# down on the group column. Neither cause is named in the message itself.
GROUP_INDICES_ERROR = "failed to calculate group indices"

# What a correctly-typed value looks like, for the retry hint. Only the
# types a string can be mistaken for: a string column never causes this
# error by carrying a string.
_TYPE_EXAMPLE: dict[str, str] = {
    "boolean": 'true, not "true"',
    "integer": '8, not "8"',
    "float": '1.5, not "1.5"',
}

# JSON numeric literals, which is what a retyped group value has to be.
_INT_LITERAL = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_FLOAT_LITERAL = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?$")

# What a wire value of each retypable column type looks like once it is
# right, for the hint to tell a typed value from a string one.
_TYPE_MATCHES: dict[str, tuple[type, ...]] = {
    "boolean": (bool,),
    "integer": (int,),
    "float": (int, float),
}


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
    them (`run_query`, `run_bubbleup`, and `get_trace` do). `coerced` names
    the argument paths a `run_bubbleup` group selection was retyped on
    (e.g. `selection.group.error`) from the dataset's column schema, empty
    for every other call and for a call that needed no retyping. `hinted`
    is set when a `run_bubbleup` "failed to calculate group indices" error
    text carries an added retry hint.
    """

    raw: Any
    text: str
    is_error: bool
    query_id: str | None
    permalink: str | None
    coerced: tuple[str, ...] = ()
    hinted: bool = False


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
        # Ids this session has produced against the configured dataset: a
        # `run_query`'s `query_id`, plus a `run_bubbleup`'s own id and any
        # `bubbleup_result_id` it returns. `run_bubbleup` is scoped against
        # this set instead of a `dataset_slug` parameter it does not have.
        self._produced_ids: set[str] = set()
        # Column name to lowercased type ("boolean", "integer", "float",
        # "string"), populated from every successful `get_dataset_columns`
        # result this session sees (merged by union across pages). Used to
        # retype a `run_bubbleup` group selection's values against the
        # dataset's own schema. Empty until a `get_dataset_columns` call
        # succeeds.
        self.column_types: dict[str, str] = {}
        # A `run_query`'s `breakdowns`, keyed by the `query_id` it returned.
        # Read by the `run_bubbleup` retry hint: a group selection only
        # works when the source query already broke down on the group
        # column, a fact the server's own error text does not mention.
        self._breakdowns_by_id: dict[str, list[str]] = {}

    def _allowed(self, name: str) -> bool:
        if name in READ_TOOLS:
            return True
        return self._allow_write and name in WRITE_TOOLS

    async def _build_http_client(self) -> httpx2.AsyncClient:
        """The httpx2 client `_open_streams` wraps with the streamable-HTTP
        transport, picked by `settings.honeycomb_auth`.

        `"key"` (the default) sends the management key as a Bearer header,
        unchanged since before R12. `"oauth"` builds an `OAuthClientProvider`
        from `agent.auth.require_oauth_provider`, which raises
        `OAuthNotAuthorized` naming the login command rather than opening a
        browser here. Canvas (R12, EDW-1334) needs OAuth; a management key
        has no user actor and `canvas_agent_invoke` fails with
        `actor_user_hcid is required` under one, verified live on
        2026-09-07. Split out from `_open_streams` so a test can build and
        inspect one without opening a real transport.
        """
        if self._settings.honeycomb_auth == "oauth":
            provider = await require_oauth_provider(self._settings)
            return httpx2.AsyncClient(auth=provider, timeout=httpx2.Timeout(30.0, read=300.0))
        key = self._settings.honeycomb_mcp_key.get_secret_value()
        return httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx2.Timeout(30.0, read=300.0),
        )

    async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        """Open the transport and return its (read, write) streams.

        Split out so a test can subclass and hand back in-memory streams
        wired to an in-process server instead of the network.
        """
        http_client = self._http_client
        if http_client is None:
            http_client = await self._build_http_client()
            await stack.enter_async_context(http_client)
        return await stack.enter_async_context(
            streamable_http_client(self._settings.honeycomb_mcp_url, http_client=http_client)
        )

    async def __aenter__(self) -> HoneycombMCP:
        # A re-entered client (a fresh `async with` on the same instance)
        # starts with an empty provenance set: an id from a prior session
        # should not authorize a `run_bubbleup` in this one, and its column
        # types and breakdowns should not carry over either.
        self._produced_ids = set()
        self.column_types = {}
        self._breakdowns_by_id = {}
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
        a read tool nor, with `allow_write=True`, a write tool; if `name` is
        dataset-scoped and `args` names a dataset other than
        `settings.honeycomb_dataset`; if `name` is `run_query` and `args`
        sets a truthy `environment_wide_query` (which queries every dataset
        in the environment regardless of `dataset_slug`); or if `name` is
        `run_bubbleup` and the id the server will key its lookup on (its
        `bubbleup_result_id` when given, else its `query_pk`) is not one
        this session itself produced with a `run_query` or `run_bubbleup`
        call that passed the dataset guard (see `DATASET_SCOPED_TOOLS` and
        the comment above it). A server-side error comes back as a
        `ToolResult` with `is_error=True` and the server's message in
        `text`, so the agent loop can show the model what went wrong and
        move on; these checks raise instead, because their messages are
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
        if name == "run_query" and (args or {}).get("environment_wide_query"):
            raise ToolNotAllowed(
                "run_query must not be called with environment_wide_query=true: it queries "
                f"every dataset in the environment. This investigation is scoped to "
                f"{self._settings.honeycomb_dataset!r} only."
            )

        call_args = dict(args or {})
        coerced: tuple[str, ...] = ()
        if name == "run_bubbleup":
            call_args = self._check_bubbleup_provenance(call_args)
            call_args, coerced = self._coerce_bubbleup_group(call_args)

        session = self._require_session()

        await self._bucket.wait()

        meta: dict[str, Any] = {}
        if traceparent:
            meta["traceparent"] = traceparent
        if tracestate:
            meta["tracestate"] = tracestate

        result = await session.call_tool(name, call_args, meta=meta or None)

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
            hinted = False
            if name == "run_bubbleup" and GROUP_INDICES_ERROR in joined_text:
                hint = self._bubbleup_hint(call_args)
                if hint:
                    text = f"{text} {hint}"
                    hinted = True
            return ToolResult(
                raw=payload,
                text=text,
                is_error=True,
                query_id=None,
                permalink=None,
                coerced=coerced,
                hinted=hinted,
            )

        query_id, permalink = fmt.extract_ids(joined_text)
        self._record_produced_ids(name, joined_text, query_id)
        if name == "run_query":
            self._record_breakdowns(query_id, call_args)
        elif name == "get_dataset_columns":
            self._record_column_types(joined_text)
        text = fmt.format_tool_result(name, payload, args=call_args)
        return ToolResult(
            raw=payload,
            text=text,
            is_error=False,
            query_id=query_id,
            permalink=permalink,
            coerced=coerced,
        )

    def _check_bubbleup_provenance(self, args: dict[str, Any]) -> dict[str, Any]:
        """Strip a stray `dataset_slug` and check `run_bubbleup`'s provenance.

        `run_bubbleup` has no `dataset_slug` parameter on the server, so a
        value the model sends anyway is dropped rather than forwarded.

        The live schema gives `bubbleup_result_id` precedence over
        `query_pk` when both are present: the server pages into the named
        BubbleUp result and ignores `query_pk` entirely. A guard that
        allowed either id to be valid on its own would let a known
        `query_pk` vouch for a foreign `bubbleup_result_id` that the server
        then actually uses, so the check follows the same precedence: when
        `bubbleup_result_id` is given at all, it alone must be in
        `_produced_ids`; only when it is absent does `query_pk` have to be.
        """
        if "dataset_slug" in args:
            logger.debug(
                "run_bubbleup: dropping dataset_slug=%r, the server takes no such parameter",
                args["dataset_slug"],
            )
            args = {k: v for k, v in args.items() if k != "dataset_slug"}

        bubbleup_result_id = args.get("bubbleup_result_id")
        if bubbleup_result_id:
            if bubbleup_result_id in self._produced_ids:
                return args
            dataset = self._settings.honeycomb_dataset
            raise ToolNotAllowed(
                f"run_bubbleup bubbleup_result_id={bubbleup_result_id!r} names a result id "
                f"that was not produced by a BubbleUp in this session (on {dataset!r}). "
                f"Page an existing BubbleUp only using a bubbleup_result_id this session "
                f"produced."
            )

        query_pk = args.get("query_pk")
        if query_pk in self._produced_ids:
            return args

        dataset = self._settings.honeycomb_dataset
        raise ToolNotAllowed(
            f"run_bubbleup query_pk={query_pk!r} names an id that was not produced by a "
            f"run_query on {dataset!r} in this session. Call run_query first and build "
            f"run_bubbleup on its query_id."
        )

    def _record_produced_ids(self, name: str, text: str, query_id: str | None) -> None:
        """Track ids this session produced, for `run_bubbleup`'s provenance check.

        `query_id` is already `fmt.extract_ids`'s first match, which for a
        `run_query` result is its `query_run_pk`. A `run_bubbleup` result's
        own id is `fmt.extract_bubbleup_result_id`, parsed from
        `bubble_up_url` rather than through `extract_ids`, since the live
        server does not send it as a plain Metadata key (see that
        function's docstring) and `query_run_pk` takes priority in
        `extract_ids` regardless.
        """
        if name == "run_query":
            if query_id:
                self._produced_ids.add(query_id)
        elif name == "run_bubbleup":
            if query_id:
                self._produced_ids.add(query_id)
            bubbleup_result_id = fmt.extract_bubbleup_result_id(text)
            if bubbleup_result_id:
                self._produced_ids.add(bubbleup_result_id)

    def _record_breakdowns(self, query_id: str | None, call_args: dict[str, Any]) -> None:
        """Remember what a `run_query` broke down on, keyed by its `query_id`.

        Read by `_bubbleup_hint`: a group selection only works when the
        query it pages into already broke down on the group column, and the
        server's own error text does not say so.
        """
        if not query_id:
            return
        spec = call_args.get("query_spec") or {}
        breakdowns = spec.get("breakdowns") or []
        self._breakdowns_by_id[query_id] = [str(column) for column in breakdowns]

    def _record_column_types(self, text: str) -> None:
        """Merge a `get_dataset_columns` result's types into `column_types`.

        By union rather than replacement: the tool pages, and a later page
        should not lose the columns an earlier page already named.
        """
        parsed = fmt.parse_column_types(text)
        if parsed:
            self.column_types.update(parsed)

    def _coerce_bubbleup_group(
        self, args: dict[str, Any]
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Retype a `run_bubbleup` group selection's values from the column schema.

        The hosted MCP rejects a group selection whose value is a string
        against a boolean or numeric column (`{"error": "true"}`) with
        `failed to calculate group indices`, and only accepts the column's
        own JSON type (`{"error": true}`). This converts each group value
        whose column is known (from a prior `get_dataset_columns`) and whose
        string value parses as that column's type: `boolean` from a string
        equal to `true`/`false` in any case, `integer` and `float` from a
        string that parses as one. Any other value, any other type, and any
        column not in `column_types` is left alone; the model's own value
        goes out unchanged. Returns the (possibly rewritten) args and the
        `selection.group.<column>` paths that were converted, in selection
        order, for `ToolResult.coerced`.
        """
        selection = args.get("selection")
        if not isinstance(selection, dict):
            return args, ()
        group = selection.get("group")
        if not isinstance(group, dict):
            return args, ()

        new_group = dict(group)
        coerced: list[str] = []
        for column, value in group.items():
            if not isinstance(value, str):
                continue
            column_type = self.column_types.get(column)
            if column_type == "boolean" and value.lower() in ("true", "false"):
                new_group[column] = value.lower() == "true"
            elif column_type == "integer" and _parses_as(int, value):
                new_group[column] = int(value)
            elif column_type == "float" and _parses_as(float, value):
                new_group[column] = float(value)
            else:
                continue
            coerced.append(f"selection.group.{column}")

        if not coerced:
            return args, ()

        logger.debug("run_bubbleup: retyped %s from the column schema", coerced)
        new_args = dict(args)
        new_args["selection"] = {**selection, "group": new_group}
        return new_args, tuple(coerced)

    def _bubbleup_hint(self, call_args: dict[str, Any]) -> str | None:
        """The sentences added to a `failed to calculate group indices` error.

        Two facts the server's own message leaves out: what JSON type each
        group column wants, and whether the query it pages into broke down
        on that column at all. Both have to hold for a group selection to
        work; a live check on 2026-09-04 found every stored call with a
        correctly-typed value against a query with no breakdowns still
        failed the same way, and every one against a query that broke down
        on the group column succeeded.

        Reads the args as they went on the wire, after `_coerce_bubbleup_group`,
        so a value the client already retyped is not sent back to the model
        as a mistake to fix. Only the values that are still strings against
        a boolean or numeric column get the type sentence. None for a call
        with no group selection, where neither fact applies.
        """
        selection = call_args.get("selection")
        group = selection.get("group") if isinstance(selection, dict) else None
        if not isinstance(group, dict):
            return None

        sentences: list[str] = []
        for column, value in group.items():
            column_type = self.column_types.get(column)
            if column_type is None:
                if self.column_types:
                    sentences.append(
                        f"{column} is not in the columns this session fetched, so its type "
                        "is unknown here."
                    )
                elif isinstance(value, str):
                    sentences.append(
                        f'{column} was sent as the string "{value}"; a boolean column needs a '
                        "JSON boolean and a numeric column needs a number."
                    )
                continue
            wanted = _TYPE_MATCHES.get(column_type)
            if wanted is None:
                continue
            if isinstance(value, bool) and column_type != "boolean":
                typed = False
            else:
                typed = isinstance(value, wanted)
            if not typed:
                sentences.append(
                    f"{column} is {column_type}, so send {_TYPE_EXAMPLE[column_type]}."
                )

        # The server keys its lookup on bubbleup_result_id when it is given,
        # the same precedence `_check_bubbleup_provenance` follows.
        result_id = call_args.get("bubbleup_result_id")
        query_pk = call_args.get("query_pk")
        if result_id:
            sentences.append(
                f"This call pages into BubbleUp result {result_id}; a group selection needs "
                "the group column in the breakdowns of the run_query that result was built on."
            )
        elif query_pk in self._breakdowns_by_id:
            sentences.append(
                f"query {query_pk} breaks down on {self._breakdowns_by_id[query_pk]} and a "
                "group selection needs the group column in the source query's breakdowns."
            )
        else:
            sentences.append(
                "A group selection needs the group column in the source query's breakdowns."
            )
        return " ".join(sentences)


def _parses_as(kind: type, value: str) -> bool:
    """True when `value` is a plain JSON numeric literal of `kind` (`int` or `float`).

    A regex rather than `int()`/`float()` on purpose. Python's parsers take
    `"1_000"`, `" 8 "`, `"+8"`, non-ASCII digits, and `"nan"`, and a `nan`
    goes on the wire as `null`, a value the model never wrote. What the
    server wants is the JSON grammar, so that is what is accepted.
    """
    pattern = _INT_LITERAL if kind is int else _FLOAT_LITERAL
    return pattern.match(value) is not None


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
