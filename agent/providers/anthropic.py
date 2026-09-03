"""The Anthropic provider: the Messages API behind the `Provider` interface.

The key on this project is identity-linked, so every request carries the
`anthropic-workspace-id` header. It goes on the client as a default header
rather than per call, which means a request cannot be built that forgets it.

The model comes from `ANTHROPIC_MODEL` in `.env`. No `thinking` parameter is
sent: the configured model predates adaptive thinking, and the loop's own
structure is what keeps the investigation on method.

Two cache breakpoints per request. One sits on the system prompt, which caches
the tool schemas as well because tools render before the system block, and
those schemas are about 50 KB of JSON that would otherwise be re-read on every
turn. The second moves to the end of the transcript each turn, so the
conversation so far is read from cache instead of re-sent at full price. The
`Usage` this returns reports cache reads and writes separately, and
`evals/pricing.py` prices them apart.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from agent.providers.base import Completion, ToolSchema, ToolUse, Turn, Usage
from receipts.settings import Settings

DEFAULT_MAX_TOKENS = 8192


class AnthropicProvider:
    """One async Anthropic client, wrapped to the provider interface."""

    name = "anthropic"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model: str | None = None,
        client: Any = None,
    ) -> None:
        settings = settings or Settings()
        self.model = model or settings.anthropic_model
        self._client = client or anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            default_headers={"anthropic-workspace-id": settings.anthropic_workspace_id},
        )

    async def complete(
        self,
        system: str,
        turns: list[Turn],
        tools: list[ToolSchema],
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Completion:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ],
            messages=_to_messages(turns),
        )
        return _to_completion(response)


def _to_messages(turns: list[Turn]) -> list[dict[str, Any]]:
    """The neutral transcript as Anthropic messages, with a moving cache breakpoint."""
    messages = [_to_message(turn) for turn in turns]
    _mark_cache_breakpoint(messages)
    return messages


def _to_message(turn: Turn) -> dict[str, Any]:
    if turn.role == "assistant":
        # `raw` is this provider's own content list from a previous turn. Echoing
        # it back unchanged keeps anything the neutral shape does not model.
        if turn.raw is not None:
            return {"role": "assistant", "content": turn.raw}
        blocks: list[dict[str, Any]] = []
        if turn.text:
            blocks.append({"type": "text", "text": turn.text})
        blocks.extend(
            {"type": "tool_use", "id": use.id, "name": use.name, "input": use.args}
            for use in turn.tool_uses
        )
        return {"role": "assistant", "content": blocks}

    if turn.tool_results:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": result.tool_use_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
                for result in turn.tool_results
            ],
        }
    return {"role": "user", "content": turn.text or ""}


def _mark_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
    """Put a cache breakpoint on the last block of the last message, in place.

    Everything before it is read from cache on the next turn. Two things are
    skipped: a message whose content is a bare string, which has nothing to
    hang the marker on, and an echoed assistant turn, whose blocks are SDK
    objects rather than the dicts built here. The system breakpoint still
    covers the tools and the prompt in both cases.
    """
    for message in reversed(messages):
        content = message.get("content")
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            content[-1]["cache_control"] = {"type": "ephemeral"}
            return


def _to_completion(response: Any) -> Completion:
    """One Anthropic response as a `Completion`."""
    text_parts: list[str] = []
    tool_uses: list[ToolUse] = []
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(block.text)
        elif block_type == "tool_use":
            # Tool inputs come back as parsed objects; a stray string is
            # re-parsed rather than string-matched.
            args = block.input
            if isinstance(args, str):
                args = json.loads(args)
            tool_uses.append(ToolUse(id=block.id, name=block.name, args=dict(args or {})))

    usage = getattr(response, "usage", None)
    return Completion(
        text="\n".join(text_parts).strip(),
        tool_uses=tool_uses,
        usage=Usage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        ),
        stop_reason=getattr(response, "stop_reason", None),
        raw_content=list(response.content),
    )
