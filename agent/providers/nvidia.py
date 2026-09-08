"""The NVIDIA provider: NVIDIA's hosted NIM endpoint, which speaks the OpenAI chat
completions protocol, behind the `Provider` interface.

The default model is `nvidia/nemotron-3-super-120b-a12b`: eight back-to-back
22k-token requests each come back 200 in about a second, and a tool probe
returns a well-formed `run_query` call. Two other models on the same endpoint
are not used. `moonshotai/kimi-k3` answers a short burst of requests (about
40k input tokens in a minute) with a 429 whose body is
`{"status":429,"title":"Too Many Requests"}` and no `Retry-After` header, and
keeps returning 429 to a 5-token request for over five minutes while other
models on the same key answer 200: a per-model quota window, not the roughly
40 requests/minute limit the endpoint documents. `deepseek-ai/deepseek-v4-pro-0813`
times out at 120 seconds on every request. `_post_with_retry` retries a 429
or a 5xx up to `_MAX_ATTEMPTS` times (six, sized to that five-minute quota
window: 5s, 10s, 20s, 40s,
60s, about 2.5 minutes total), doubling from `_INITIAL_BACKOFF_S` and capped
at `_BACKOFF_CAP_S`, honouring a `Retry-After` header in seconds when the
server sends one. A connection failure (`httpx2.TransportError`:
`ConnectError`, `ReadTimeout`, `RemoteProtocolError`) is retried the same
way. `evals/pricing.yml` prices `nvidia/nemotron-3-super-120b-a12b`,
`moonshotai/kimi-k3`, and `deepseek-ai/deepseek-v4-pro-0813` all at zero: the
build.nvidia.com developer endpoint is rate-limited rather than metered, and
publishes no per-token rate for `evals/pricing.py` to charge against usage
on any model there.

Message shapes are OpenAI's:

  system  {"role": "system", "content": <the system prompt>}
  user    {"role": "user", "content": <text>}
  assistant with tool calls
          {"role": "assistant", "content": <text or None>,
           "tool_calls": [{"id": ..., "type": "function",
                            "function": {"name": ..., "arguments": <JSON string>}}]}
  tool result
          {"role": "tool", "tool_call_id": <id>, "content": <text>}

Unlike Ollama's native API, OpenAI's shape keys a tool result on the call id
rather than the tool name, so there is no id-to-name map to rebuild on every
request: a `ToolResultBlock.tool_use_id` becomes `tool_call_id` directly.

Tool call ids come from the server, and their shape is not something to rely
on: kimi-k3 hands back ids shaped like `run_query:0`, which repeat across
separate completions in the same run rather than being unique for the life
of the conversation; `nvidia/nemotron-3-super-120b-a12b` hands back a
UUID-style id (`call-7d69eddc-...`), with nothing observed about whether it
ever repeats. Nothing in this module keys on an id across turns
either way (unlike `ollama.py`, which has to invent ids and track them
because its native API gives none at all): each request carries its own
tool_calls/tool-results pair in the right adjacency regardless of what the
ids say, and nothing downstream needs a repeated id disambiguated either.
`agent/report.py`'s `ToolCall` (the tool log) carries no id field at all,
`agent/validate.py` keys evidence on the MCP's own `query_id` instead,
`evals/grader.py` never reads a `ToolUse.id`, and
`agent/telemetry.py` writes `gen_ai.tool.call.id` as an attribute on each
`execute_tool` span rather than a key spans are grouped by, so two spans
carrying the same id stay two spans. A call that arrives with no id at all
(the shape docs allow it even if a live response has not shown it) falls
back to `call_<n>_<8 hex>`, the same scheme `ollama.py` uses, and that
fallback id is written back into the tool_calls entry in place before it
becomes `raw_content`, so an echoed assistant turn and the tool message that
answers it always carry the same id.

A live response can also carry `content: null` on an assistant message that
is all tool calls, and a `reasoning_content` field the neutral `Turn`/
`Completion` shape does not model at all; both kimi-k3 and
`nvidia/nemotron-3-super-120b-a12b` use this shape. Text
comes back as `""` for the former. For the latter, `raw` on the assistant
`Turn` is the whole response message dict, and echoing it back verbatim on
the next request (the same
convention `ollama.py` and `anthropic.py` follow) keeps `reasoning_content`
on the wire without this module parsing or interpreting it.

`function.arguments` is normally a JSON-encoded string; `agent/providers/
_args.py`'s `decode_args`, shared with `ollama.py`, is what decides whether it
can be trusted as an object. Anything else sets `malformed=True` with
`args={}`.

Usage comes from `usage.prompt_tokens` and `usage.completion_tokens`, missing
meaning zero. `usage.prompt_tokens_details.cached_tokens`, when present, is
this endpoint's one cache concept, and it fills `Usage.cache_read_tokens`;
there is no cache-write side to this API, so `cache_write_tokens` stays at
`Usage`'s zero default and `evals/pricing.yml` has nothing to price it
against regardless, since the model is priced at zero.

`stop_reason` is `tool_use` whenever the response carries any tool calls,
matching every other provider in this project, regardless of what
`finish_reason` says (a live response used `tool_calls` there, which would
otherwise need its own mapping). Otherwise `finish_reason` maps `stop` to
`end_turn`, `length` to `max_tokens`, and passes anything else through
unchanged.

`max_tokens` is clamped to `MAX_REQUEST_TOKENS` (16384): the live endpoint
rejects a request above that, and the loop's own `config.max_tokens` passes
straight through with no idea this ceiling exists.

A failure that reaches the caller, whether a status this module declined to
retry or the last attempt of one it did, is raised as `NvidiaAPIError` with
the status code and the first 500 characters of the response body in its
message: `agent/loop.py` records only `f"{type(exc).__name__}: {exc}"` on a
failed run, so the body has to be in that message or it never reaches a
run's `report.json` or `grade.json`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2

from agent.providers._args import decode_args
from agent.providers.base import Completion, ToolSchema, ToolUse, Turn, Usage
from receipts.settings import Settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 8192

# The live endpoint rejects a request whose max_tokens is above this; the
# loop's own budget (agent/loop.py's config.max_tokens) has no idea this
# ceiling exists, so the provider is what enforces it.
MAX_REQUEST_TOKENS = 16384

# Hosted rather than local: read timeout is far shorter than ollama.py's 1200s.
_CONNECT_TIMEOUT_S = 30.0
_READ_TIMEOUT_S = 300.0

# Retry policy, sized to a live smoke run that saw kimi-k3 hold a 429 for
# over five minutes on a per-model quota window: six attempts, exponential
# backoff from 5s doubling to a 60s cap (5, 10, 20, 40, 60s between them,
# about 2.5 minutes total), a Retry-After header (seconds) overriding the
# computed wait when present and finite.
_MAX_ATTEMPTS = 6
_INITIAL_BACKOFF_S = 5.0
_BACKOFF_CAP_S = 60.0

# Truncated into a raised error's message so a failed run's report.json and
# grade.json (which only keep f"{type(exc).__name__}: {exc}") carry some of
# what the server actually said, not just a status code.
_ERROR_BODY_CHARS = 500

Sleep = Callable[[float], Awaitable[None]]


class NvidiaAPIError(RuntimeError):
    """A `/chat/completions` call failed after retries, or was not retried at all.

    Carries the status code and the response body (truncated) in its
    message, since that is the only place either reaches a run's result
    files: `agent/loop.py` records a failed run as
    `f"{type(exc).__name__}: {exc}"`, nothing more.
    """


def _default_sleep(seconds: float) -> Awaitable[None]:
    return asyncio.sleep(seconds)


class NvidiaProvider:
    """One async client against NVIDIA's hosted NIM chat completions endpoint."""

    name = "nvidia"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model: str | None = None,
        base_url: str | None = None,
        client: Any = None,
        sleep: Sleep | None = None,
    ) -> None:
        settings = settings or Settings()
        if settings.nvidia_api_key is None:
            raise ValueError("NVIDIA_API_KEY is not set; add it to .env to use --provider nvidia")
        self.model = model or settings.nvidia_model
        self._base_url = (base_url or settings.nvidia_base_url).rstrip("/")
        self._sleep: Sleep = sleep or _default_sleep
        self._client = client or httpx2.AsyncClient(
            timeout=httpx2.Timeout(_CONNECT_TIMEOUT_S, read=_READ_TIMEOUT_S),
        )
        self._headers = {
            "Authorization": f"Bearer {settings.nvidia_api_key.get_secret_value()}",
            "Accept": "application/json",
        }

    def __repr__(self) -> str:
        """Names the model and base URL only: the bearer key never belongs in a log line."""
        return f"NvidiaProvider(model={self.model!r}, base_url={self._base_url!r})"

    async def complete(
        self,
        system: str,
        turns: list[Turn],
        tools: list[ToolSchema],
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Completion:
        tool_specs = [_to_tool_spec(tool) for tool in tools]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _to_messages(system, turns),
            "tools": tool_specs,
            "max_tokens": min(max_tokens, MAX_REQUEST_TOKENS),
            "stream": False,
        }
        if tool_specs:
            payload["tool_choice"] = "auto"
        response = await self._post_with_retry(payload)
        return _to_completion(response.json())

    async def _post_with_retry(self, payload: dict[str, Any]) -> Any:
        """POST `/chat/completions`, retrying 429s, 5xxs, and transport errors.

        A non-retryable status (a 4xx other than 429) and the last attempt of
        a retryable one both end here in `_raise_for_response`, which is what
        puts the response body in the raised message. A transport error
        (`httpx2.TransportError`) on the last attempt is re-raised as itself,
        since there is no response or body to carry.
        """
        url = f"{self._base_url}/chat/completions"
        backoff = _INITIAL_BACKOFF_S
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            last_attempt = attempt == _MAX_ATTEMPTS
            try:
                response = await self._client.post(url, json=payload, headers=self._headers)
            except httpx2.TransportError as exc:
                if last_attempt:
                    raise
                logger.warning(
                    "nvidia NIM request failed (%s: %s), retrying in %.1fs (attempt %d/%d)",
                    type(exc).__name__,
                    exc,
                    backoff,
                    attempt,
                    _MAX_ATTEMPTS,
                )
                await self._sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP_S)
                continue

            if response.status_code < 400:
                return response
            if _is_retryable(response.status_code) and not last_attempt:
                wait = _retry_wait(response, backoff)
                logger.warning(
                    "nvidia NIM returned %d, retrying in %.1fs (attempt %d/%d)",
                    response.status_code,
                    wait,
                    attempt,
                    _MAX_ATTEMPTS,
                )
                await self._sleep(wait)
                backoff = min(backoff * 2, _BACKOFF_CAP_S)
                continue
            _raise_for_response(response)
        # Unreachable: every iteration above either continues, returns, or raises.
        raise AssertionError("retry loop exited without returning")


def _is_retryable(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _raise_for_response(response: Any) -> None:
    """Raise `NvidiaAPIError` with the status and the response body.

    `response.raise_for_status()` alone gives a message with no body, and
    `agent/loop.py` records a failed run as `f"{type(exc).__name__}: {exc}"`,
    so the body has to be in this message or a run's result files never see it.
    """
    text = getattr(response, "text", "") or ""
    raise NvidiaAPIError(f"nvidia NIM returned {response.status_code}: {text[:_ERROR_BODY_CHARS]}")


def _retry_wait(response: Any, backoff: float) -> float:
    """The wait before the next attempt: `Retry-After` (seconds) if present, else backoff.

    A header value is clamped to `[0, _BACKOFF_CAP_S]` when it parses to a
    finite number: a live 429 body carried no `Retry-After` at all, but a
    negative or huge value from a future response should not shrink the wait
    below zero or blow past the cap, and `inf` is not a wait at all, so it
    falls back to the computed backoff exactly like an unparseable header.
    """
    headers = getattr(response, "headers", None) or {}
    retry_after = headers.get("Retry-After")
    if retry_after is None:
        return backoff
    try:
        wait = float(retry_after)
    except ValueError:
        return backoff
    if not math.isfinite(wait):
        return backoff
    return min(max(wait, 0.0), _BACKOFF_CAP_S)


def _to_messages(system: str, turns: list[Turn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for turn in turns:
        messages.extend(_to_openai_messages(turn))
    return messages


def _to_openai_messages(turn: Turn) -> list[dict[str, Any]]:
    if turn.role == "assistant":
        # `raw` is this provider's own message dict from a previous response.
        # Echoing it back unchanged keeps anything the neutral shape does not
        # model, such as `reasoning_content`, the same convention
        # `ollama.py` and `anthropic.py` follow.
        if turn.raw is not None:
            return [turn.raw]
        if turn.tool_uses:
            return [
                {
                    "role": "assistant",
                    "content": turn.text or None,
                    "tool_calls": [_to_tool_call(use) for use in turn.tool_uses],
                }
            ]
        return [{"role": "assistant", "content": turn.text or ""}]

    if turn.tool_results:
        # One `role: tool` message per result, keyed on the call id: OpenAI's
        # shape needs no name, unlike Ollama's `role: tool` message.
        return [
            {
                "role": "tool",
                "tool_call_id": result.tool_use_id,
                "content": result.content,
            }
            for result in turn.tool_results
        ]

    return [{"role": "user", "content": turn.text or ""}]


def _to_tool_call(use: ToolUse) -> dict[str, Any]:
    return {
        "id": use.id,
        "type": "function",
        "function": {"name": use.name, "arguments": json.dumps(use.args)},
    }


def _to_tool_spec(tool: ToolSchema) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _to_completion(data: dict[str, Any]) -> Completion:
    """One `/chat/completions` response, as a `Completion`."""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content") or ""
    finish_reason = choice.get("finish_reason")

    tool_uses: list[ToolUse] = []
    # The suffix keeps a fallback id unique across the whole transcript, the
    # same scheme `ollama.py` uses, for the rare case the server omits an id.
    suffix = uuid.uuid4().hex[:8]
    for index, call in enumerate(message.get("tool_calls") or [], start=1):
        function = call.get("function") or {}
        args, malformed = decode_args(function.get("arguments"))
        call_id = call.get("id")
        if not call_id:
            # `call` is the same dict object sitting inside `message`, which
            # becomes `raw_content` below. Writing the fallback id in place
            # here is what makes the id an echoed assistant turn carries
            # match the id its own tool result message is keyed on; leaving
            # `raw_content` untouched would echo a `tool_calls` entry with no
            # id at all next turn.
            call_id = f"call_{index}_{suffix}"
            call["id"] = call_id
        tool_uses.append(
            ToolUse(
                id=call_id,
                name=function.get("name") or "",
                args=args,
                malformed=malformed,
            )
        )

    usage = data.get("usage") or {}
    cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0

    return Completion(
        text=text,
        tool_uses=tool_uses,
        usage=Usage(
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
            cache_read_tokens=cached_tokens,
        ),
        stop_reason="tool_use" if tool_uses else _map_finish_reason(finish_reason),
        response_model=data.get("model"),
        response_id=data.get("id"),
        raw_content=message,
    )


def _map_finish_reason(finish_reason: str | None) -> str | None:
    if finish_reason == "stop":
        return "end_turn"
    if finish_reason == "length":
        return "max_tokens"
    return finish_reason
