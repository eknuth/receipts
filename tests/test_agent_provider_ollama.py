"""Tests for agent/providers/ollama.py: the translation to and from Ollama's native /api/chat.

No network. A fake httpx2 client records the request and returns a canned
response, so the message shapes, the id-to-name mapping, and the malformed-
call detection are all checked against what would have gone on the wire.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.providers.base import ToolResultBlock, ToolSchema, ToolUse, Turn
from agent.providers.ollama import OllamaProvider
from receipts.settings import Settings

TOOLS = [ToolSchema(name="run_query", description="query", input_schema={"type": "object"})]


class FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._data


class FakeClient:
    """Records every `/api/chat` request and replays the next canned response."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> FakeResponse:
        self.requests.append({"url": url, "json": json})
        data = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return FakeResponse(data)


def response(
    *,
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_eval_count: int = 0,
    eval_count: int = 0,
    model: str = "qwen3.8:27b",
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "model": model,
        "message": message,
        "done": True,
        "prompt_eval_count": prompt_eval_count,
        "eval_count": eval_count,
    }


def provider(settings: Settings, *responses: dict[str, Any]) -> OllamaProvider:
    made = list(responses) or [response(content="ok")]
    return OllamaProvider(settings, client=FakeClient(made))


# --------------------------------------------------------------------------
# Construction and defaults
# --------------------------------------------------------------------------


def test_the_model_and_host_come_from_settings(settings: Settings) -> None:
    made = OllamaProvider(settings, client=FakeClient([response()]))
    assert made.model == settings.ollama_model
    assert made._host == settings.ollama_host.rstrip("/")  # type: ignore[attr-defined]


def test_the_model_and_host_can_be_overridden(settings: Settings) -> None:
    made = OllamaProvider(
        settings, model="other-model", host="http://elsewhere:1234", client=FakeClient([response()])
    )
    assert made.model == "other-model"
    assert made._host == "http://elsewhere:1234"  # type: ignore[attr-defined]


def test_the_provider_satisfies_the_interface(settings: Settings) -> None:
    from agent.providers.base import Provider

    assert isinstance(provider(settings), Provider)


@pytest.mark.parametrize("field", ["name", "model"])
def test_the_provider_names_itself_for_the_report(settings: Settings, field: str) -> None:
    assert getattr(provider(settings), field)


def test_the_provider_name_is_ollama(settings: Settings) -> None:
    assert provider(settings).name == "ollama"


# --------------------------------------------------------------------------
# The request: system, tools, stream/think, max_tokens
# --------------------------------------------------------------------------


async def test_the_request_carries_system_stream_and_think(settings: Settings) -> None:
    made = provider(settings, response(content="done"))
    await made.complete("the method", [Turn(role="user", text="go")], TOOLS, max_tokens=99)

    payload = made._client.requests[0]["json"]  # type: ignore[attr-defined]
    assert payload["model"] == settings.ollama_model
    assert payload["stream"] is False
    assert payload["think"] is False
    assert payload["options"]["num_predict"] == 99
    assert payload["messages"][0] == {"role": "system", "content": "the method"}
    assert payload["messages"][1] == {"role": "user", "content": "go"}


async def test_tool_schemas_translate_to_the_function_shape(settings: Settings) -> None:
    made = provider(settings, response(content="done"))
    await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)

    payload = made._client.requests[0]["json"]  # type: ignore[attr-defined]
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "run_query",
                "description": "query",
                "parameters": {"type": "object"},
            },
        }
    ]


# --------------------------------------------------------------------------
# Assistant turns and tool results translate both ways
# --------------------------------------------------------------------------


async def test_an_assistant_turn_with_tool_uses_becomes_tool_calls(settings: Settings) -> None:
    made = provider(settings, response(content="done"))
    turns = [
        Turn(role="user", text="go"),
        Turn(
            role="assistant",
            text="running a query",
            tool_uses=[ToolUse(id="call_1", name="run_query", args={"a": 1})],
        ),
    ]
    await made.complete("sys", turns, TOOLS, max_tokens=99)

    messages = made._client.requests[0]["json"]["messages"]  # type: ignore[attr-defined]
    assert messages[2] == {
        "role": "assistant",
        "content": "running a query",
        "tool_calls": [{"function": {"name": "run_query", "arguments": {"a": 1}}}],
    }


async def test_a_tool_result_becomes_a_role_tool_message_with_the_right_name(
    settings: Settings,
) -> None:
    """The generated id from an earlier assistant turn resolves back to its tool name."""
    made = provider(settings, response(content="done"))
    turns = [
        Turn(role="user", text="go"),
        Turn(
            role="assistant",
            tool_uses=[ToolUse(id="call_1", name="run_query", args={"a": 1})],
        ),
        Turn(
            role="user",
            tool_results=[ToolResultBlock(tool_use_id="call_1", content="17 rows")],
        ),
    ]
    await made.complete("sys", turns, TOOLS, max_tokens=99)

    messages = made._client.requests[0]["json"]["messages"]  # type: ignore[attr-defined]
    assert messages[3] == {"role": "tool", "tool_name": "run_query", "content": "17 rows"}


async def test_two_tool_results_in_one_turn_become_two_messages(settings: Settings) -> None:
    made = provider(settings, response(content="done"))
    turns = [
        Turn(role="user", text="go"),
        Turn(
            role="assistant",
            tool_uses=[
                ToolUse(id="call_1", name="run_query", args={}),
                ToolUse(id="call_2", name="get_trace", args={}),
            ],
        ),
        Turn(
            role="user",
            tool_results=[
                ToolResultBlock(tool_use_id="call_1", content="rows"),
                ToolResultBlock(tool_use_id="call_2", content="trace"),
            ],
        ),
    ]
    await made.complete("sys", turns, TOOLS, max_tokens=99)

    messages = made._client.requests[0]["json"]["messages"]  # type: ignore[attr-defined]
    assert messages[3] == {"role": "tool", "tool_name": "run_query", "content": "rows"}
    assert messages[4] == {"role": "tool", "tool_name": "get_trace", "content": "trace"}


async def test_a_provider_echoes_back_its_own_assistant_message(settings: Settings) -> None:
    """`raw` keeps anything the neutral transcript does not model."""
    made = provider(settings, response(content="done"))
    raw = {"role": "assistant", "content": "verbatim", "thinking": "scratch"}
    turns = [Turn(role="user", text="go"), Turn(role="assistant", text="ignored", raw=raw)]
    await made.complete("sys", turns, TOOLS, max_tokens=99)

    messages = made._client.requests[0]["json"]["messages"]  # type: ignore[attr-defined]
    assert messages[2] is raw


# --------------------------------------------------------------------------
# The response: text, tool_calls, usage, stop_reason
# --------------------------------------------------------------------------


async def test_text_and_tool_calls_come_back_as_a_completion(settings: Settings) -> None:
    made = provider(
        settings,
        response(
            content="running the first query",
            tool_calls=[{"function": {"name": "run_query", "arguments": {"dataset_slug": "x"}}}],
            prompt_eval_count=1234,
            eval_count=56,
        ),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)

    assert completion.text == "running the first query"
    (use,) = completion.tool_uses
    assert use.id.startswith("call_1_") and len(use.id) == len("call_1_") + 8
    assert (use.name, use.args, use.malformed) == ("run_query", {"dataset_slug": "x"}, False)
    assert completion.stop_reason == "tool_use"
    assert completion.usage.input_tokens == 1234
    assert completion.usage.output_tokens == 56
    assert completion.usage.cache_read_tokens == 0
    assert completion.usage.cache_write_tokens == 0
    assert completion.response_model == "qwen3.8:27b"


async def test_multiple_tool_calls_get_sequential_ids_unique_across_completions(
    settings: Settings,
) -> None:
    made = provider(
        settings,
        response(
            tool_calls=[
                {"function": {"name": "run_query", "arguments": {}}},
                {"function": {"name": "get_trace", "arguments": {}}},
            ]
        ),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    again = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    ids = [use.id for use in completion.tool_uses]
    assert [i.split("_")[1] for i in ids] == ["1", "2"]
    assert ids[0][-8:] == ids[1][-8:]
    # A second completion must never reuse an id: `_tool_names` maps ids
    # across the whole transcript, and a reuse would label a tool result
    # with the wrong tool name.
    assert not set(ids) & {use.id for use in again.tool_uses}


async def test_no_tool_calls_means_end_turn(settings: Settings) -> None:
    made = provider(settings, response(content="all clear"))
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.stop_reason == "end_turn"
    assert completion.tool_uses == []


async def test_a_response_with_no_usage_fields_counts_zero(settings: Settings) -> None:
    bare = {"model": "qwen3.8:27b", "message": {"role": "assistant", "content": "x"}}
    made = provider(settings, bare)
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.usage.input_tokens == 0
    assert completion.usage.output_tokens == 0


# --------------------------------------------------------------------------
# Malformed arguments
# --------------------------------------------------------------------------


async def test_json_string_arguments_are_decoded(settings: Settings) -> None:
    made = provider(
        settings,
        response(tool_calls=[{"function": {"name": "run_query", "arguments": '{"a": 1}'}}]),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.tool_uses[0].args == {"a": 1}
    assert completion.tool_uses[0].malformed is False


def test_no_arguments_is_an_empty_object_not_malformed() -> None:
    from agent.providers.ollama import _decode_args

    assert _decode_args(None) == ({}, False)


@pytest.mark.parametrize(
    "raw_args",
    [
        "p99(duration_ms)",  # text that never was JSON at all
        ["a", "b"],  # valid JSON, but a list rather than an object
        42,  # valid JSON, but a bare scalar
        "{not json",  # truncated, unparseable JSON
    ],
)
async def test_arguments_that_are_not_an_object_are_flagged_malformed(
    settings: Settings, raw_args: Any
) -> None:
    made = provider(
        settings,
        response(tool_calls=[{"function": {"name": "run_query", "arguments": raw_args}}]),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    use = completion.tool_uses[0]
    assert use.args == {}
    assert use.malformed is True
    assert use.name == "run_query"
