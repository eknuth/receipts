"""Decoding one tool call's arguments, shared by every provider whose tool-calling
protocol can hand back arguments that are not a JSON object.

Both `agent/providers/ollama.py` and `agent/providers/nvidia.py` speak a
protocol where `arguments` is normally a JSON-encoded string that decodes to
an object, but nothing on the wire guarantees that: a live smoke test against
Ollama had a model put a bare string where an object belonged, and the same
shape of failure is possible from any OpenAI-compatible endpoint, NVIDIA's
NIM included. `decode_args` is the one place that decision gets made, so a
provider does not have to re-derive it and cannot quietly diverge from it.

`None` (no arguments at all) is not malformed: it is an empty call, the same
reading `agent/providers/anthropic.py` gives a `tool_use` block with no
input. A JSON-encoded string is decoded first. What is left after that, if it
is not a JSON object, is what gets flagged: a list, a bare scalar, or text
that never was JSON at all. The caller is expected to build a `ToolUse` with
`args={}` and `malformed=True` in that case, and to send the call to the
tool's own schema unchanged otherwise, exactly like any other call.
"""

from __future__ import annotations

import json
from typing import Any


def decode_args(value: Any) -> tuple[dict[str, Any], bool]:
    """One tool call's arguments, or (`{}`, malformed) when they cannot be trusted."""
    if value is None:
        return {}, False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}, True
    if not isinstance(value, dict):
        return {}, True
    return value, False
