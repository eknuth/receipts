"""Tests for agent/providers/anthropic.py: the translation to and from the SDK.

No network. A fake client records the request and returns a canned response, so
the header, the cache breakpoints, and the block translation are all checked
against what would have gone on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agent.providers.anthropic import AnthropicProvider
from agent.providers.base import ToolResultBlock, ToolSchema, ToolUse, Turn
from receipts.settings import Settings


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: Any = None


@dataclass
class UsageStub:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_read_input_tokens: int = 7
    cache_creation_input_tokens: int = 3


@dataclass
class ResponseStub:
    content: list[Block]
    usage: UsageStub
    stop_reason: str = "tool_use"


class FakeMessages:
    def __init__(self, response: ResponseStub) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> ResponseStub:
        self.requests.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response: ResponseStub) -> None:
        self.messages = FakeMessages(response)


def provider(settings: Settings, response: ResponseStub | None = None) -> AnthropicProvider:
    response = response or ResponseStub(
        content=[
            Block(type="text", text="running the first query"),
            Block(type="tool_use", id="tu1", name="run_query", input={"dataset_slug": "x"}),
        ],
        usage=UsageStub(),
    )
    return AnthropicProvider(settings, client=FakeClient(response))


TOOLS = [ToolSchema(name="run_query", description="query", input_schema={"type": "object"})]


async def test_the_workspace_header_is_on_the_client(settings: Settings) -> None:
    """The key is identity linked, so a request without the header is rejected."""
    made = AnthropicProvider(settings)
    headers = made._client.default_headers  # type: ignore[attr-defined]
    assert headers["anthropic-workspace-id"] == settings.anthropic_workspace_id


async def test_the_model_comes_from_settings_and_can_be_overridden(settings: Settings) -> None:
    assert provider(settings).model == settings.anthropic_model
    over = AnthropicProvider(
        settings, model="claude-opus-5", client=FakeClient(ResponseStub([], UsageStub()))
    )
    assert over.model == "claude-opus-5"


async def test_text_and_tool_use_blocks_come_back_as_a_completion(settings: Settings) -> None:
    made = provider(settings)
    completion = await made.complete("system", [Turn(role="user", text="go")], TOOLS, max_tokens=99)

    assert completion.text == "running the first query"
    assert completion.tool_uses == [ToolUse(id="tu1", name="run_query", args={"dataset_slug": "x"})]
    assert completion.stop_reason == "tool_use"
    assert completion.usage.input_tokens == 100
    assert completion.usage.output_tokens == 20
    assert completion.usage.cache_read_tokens == 7
    assert completion.usage.cache_write_tokens == 3


async def test_a_tool_input_that_arrives_as_a_string_is_parsed(settings: Settings) -> None:
    """Escaping varies between models, so the input is parsed, never matched."""
    response = ResponseStub(
        content=[Block(type="tool_use", id="t", name="run_query", input='{"a": 1}')],
        usage=UsageStub(),
    )
    completion = await provider(settings, response).complete(
        "system", [Turn(role="user", text="go")], TOOLS, max_tokens=99
    )
    assert completion.tool_uses[0].args == {"a": 1}


async def test_the_system_prompt_carries_a_cache_breakpoint(settings: Settings) -> None:
    made = provider(settings)
    await made.complete("the method", [Turn(role="user", text="go")], TOOLS, max_tokens=99)

    request = made._client.messages.requests[0]  # type: ignore[attr-defined]
    assert request["system"][0]["text"] == "the method"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["model"] == settings.anthropic_model
    assert request["max_tokens"] == 99
    assert request["tools"][0]["name"] == "run_query"


async def test_the_transcript_becomes_messages_with_a_moving_breakpoint(
    settings: Settings,
) -> None:
    made = provider(settings)
    turns = [
        Turn(role="user", text="go"),
        Turn(role="assistant", text="thinking", tool_uses=[ToolUse("t1", "run_query", {"a": 1})]),
        Turn(role="user", tool_results=[ToolResultBlock(tool_use_id="t1", content="rows")]),
    ]
    await made.complete("system", turns, TOOLS, max_tokens=99)

    messages = made._client.messages.requests[0]["messages"]  # type: ignore[attr-defined]
    assert messages[0] == {"role": "user", "content": "go"}
    assert messages[1]["content"][0] == {"type": "text", "text": "thinking"}
    assert messages[1]["content"][1] == {
        "type": "tool_use",
        "id": "t1",
        "name": "run_query",
        "input": {"a": 1},
    }
    result = messages[2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "t1"
    assert result["cache_control"] == {"type": "ephemeral"}


async def test_a_provider_echoes_back_its_own_assistant_content(settings: Settings) -> None:
    """`raw` keeps anything the neutral transcript does not model."""
    made = provider(settings)
    raw = [Block(type="text", text="verbatim")]
    turns = [Turn(role="user", text="go"), Turn(role="assistant", text="ignored", raw=raw)]
    await made.complete("system", turns, TOOLS, max_tokens=99)

    messages = made._client.messages.requests[0]["messages"]  # type: ignore[attr-defined]
    assert messages[1]["content"] is raw


async def test_a_failed_tool_result_keeps_its_error_flag(settings: Settings) -> None:
    made = provider(settings)
    turns = [
        Turn(role="user", text="go"),
        Turn(
            role="user",
            tool_results=[ToolResultBlock(tool_use_id="t1", content="boom", is_error=True)],
        ),
    ]
    await made.complete("system", turns, TOOLS, max_tokens=99)

    messages = made._client.messages.requests[0]["messages"]  # type: ignore[attr-defined]
    assert messages[1]["content"][0]["is_error"] is True


def test_the_provider_satisfies_the_interface(settings: Settings) -> None:
    from agent.providers.base import Provider

    assert isinstance(provider(settings), Provider)


@pytest.mark.parametrize("field", ["name", "model"])
def test_the_provider_names_itself_for_the_report(settings: Settings, field: str) -> None:
    assert getattr(provider(settings), field)
