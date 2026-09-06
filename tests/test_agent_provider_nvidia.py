"""Tests for agent/providers/nvidia.py: the translation to and from NVIDIA's hosted
NIM chat completions endpoint, and its retry policy.

No network. A fake client records every request and replays canned
`httpx2.Response` objects (real ones, not stand-ins, so status codes,
headers, and body text behave exactly as they would against a live server),
so the request shape, the response parsing, and the retry/backoff path are
all checked against what would actually go on the wire. A `FlakyClient`
raises a real `httpx2.TransportError` for the transport-error retry tests,
since that path never sees an `httpx2.Response` at all.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from agent.providers.base import ToolResultBlock, ToolSchema, ToolUse, Turn
from agent.providers.nvidia import MAX_REQUEST_TOKENS, NvidiaAPIError, NvidiaProvider
from receipts.settings import Settings

TOOLS = [ToolSchema(name="run_query", description="query", input_schema={"type": "object"})]

_REQUEST = httpx2.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")


def http_response(
    status_code: int = 200,
    *,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    return httpx2.Response(
        status_code, json=json_body or {}, headers=headers or {}, request=_REQUEST
    )


class FakeClient:
    """Records every POST and replays the next canned `httpx2.Response`."""

    def __init__(self, responses: list[httpx2.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str] | None = None
    ) -> httpx2.Response:
        self.requests.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


def completion_body(
    *,
    content: str | None = "",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int | None = None,
    model: str = "moonshotai/kimi-k3",
    response_id: str = "chatcmpl-abc123",
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if cached_tokens is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": 1788663600,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


def sleeps(record: list[float]):
    async def _sleep(seconds: float) -> None:
        record.append(seconds)

    return _sleep


def provider(
    settings: Settings, *bodies: dict[str, Any], sleep=None, base_url: str | None = None
) -> NvidiaProvider:
    responses = [http_response(json_body=b) for b in bodies] or [
        http_response(json_body=completion_body(content="ok"))
    ]
    return NvidiaProvider(settings, client=FakeClient(responses), sleep=sleep, base_url=base_url)


@pytest.fixture
def nvidia_settings(clean_env: None) -> Settings:
    """The same fake settings as `settings` (see tests/conftest.py), plus a fake NVIDIA key.

    Built directly rather than derived from `settings` via `model_copy`:
    `model_copy(update=...)` assigns the raw value without re-running
    validation, so `nvidia_api_key` would end up a plain `str` instead of the
    `SecretStr` the field declares, and `.get_secret_value()` would break.
    """
    return Settings(
        _env_file=None,
        honeycomb_ingest_key="fake-ingest-key",
        honeycomb_mcp_key="fake-key-id:fake-secret",
        anthropic_api_key="fake-anthropic-key",
        anthropic_workspace_id="fake-workspace-id",
        nvidia_api_key="fake-nvidia-key",
    )


# --------------------------------------------------------------------------
# Construction and defaults
# --------------------------------------------------------------------------


def test_the_model_and_base_url_come_from_settings(nvidia_settings: Settings) -> None:
    made = provider(nvidia_settings)
    assert made.model == nvidia_settings.nvidia_model
    assert made._base_url == nvidia_settings.nvidia_base_url.rstrip("/")  # type: ignore[attr-defined]


def test_the_model_and_base_url_can_be_overridden(nvidia_settings: Settings) -> None:
    made = NvidiaProvider(
        nvidia_settings,
        model="other-model",
        base_url="https://elsewhere.example/v1",
        client=FakeClient([http_response(json_body=completion_body())]),
    )
    assert made.model == "other-model"
    assert made._base_url == "https://elsewhere.example/v1"  # type: ignore[attr-defined]


def test_missing_api_key_raises_a_clear_value_error(settings: Settings) -> None:
    assert settings.nvidia_api_key is None
    with pytest.raises(ValueError, match="NVIDIA_API_KEY"):
        NvidiaProvider(settings, client=FakeClient([http_response(json_body=completion_body())]))


def test_the_provider_satisfies_the_interface(nvidia_settings: Settings) -> None:
    from agent.providers.base import Provider

    assert isinstance(provider(nvidia_settings), Provider)


def test_the_provider_names_itself_for_the_report(nvidia_settings: Settings) -> None:
    made = provider(nvidia_settings)
    assert made.name == "nvidia"
    assert made.model == nvidia_settings.nvidia_model


def test_the_provider_name_is_nvidia(nvidia_settings: Settings) -> None:
    assert provider(nvidia_settings).name == "nvidia"


def test_repr_names_the_model_and_base_url_but_not_the_key(nvidia_settings: Settings) -> None:
    made = provider(nvidia_settings)
    text = repr(made)
    assert "fake-nvidia-key" not in text
    assert "fake-nvidia-key" not in str(made)
    assert made.model in text
    assert made._base_url in text  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# The request: url, auth header, tools, tool_choice, max_tokens, stream
# --------------------------------------------------------------------------


async def test_the_request_hits_the_chat_completions_url_with_bearer_auth(
    nvidia_settings: Settings,
) -> None:
    made = provider(nvidia_settings, completion_body(content="done"))
    await made.complete("the method", [Turn(role="user", text="go")], TOOLS, max_tokens=99)

    request = made._client.requests[0]  # type: ignore[attr-defined]
    assert request["url"] == f"{nvidia_settings.nvidia_base_url}/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer fake-nvidia-key"
    assert request["headers"]["Accept"] == "application/json"


async def test_the_request_carries_model_messages_max_tokens_and_stream_false(
    nvidia_settings: Settings,
) -> None:
    made = provider(nvidia_settings, completion_body(content="done"))
    await made.complete("the method", [Turn(role="user", text="go")], TOOLS, max_tokens=99)

    payload = made._client.requests[0]["json"]  # type: ignore[attr-defined]
    assert payload["model"] == nvidia_settings.nvidia_model
    assert payload["max_tokens"] == 99
    assert payload["stream"] is False
    assert payload["messages"][0] == {"role": "system", "content": "the method"}
    assert payload["messages"][1] == {"role": "user", "content": "go"}


async def test_max_tokens_is_clamped_to_the_server_ceiling(nvidia_settings: Settings) -> None:
    made = provider(nvidia_settings, completion_body(content="done"))
    await made.complete(
        "sys", [Turn(role="user", text="go")], TOOLS, max_tokens=MAX_REQUEST_TOKENS + 5000
    )
    payload = made._client.requests[0]["json"]  # type: ignore[attr-defined]
    assert payload["max_tokens"] == MAX_REQUEST_TOKENS == 16384


async def test_max_tokens_under_the_ceiling_passes_through_unchanged(
    nvidia_settings: Settings,
) -> None:
    made = provider(nvidia_settings, completion_body(content="done"))
    await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=1234)
    payload = made._client.requests[0]["json"]  # type: ignore[attr-defined]
    assert payload["max_tokens"] == 1234


async def test_tool_schemas_translate_to_the_function_shape_with_tool_choice_auto(
    nvidia_settings: Settings,
) -> None:
    made = provider(nvidia_settings, completion_body(content="done"))
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
    assert payload["tool_choice"] == "auto"


async def test_no_tools_means_no_tool_choice_key(nvidia_settings: Settings) -> None:
    made = provider(nvidia_settings, completion_body(content="done"))
    await made.complete("sys", [Turn(role="user", text="go")], [], max_tokens=99)

    payload = made._client.requests[0]["json"]  # type: ignore[attr-defined]
    assert payload["tools"] == []
    assert "tool_choice" not in payload


# --------------------------------------------------------------------------
# Assistant turns and tool results translate both ways
# --------------------------------------------------------------------------


async def test_an_assistant_turn_with_tool_uses_becomes_openai_tool_calls(
    nvidia_settings: Settings,
) -> None:
    made = provider(nvidia_settings, completion_body(content="done"))
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
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "run_query", "arguments": '{"a": 1}'},
            }
        ],
    }


async def test_an_assistant_turn_with_tool_uses_and_no_text_sends_content_none(
    nvidia_settings: Settings,
) -> None:
    made = provider(nvidia_settings, completion_body(content="done"))
    turns = [
        Turn(role="user", text="go"),
        Turn(
            role="assistant",
            text=None,
            tool_uses=[ToolUse(id="call_1", name="run_query", args={})],
        ),
    ]
    await made.complete("sys", turns, TOOLS, max_tokens=99)
    messages = made._client.requests[0]["json"]["messages"]  # type: ignore[attr-defined]
    assert messages[2]["content"] is None


async def test_an_assistant_turn_with_no_tool_uses_and_no_text_sends_content_empty_string(
    nvidia_settings: Settings,
) -> None:
    """No tool calls at all is a plain (if empty) reply, not a null one."""
    made = provider(nvidia_settings, completion_body(content="done"))
    turns = [
        Turn(role="user", text="go"),
        Turn(role="assistant", text=None, tool_uses=[]),
    ]
    await made.complete("sys", turns, TOOLS, max_tokens=99)
    messages = made._client.requests[0]["json"]["messages"]  # type: ignore[attr-defined]
    assert messages[2] == {"role": "assistant", "content": ""}


async def test_a_tool_result_becomes_a_role_tool_message_keyed_on_the_call_id(
    nvidia_settings: Settings,
) -> None:
    made = provider(nvidia_settings, completion_body(content="done"))
    turns = [
        Turn(role="user", text="go"),
        Turn(
            role="assistant",
            tool_uses=[ToolUse(id="run_query:0", name="run_query", args={"a": 1})],
        ),
        Turn(
            role="user",
            tool_results=[ToolResultBlock(tool_use_id="run_query:0", content="17 rows")],
        ),
    ]
    await made.complete("sys", turns, TOOLS, max_tokens=99)

    messages = made._client.requests[0]["json"]["messages"]  # type: ignore[attr-defined]
    assert messages[3] == {"role": "tool", "tool_call_id": "run_query:0", "content": "17 rows"}


async def test_two_tool_results_in_one_turn_become_two_messages(
    nvidia_settings: Settings,
) -> None:
    made = provider(nvidia_settings, completion_body(content="done"))
    turns = [
        Turn(role="user", text="go"),
        Turn(
            role="assistant",
            tool_uses=[
                ToolUse(id="run_query:0", name="run_query", args={}),
                ToolUse(id="get_trace:1", name="get_trace", args={}),
            ],
        ),
        Turn(
            role="user",
            tool_results=[
                ToolResultBlock(tool_use_id="run_query:0", content="rows"),
                ToolResultBlock(tool_use_id="get_trace:1", content="trace"),
            ],
        ),
    ]
    await made.complete("sys", turns, TOOLS, max_tokens=99)

    messages = made._client.requests[0]["json"]["messages"]  # type: ignore[attr-defined]
    assert messages[3] == {"role": "tool", "tool_call_id": "run_query:0", "content": "rows"}
    assert messages[4] == {"role": "tool", "tool_call_id": "get_trace:1", "content": "trace"}


async def test_a_provider_echoes_back_its_own_assistant_message(
    nvidia_settings: Settings,
) -> None:
    """`raw` keeps anything the neutral transcript does not model, such as
    `reasoning_content`."""
    made = provider(nvidia_settings, completion_body(content="done"))
    raw = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "run_query:0",
                "type": "function",
                "function": {"name": "run_query", "arguments": "{}"},
            }
        ],
        "reasoning_content": "thinking about it",
    }
    turns = [Turn(role="user", text="go"), Turn(role="assistant", text="ignored", raw=raw)]
    await made.complete("sys", turns, TOOLS, max_tokens=99)

    messages = made._client.requests[0]["json"]["messages"]  # type: ignore[attr-defined]
    assert messages[2] is raw


# --------------------------------------------------------------------------
# The response: text, tool_calls, usage, stop_reason, ids
# --------------------------------------------------------------------------


async def test_text_and_tool_calls_come_back_as_a_completion(nvidia_settings: Settings) -> None:
    made = provider(
        nvidia_settings,
        completion_body(
            content=None,
            tool_calls=[
                {
                    "id": "run_query:0",
                    "type": "function",
                    "function": {
                        "name": "run_query",
                        "arguments": '{"breakdowns": ["cloud.region"]}',
                    },
                }
            ],
            finish_reason="tool_calls",
            prompt_tokens=237,
            completion_tokens=96,
            model="moonshotai/kimi-k3",
            response_id="chatcmpl-63280d8a",
        ),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)

    assert completion.text == ""
    (use,) = completion.tool_uses
    assert use.id == "run_query:0"
    assert (use.name, use.args, use.malformed) == (
        "run_query",
        {"breakdowns": ["cloud.region"]},
        False,
    )
    assert completion.stop_reason == "tool_use"
    assert completion.usage.input_tokens == 237
    assert completion.usage.output_tokens == 96
    assert completion.usage.cache_read_tokens == 0
    assert completion.usage.cache_write_tokens == 0
    assert completion.response_model == "moonshotai/kimi-k3"
    assert completion.response_id == "chatcmpl-63280d8a"


async def test_cached_tokens_fill_cache_read_tokens_when_present(
    nvidia_settings: Settings,
) -> None:
    made = provider(
        nvidia_settings, completion_body(content="ok", prompt_tokens=100, cached_tokens=40)
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.usage.cache_read_tokens == 40


async def test_a_repeated_server_id_across_two_turns_pairs_with_its_own_tool_result(
    nvidia_settings: Settings,
) -> None:
    """A live kimi-k3 response reuses ids like `run_query:0` across separate

    completions in the same run. Nothing in the provider may dedup or key on
    an id across turns, so two assistant turns that both use `run_query:0`
    must each still land next to their own tool result, in order, rather than
    being merged or mismatched.
    """
    made = provider(nvidia_settings, completion_body(content="done"))
    turns = [
        Turn(role="user", text="go"),
        Turn(
            role="assistant",
            tool_uses=[ToolUse(id="run_query:0", name="run_query", args={"a": 1})],
        ),
        Turn(
            role="user",
            tool_results=[ToolResultBlock(tool_use_id="run_query:0", content="first rows")],
        ),
        Turn(
            role="assistant",
            tool_uses=[ToolUse(id="run_query:0", name="run_query", args={"a": 2})],
        ),
        Turn(
            role="user",
            tool_results=[ToolResultBlock(tool_use_id="run_query:0", content="second rows")],
        ),
    ]
    await made.complete("sys", turns, TOOLS, max_tokens=99)

    messages = made._client.requests[0]["json"]["messages"]  # type: ignore[attr-defined]
    # index 0 is the system message, 1 is the opening user turn.
    first_assistant, first_tool, second_assistant, second_tool = messages[2:6]
    assert first_assistant["tool_calls"][0]["id"] == "run_query:0"
    assert first_tool == {"role": "tool", "tool_call_id": "run_query:0", "content": "first rows"}
    assert second_assistant["tool_calls"][0]["id"] == "run_query:0"
    assert second_tool == {
        "role": "tool",
        "tool_call_id": "run_query:0",
        "content": "second rows",
    }
    # The two assistant messages carry different arguments despite the same id.
    assert first_assistant["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    assert second_assistant["tool_calls"][0]["function"]["arguments"] == '{"a": 2}'


async def test_a_fallback_id_is_written_into_raw_so_the_echoed_turn_matches_its_tool_result(
    nvidia_settings: Settings,
) -> None:
    """A tool call with no server id gets a fallback id, and that fallback id

    has to end up inside `raw_content`'s own `tool_calls` entry, not just on
    the `ToolUse` the loop builds a `Turn` from: `raw` is echoed back
    verbatim on the next request, so if the fallback id were only on
    `ToolUse` the echoed assistant message would carry no id at all, and the
    tool result message the loop builds from `ToolUse.id` would then answer
    a call the assistant message never claimed.
    """
    made = provider(
        nvidia_settings,
        completion_body(
            content=None,
            tool_calls=[{"type": "function", "function": {"name": "run_query", "arguments": "{}"}}],
        ),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    fallback_id = completion.tool_uses[0].id
    assert completion.raw_content["tool_calls"][0]["id"] == fallback_id

    # Round-trip: echo the raw assistant turn, then answer it with a tool
    # result keyed on the same fallback id, and check the two ids match on
    # the wire.
    turns = [
        Turn(role="user", text="go"),
        Turn(role="assistant", raw=completion.raw_content),
        Turn(
            role="user",
            tool_results=[ToolResultBlock(tool_use_id=fallback_id, content="17 rows")],
        ),
    ]
    await made.complete("sys", turns, TOOLS, max_tokens=99)
    messages = made._client.requests[-1]["json"]["messages"]  # type: ignore[attr-defined]
    echoed_assistant, tool_message = messages[2], messages[3]
    assert echoed_assistant["tool_calls"][0]["id"] == fallback_id
    assert tool_message == {"role": "tool", "tool_call_id": fallback_id, "content": "17 rows"}


async def test_a_null_content_message_is_empty_text_not_none(nvidia_settings: Settings) -> None:
    made = provider(nvidia_settings, completion_body(content=None, tool_calls=[]))
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.text == ""


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("content_filter", "content_filter"),
    ],
)
async def test_finish_reason_maps_to_stop_reason_when_no_tool_calls(
    nvidia_settings: Settings, finish_reason: str, expected: str
) -> None:
    made = provider(
        nvidia_settings, completion_body(content="all clear", finish_reason=finish_reason)
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.stop_reason == expected
    assert completion.tool_uses == []


async def test_tool_calls_present_means_tool_use_regardless_of_finish_reason(
    nvidia_settings: Settings,
) -> None:
    made = provider(
        nvidia_settings,
        completion_body(
            content=None,
            tool_calls=[
                {
                    "id": "run_query:0",
                    "type": "function",
                    "function": {"name": "run_query", "arguments": "{}"},
                }
            ],
            finish_reason="tool_calls",
        ),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.stop_reason == "tool_use"


async def test_a_response_with_no_usage_fields_counts_zero(nvidia_settings: Settings) -> None:
    bare = {
        "id": "chatcmpl-x",
        "model": "moonshotai/kimi-k3",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}
        ],
    }
    made = provider(nvidia_settings, bare)
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.usage.input_tokens == 0
    assert completion.usage.output_tokens == 0


# --------------------------------------------------------------------------
# Fallback ids when the server omits one
# --------------------------------------------------------------------------


async def test_a_missing_server_id_falls_back_to_call_n_hex(nvidia_settings: Settings) -> None:
    made = provider(
        nvidia_settings,
        completion_body(
            content=None,
            tool_calls=[{"type": "function", "function": {"name": "run_query", "arguments": "{}"}}],
        ),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    (use,) = completion.tool_uses
    assert use.id.startswith("call_1_") and len(use.id) == len("call_1_") + 8


async def test_multiple_missing_ids_get_sequential_fallback_numbers(
    nvidia_settings: Settings,
) -> None:
    made = provider(
        nvidia_settings,
        completion_body(
            content=None,
            tool_calls=[
                {"type": "function", "function": {"name": "run_query", "arguments": "{}"}},
                {"type": "function", "function": {"name": "get_trace", "arguments": "{}"}},
            ],
        ),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    ids = [use.id for use in completion.tool_uses]
    assert [i.split("_")[1] for i in ids] == ["1", "2"]
    assert ids[0][-8:] == ids[1][-8:]


# --------------------------------------------------------------------------
# Malformed arguments
# --------------------------------------------------------------------------


async def test_json_string_arguments_are_decoded(nvidia_settings: Settings) -> None:
    made = provider(
        nvidia_settings,
        completion_body(
            content=None,
            tool_calls=[
                {
                    "id": "run_query:0",
                    "type": "function",
                    "function": {"name": "run_query", "arguments": '{"a": 1}'},
                }
            ],
        ),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.tool_uses[0].args == {"a": 1}
    assert completion.tool_uses[0].malformed is False


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
    nvidia_settings: Settings, raw_args: Any
) -> None:
    made = provider(
        nvidia_settings,
        completion_body(
            content=None,
            tool_calls=[
                {
                    "id": "run_query:0",
                    "type": "function",
                    "function": {"name": "run_query", "arguments": raw_args},
                }
            ],
        ),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    use = completion.tool_uses[0]
    assert use.args == {}
    assert use.malformed is True
    assert use.name == "run_query"


# --------------------------------------------------------------------------
# Retries: 429/5xx with backoff and Retry-After, transport errors, exhaustion,
# and no retry on a non-retryable status
# --------------------------------------------------------------------------


async def test_a_429_is_retried_and_retry_after_is_honoured(nvidia_settings: Settings) -> None:
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FakeClient(
            [
                http_response(429, headers={"Retry-After": "7"}),
                http_response(json_body=completion_body(content="ok after retry")),
            ]
        ),
        sleep=sleeps(waits),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.text == "ok after retry"
    assert waits == [7.0]
    assert len(made._client.requests) == 2  # type: ignore[attr-defined]


async def test_a_429_with_no_retry_after_uses_the_computed_backoff(
    nvidia_settings: Settings,
) -> None:
    """The live 429 body carried no `Retry-After` header at all."""
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FakeClient(
            [
                http_response(429, json_body={"status": 429, "title": "Too Many Requests"}),
                http_response(json_body=completion_body(content="ok")),
            ]
        ),
        sleep=sleeps(waits),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.text == "ok"
    assert waits == [5.0]


async def test_a_5xx_is_retried_with_exponential_backoff_when_no_retry_after(
    nvidia_settings: Settings,
) -> None:
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FakeClient(
            [
                http_response(503),
                http_response(503),
                http_response(json_body=completion_body(content="ok")),
            ]
        ),
        sleep=sleeps(waits),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.text == "ok"
    assert waits == [5.0, 10.0]


async def test_backoff_is_capped_at_sixty_seconds(nvidia_settings: Settings) -> None:
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FakeClient(
            [
                http_response(503),
                http_response(503),
                http_response(503),
                http_response(503),
                http_response(503),
                http_response(json_body=completion_body(content="ok")),
            ]
        ),
        sleep=sleeps(waits),
    )
    await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert waits == [5.0, 10.0, 20.0, 40.0, 60.0]


async def test_six_failures_exhaust_retries_and_raise(nvidia_settings: Settings) -> None:
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FakeClient(
            [http_response(503, json_body={"error": {"message": "overloaded"}}) for _ in range(6)]
        ),
        sleep=sleeps(waits),
    )
    with pytest.raises(NvidiaAPIError, match="503"):
        await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert len(made._client.requests) == 6  # type: ignore[attr-defined]
    assert len(waits) == 5  # never sleeps after the last attempt


async def test_a_400_is_not_retried(nvidia_settings: Settings) -> None:
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FakeClient([http_response(400, json_body={"error": "bad request"})]),
        sleep=sleeps(waits),
    )
    with pytest.raises(NvidiaAPIError):
        await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert len(made._client.requests) == 1  # type: ignore[attr-defined]
    assert waits == []


async def test_a_400_body_appears_in_the_raised_message(nvidia_settings: Settings) -> None:
    """`agent/loop.py` only records `f"{type(exc).__name__}: {exc}"` on a failed

    run, so the response body has to be inside the exception's own message
    or it never reaches a run's report.json or grade.json.
    """
    made = NvidiaProvider(
        nvidia_settings,
        client=FakeClient(
            [http_response(400, json_body={"error": "invalid_request", "detail": "bad model id"})]
        ),
    )
    with pytest.raises(NvidiaAPIError) as excinfo:
        await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    message = f"{type(excinfo.value).__name__}: {excinfo.value}"
    assert "400" in message
    assert "invalid_request" in message
    assert "bad model id" in message


async def test_an_unparseable_retry_after_falls_back_to_the_computed_backoff(
    nvidia_settings: Settings,
) -> None:
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FakeClient(
            [
                http_response(429, headers={"Retry-After": "not-a-number"}),
                http_response(json_body=completion_body(content="ok")),
            ]
        ),
        sleep=sleeps(waits),
    )
    await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert waits == [5.0]


@pytest.mark.parametrize(
    ("retry_after", "expected_wait"),
    [
        ("999999", 60.0),  # far above the cap: clamped down to it
        ("-30", 0.0),  # negative: clamped up to zero
        ("inf", 5.0),  # not finite at all: falls back to the computed backoff
    ],
)
async def test_retry_after_is_clamped_to_a_sane_range(
    nvidia_settings: Settings, retry_after: str, expected_wait: float
) -> None:
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FakeClient(
            [
                http_response(429, headers={"Retry-After": retry_after}),
                http_response(json_body=completion_body(content="ok")),
            ]
        ),
        sleep=sleeps(waits),
    )
    await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert waits == [expected_wait]


# --------------------------------------------------------------------------
# Transport errors: retried the same as a retryable status
# --------------------------------------------------------------------------


class FlakyClient:
    """Raises a transport error for the first N posts, then defers to a FakeClient."""

    def __init__(self, failures: list[Exception], client: FakeClient) -> None:
        self._failures = list(failures)
        self._client = client
        self.requests = client.requests

    async def post(self, *args: Any, **kwargs: Any) -> httpx2.Response:
        if self._failures:
            raise self._failures.pop(0)
        return await self._client.post(*args, **kwargs)


async def test_a_connect_error_is_retried_like_a_retryable_status(
    nvidia_settings: Settings,
) -> None:
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FlakyClient(
            [httpx2.ConnectError("connection refused")],
            FakeClient([http_response(json_body=completion_body(content="ok"))]),
        ),
        sleep=sleeps(waits),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.text == "ok"
    assert waits == [5.0]


async def test_a_read_timeout_is_retried_like_a_retryable_status(
    nvidia_settings: Settings,
) -> None:
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FlakyClient(
            [httpx2.ReadTimeout("timed out")],
            FakeClient([http_response(json_body=completion_body(content="ok"))]),
        ),
        sleep=sleeps(waits),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.text == "ok"
    assert waits == [5.0]


async def test_a_remote_protocol_error_is_retried_like_a_retryable_status(
    nvidia_settings: Settings,
) -> None:
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FlakyClient(
            [httpx2.RemoteProtocolError("connection closed")],
            FakeClient([http_response(json_body=completion_body(content="ok"))]),
        ),
        sleep=sleeps(waits),
    )
    completion = await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert completion.text == "ok"
    assert waits == [5.0]


async def test_a_transport_error_on_the_last_attempt_is_re_raised(
    nvidia_settings: Settings,
) -> None:
    waits: list[float] = []
    made = NvidiaProvider(
        nvidia_settings,
        client=FlakyClient(
            [httpx2.ConnectError("connection refused") for _ in range(6)],
            FakeClient([http_response(json_body=completion_body())]),
        ),
        sleep=sleeps(waits),
    )
    with pytest.raises(httpx2.ConnectError):
        await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    assert len(waits) == 5


# --------------------------------------------------------------------------
# Retry logging: WARNING, not INFO, so a run's own log shows a retry happened
# --------------------------------------------------------------------------


async def test_a_status_retry_is_logged_at_warning(
    nvidia_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    made = NvidiaProvider(
        nvidia_settings,
        client=FakeClient(
            [http_response(429), http_response(json_body=completion_body(content="ok"))]
        ),
        sleep=sleeps([]),
    )
    with caplog.at_level("WARNING", logger="agent.providers.nvidia"):
        await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    records = [r for r in caplog.records if r.name == "agent.providers.nvidia"]
    assert any(r.levelname == "WARNING" and "429" in r.getMessage() for r in records)


async def test_a_transport_error_retry_is_logged_at_warning(
    nvidia_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    made = NvidiaProvider(
        nvidia_settings,
        client=FlakyClient(
            [httpx2.ConnectError("connection refused")],
            FakeClient([http_response(json_body=completion_body(content="ok"))]),
        ),
        sleep=sleeps([]),
    )
    with caplog.at_level("WARNING", logger="agent.providers.nvidia"):
        await made.complete("sys", [Turn(role="user", text="go")], TOOLS, max_tokens=99)
    records = [r for r in caplog.records if r.name == "agent.providers.nvidia"]
    assert any(r.levelname == "WARNING" and "ConnectError" in r.getMessage() for r in records)
