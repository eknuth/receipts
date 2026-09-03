"""The one interface every model provider implements: messages in, tool use out.

The loop in `agent/loop.py` never imports a vendor SDK. It builds a neutral
transcript of `Turn` objects and hands it to a `Provider`, which translates to
whatever its API wants and translates the answer back into a `Completion`.
`anthropic.py` is here now; `bedrock.py` (R11) and `ollama.py` (R15) plug into
the same three types.

An assistant turn carries `raw`, the provider's own representation of what it
said. A provider that gets its own `raw` back can echo it verbatim instead of
rebuilding it from parts, which matters for anything the neutral shape does not
model, such as thinking blocks. A provider that is handed a turn produced by a
different provider falls back to rebuilding from `text` and `tool_uses`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolSchema:
    """One tool offered to the model."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolUse:
    """The model asking for one tool call."""

    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    """The answer to one `ToolUse`, on its way back to the model."""

    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Usage:
    """Tokens billed for one completion.

    Cached reads are counted separately because they are billed at a tenth of
    the input rate. `tokens_in` in the report is the uncached figure plus the
    cache writes; `evals/pricing.py` prices the three buckets apart.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class Turn:
    """One entry in the neutral transcript.

    A user turn carries either `text` or `tool_results`. An assistant turn
    carries `text` and `tool_uses`, plus `raw` when the provider that produced
    it wants it back unchanged.
    """

    role: Literal["user", "assistant"]
    text: str | None = None
    tool_uses: list[ToolUse] = field(default_factory=list)
    tool_results: list[ToolResultBlock] = field(default_factory=list)
    raw: Any = None


@dataclass(frozen=True)
class Completion:
    """One model response."""

    text: str
    tool_uses: list[ToolUse]
    usage: Usage
    stop_reason: str | None = None
    raw_content: Any = None


@runtime_checkable
class Provider(Protocol):
    """Messages in, tool use out. Every provider is async and stateless."""

    name: str
    model: str

    async def complete(
        self,
        system: str,
        turns: list[Turn],
        tools: list[ToolSchema],
        *,
        max_tokens: int,
    ) -> Completion:
        """One turn of the conversation."""
        ...
