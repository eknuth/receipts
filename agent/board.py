"""Create (or find) the Canvas board for one investigation.

    result = await ensure_board(report, mcp, environment_slug=settings.honeycomb_env)

R12 (EDW-1334). A board is `create_board`'s durable artifact: a named page
of panels in an environment. This gives each investigation one, named
`receipts <scenario> <run_id[:8]>`, holding the top three evidence queries
from the top hypothesis as query panels and one markdown panel with the
report's own findings.

`list_boards` runs first and the exact name is matched against what comes
back, so re-running the eval matrix over a run id that already has a board
returns that board's id and url instead of making a second one. This is the
same "read before you write" shape `agent/mcp_client.py`'s `run_bubbleup`
provenance check uses, for the same reason: a rerun is expected and should
be a no-op, not a duplicate.

Like `agent/handoff.py`, the exact JSON shape `list_boards` and
`create_board` return was not captured against the live server before this
was written; only the field names and constraints the R12 issue's live
facts describe. Both are read through the same small set of candidate key
names `agent/handoff.py` uses, for the same reason: a live shape outside
that list reads back as `None` rather than raising, and the orchestrator
confirms the real shape afterward.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from agent.handoff import summary_lines
from agent.report import Report

MAX_EVIDENCE_PANELS = 3

# create_board's own limits (from the R12 issue's live facts): a panel
# description is capped at 1023 characters, a text panel's markdown at
# 10000, and a board name at 255. The name this module builds is always
# well under that; the caps below guard the two fields built from
# free-text report content.
MAX_PANEL_DESCRIPTION = 1023
MAX_TEXT_PANEL_CONTENT = 10000

# A tag value: a lowercase letter, then letters, digits, "/", or "-", up to
# 128 characters total. `run_id` (gen/emit.py's `new_run_id`) is `run-` plus
# 12 lowercase hex characters, which already satisfies this, but the rule is
# checked rather than assumed: a run id from anywhere else in the system
# that does not fit is left untagged and found by name instead.
_TAG_VALUE = re.compile(r"^[a-z][a-zA-Z0-9/-]{0,127}$")

# How many pages of list_boards to look through for a name match before
# giving up and creating a board. A guard against an unbounded loop if the
# live pagination shape turns out to keep signalling "more" forever; a team
# does not have thousands of boards today, so this is far more than one
# real search should need.
MAX_LIST_PAGES = 40


@dataclass(frozen=True)
class BoardResult:
    """What `ensure_board` found or made.

    `created` is False both when an existing board was reused and when
    creation failed outright (`error` is set in that case); the field
    distinguishes "no new board because one was already there" from
    "no new board because this run does not have one", which
    `evals/report.py`'s handoff summary reads differently.
    """

    board_id: str | None
    board_url: str | None
    created: bool
    error: str | None = None


def board_name(report: Report) -> str:
    """`receipts <scenario> <run_id[:8]>`, the name every board for this
    run is created and found under."""
    return f"receipts {report.scenario_id} {report.run_id[:8]}"


def run_tag(run_id: str) -> str | None:
    """`run:<run_id>`, when `run_id` satisfies a tag value's own rules; else None."""
    return f"run:{run_id}" if _TAG_VALUE.match(run_id) else None


def _summary_markdown(report: Report) -> str:
    """The text panel's content: the same findings `agent/handoff.py`
    sends to Canvas, as a markdown bullet list instead of chat lines, with
    no trailing question since this panel is not a conversation."""
    lines = ["## Findings", ""]
    lines += [f"- {line}" for line in summary_lines(report)]
    return "\n".join(lines)[:MAX_TEXT_PANEL_CONTENT]


def _panels(report: Report) -> list[dict[str, Any]]:
    """Up to `MAX_EVIDENCE_PANELS` query panels from the top hypothesis's
    evidence, in order, plus one text panel last.

    A query panel's `id` is the evidence's `query_id`: `Evidence.query_id`
    already holds the query run PK `create_board` wants for a `type="query"`
    panel, no translation needed (confirmed against a stored result, see the
    R12 issue notes). Fewer than three evidence items just means fewer query
    panels; a hypothesis with none produces only the text panel. The text
    panel never carries `id`, since the schema forbids one on `type="text"`.
    """
    evidence = report.hypotheses[0].evidence[:MAX_EVIDENCE_PANELS] if report.hypotheses else []
    panels: list[dict[str, Any]] = [
        {
            "type": "query",
            "id": item.query_id,
            "description": item.summary[:MAX_PANEL_DESCRIPTION],
        }
        for item in evidence
    ]
    panels.append({"type": "text", "content": _summary_markdown(report)})
    return panels


def _payload(result: Any) -> dict[str, Any]:
    """See `agent.handoff._payload`: the same defensive dict-or-JSON-text read."""
    raw = getattr(result, "raw", None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _boards(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The list of boards in a `list_boards` result, whichever key it came under."""
    for key in ("boards", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _field(payload: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if value:
            return str(value)
    return None


async def _find_existing(
    mcp: Any, *, environment_slug: str, name: str, tag: str | None
) -> tuple[str, str | None] | None:
    """The id and url of a board already named `name`, or None.

    Filtered by `tag` server-side when one is valid, which is the whole
    point of tagging boards with `run:<run_id>`; without one (or if the
    tagged page still turns up nothing, which a tag alone cannot rule out
    on its own listing semantics) every page is read and matched by name,
    up to `MAX_LIST_PAGES`.
    """
    page = 1
    while page <= MAX_LIST_PAGES:
        args: dict[str, Any] = {"environment_slug": environment_slug, "page": page}
        if tag:
            args["tags"] = [tag]
        result = await mcp.call("list_boards", args)
        if getattr(result, "is_error", False):
            return None
        payload = _payload(result)
        boards = _boards(payload)
        for board in boards:
            if board.get("name") == name:
                board_id = _field(board, "id", "board_id")
                board_url = _field(board, "url", "board_url", "board_link")
                if board_id:
                    return board_id, board_url
        if not boards:
            return None
        page += 1
    return None


async def ensure_board(report: Report, mcp: Any, *, environment_slug: str) -> BoardResult:
    """The board for this run: an existing one by name, or a freshly created one.

    Never raises: a `list_boards` or `create_board` failure comes back as a
    `BoardResult` with `created=False` and `error` set, the same "bolt-on,
    not a gate" contract `agent/handoff.py`'s `hand_off` follows, since a
    board is a courtesy on top of a graded report, not part of it.
    """
    name = board_name(report)
    tag = run_tag(report.run_id)
    try:
        existing = await _find_existing(mcp, environment_slug=environment_slug, name=name, tag=tag)
        if existing is not None:
            board_id, board_url = existing
            return BoardResult(board_id=board_id, board_url=board_url, created=False)

        result = await mcp.call(
            "create_board",
            {
                "environment_slug": environment_slug,
                "name": name,
                "panels": _panels(report),
                "tags": [tag] if tag else [],
            },
        )
        if getattr(result, "is_error", False):
            return BoardResult(
                board_id=None, board_url=None, created=False, error=getattr(result, "text", None)
            )
        payload = _payload(result)
        board_id = _field(payload, "id", "board_id")
        board_url = _field(payload, "url", "board_url", "board_link")
        return BoardResult(board_id=board_id, board_url=board_url, created=True)
    except Exception as exc:
        return BoardResult(
            board_id=None, board_url=None, created=False, error=f"{type(exc).__name__}: {exc}"
        )
