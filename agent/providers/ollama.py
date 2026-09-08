"""The Ollama provider: the native `/api/chat` endpoint behind the `Provider` interface.

Measured on this machine against `qwen3.8:27b` on `http://localhost:11434`
(2026-09-02): about 14 tokens/second generation, an 11 second cold load, and
a well-formed `run_query` tool call back with `think: false`. The expected
weak spot is prompt eval on a late-run 30 to 40k token context, which is why
the loop's wall budget for this provider defaults to 20 minutes instead of 8
(see `evals/run.py`).

Message shapes, read from Ollama's docs and checked against the live server:

  system  {"role": "system", "content": <the system prompt>}
  user    {"role": "user", "content": <text>}
  assistant with tool calls
          {"role": "assistant", "content": <text>,
           "tool_calls": [{"function": {"name": ..., "arguments": {...}}}]}
  tool result
          {"role": "tool", "tool_name": <name>, "content": <text>}

Ollama's native tool calls carry no id: a live call always came back with
`tool_calls[].function` only, no `id` field, so an assistant turn cannot echo
one back on its own. Instead, this module assigns `call_<n>_<8 hex>` (1-based within the
completion, with a random suffix so two completions never hand out the same
id) when it builds `ToolUse` objects out of a response, and rebuilds
the id-to-name mapping fresh on every request by walking the given `turns`
for the `ToolUse.id` / `.name` pairs already sitting on every assistant turn's
`tool_uses` list, regardless of who produced them or whether `raw` is set.
That is what turns a later `ToolResultBlock.tool_use_id` back into the
`tool_name` a `role: tool` message needs. No state lives on the provider
instance between calls, matching the "stateless" rule in
`agent/providers/base.py`.

Tool schemas translate to `{"type": "function", "function": {"name",
"description", "parameters"}}`, the same shape Ollama documents for
OpenAI-compatible function calling.

Malformed calls. A live smoke test had the model put `p99(duration_ms)` in
`run_query`'s `op` field instead of splitting `op` and `column`: still valid
JSON, still an object, just wrong content, and that is the MCP server's
schema to reject, not this module's. What this module does catch is the
shape failure below that: arguments that arrive as a JSON-encoded string are
decoded, same as `agent/providers/anthropic.py` does for the Anthropic API;
arguments that, after decoding, are not a JSON object at all (a list, a bare
number, a string that is not JSON) cannot be trusted as this call's input, so
the `ToolUse` comes back with `args={}` and `malformed=True`. That decision
lives in `agent/providers/_args.py`, shared with `agent/providers/nvidia.py`
(R15), so the two providers cannot quietly diverge on it. `agent/loop.py`
sends that call to the MCP server exactly like any other: the server's own
schema rejects the empty args the ordinary way, through the same tool-error
path a well-formed but invalid call already takes, and the loop counts the
flag onto `Report.malformed_calls` so the run does not have to lose a claim
just to see it happened.

Usage comes from `prompt_eval_count` and `eval_count`, the two counters
Ollama's response carries; there is no cache concept to report, so
`cache_read_tokens` and `cache_write_tokens` stay at `Usage`'s zero default.
`evals/pricing.py` has no entry for API cache accounting to apply here
either, since a local model is priced at zero regardless of cache.

`stop_reason` is not read from Ollama's own `done_reason` (that is "why
generation stopped", such as `stop` or `length`, not "did the model ask for
a tool"): the mapping the loop needs is `tool_use` when the response carries
any tool calls, `end_turn` otherwise.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx2

from agent.providers._args import decode_args as _decode_args
from agent.providers.base import Completion, ToolSchema, ToolUse, Turn, Usage
from receipts.settings import Settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 8192

# The read timeout is generous rather than tied to the loop's wall budget:
# `agent/loop.py` already wraps every `provider.complete()` call in
# `asyncio.timeout(remaining)` against the run's own budget, so this is only
# a backstop against a connection that never answers at all, not the thing
# that enforces the 20 minute default (see `evals/run.py`).
_READ_TIMEOUT_S = 1200.0


class OllamaProvider:
    """One async client against a local Ollama server, wrapped to the provider interface."""

    name = "ollama"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model: str | None = None,
        host: str | None = None,
        client: Any = None,
    ) -> None:
        settings = settings or Settings()
        self.model = model or settings.ollama_model
        self._host = (host or settings.ollama_host).rstrip("/")
        self._client = client or httpx2.AsyncClient(
            timeout=httpx2.Timeout(30.0, read=_READ_TIMEOUT_S)
        )

    async def complete(
        self,
        system: str,
        turns: list[Turn],
        tools: list[ToolSchema],
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Completion:
        payload = {
            "model": self.model,
            "messages": _to_messages(system, turns),
            "tools": [_to_tool_spec(tool) for tool in tools],
            "stream": False,
            "think": False,
            "options": {"num_predict": max_tokens},
        }
        response = await self._client.post(f"{self._host}/api/chat", json=payload)
        response.raise_for_status()
        return _to_completion(response.json())


def _tool_names(turns: list[Turn]) -> dict[str, str]:
    """Every `ToolUse.id` seen on an assistant turn, mapped to its tool name.

    Built fresh from the neutral transcript on every request rather than
    kept on the provider, per the module docstring: the ids a `role: tool`
    message needs to resolve back to `tool_name` are already sitting on the
    `ToolUse` objects the loop appended to `turns`, whoever produced them.
    """
    names: dict[str, str] = {}
    for turn in turns:
        for use in turn.tool_uses:
            names[use.id] = use.name
    return names


def _to_messages(system: str, turns: list[Turn]) -> list[dict[str, Any]]:
    id_to_name = _tool_names(turns)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for turn in turns:
        messages.extend(_to_ollama_messages(turn, id_to_name))
    return messages


def _to_ollama_messages(turn: Turn, id_to_name: dict[str, str]) -> list[dict[str, Any]]:
    if turn.role == "assistant":
        # `raw` is this provider's own message dict from a previous
        # response. Echoing it back unchanged keeps anything the neutral
        # shape does not model, the same convention `anthropic.py` follows.
        if turn.raw is not None:
            return [turn.raw]
        message: dict[str, Any] = {"role": "assistant", "content": turn.text or ""}
        if turn.tool_uses:
            message["tool_calls"] = [
                {"function": {"name": use.name, "arguments": use.args}} for use in turn.tool_uses
            ]
        return [message]

    if turn.tool_results:
        # One `role: tool` message per result. Ollama's native API has no
        # shape for bundling several tool results into one message the way
        # Anthropic's `tool_result` content blocks do.
        return [
            {
                "role": "tool",
                "tool_name": id_to_name.get(result.tool_use_id, ""),
                "content": result.content,
            }
            for result in turn.tool_results
        ]

    return [{"role": "user", "content": turn.text or ""}]


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
    """One `/api/chat` response, as a `Completion`."""
    message = data.get("message") or {}
    text = message.get("content") or ""
    tool_uses: list[ToolUse] = []
    # The suffix keeps ids unique across the whole transcript: `_tool_names`
    # maps every id it finds on every turn, and `call_1` alone would collide
    # between completions and label a tool result with the wrong tool name.
    suffix = uuid.uuid4().hex[:8]
    for index, call in enumerate(message.get("tool_calls") or [], start=1):
        function = call.get("function") or {}
        args, malformed = _decode_args(function.get("arguments"))
        tool_uses.append(
            ToolUse(
                id=f"call_{index}_{suffix}",
                name=function.get("name") or "",
                args=args,
                malformed=malformed,
            )
        )
    return Completion(
        text=text,
        tool_uses=tool_uses,
        usage=Usage(
            input_tokens=data.get("prompt_eval_count") or 0,
            output_tokens=data.get("eval_count") or 0,
        ),
        stop_reason="tool_use" if tool_uses else "end_turn",
        response_model=data.get("model"),
        response_id=None,
        raw_content=message,
    )
