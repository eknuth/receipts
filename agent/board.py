"""Create (or find) the Canvas board for one investigation.

    result = await ensure_board(report, mcp, environment_slug=settings.honeycomb_env)

R12 (EDW-1334). A board is `create_board`'s durable artifact: a named page
of panels in an environment. This gives each investigation one, named
`receipts <scenario> <run_id[:8]>`, holding the top three evidence queries
from the top hypothesis as query panels and one markdown panel with the
report's own findings.

`list_boards` runs first and the exact name is matched against what comes
back, so re-running the eval matrix over a run id that already has a board
returns that board's id instead of making a second one. This is the same
"read before you write" shape `agent/mcp_client.py`'s `run_bubbleup`
provenance check uses, for the same reason: a rerun is expected and should
be a no-op, not a duplicate.

Both tools were captured live on 2026-09-07, and the shape is Markdown text,
not JSON: `create_board` returns a "Board created successfully." line
followed by a `Metadata:` block (`board_id`, `board_name`, `board_url`,
`environment`, `text_count`/`query_count`, `updated_at`), or, on failure,
`is_error=True` with the body `Unable to create board` and no further
detail; `list_boards` returns a `# Boards` heading, a Markdown table (`ID`,
`Name`, `Description`, `Private`, `QueryCount`, `SLOCount`, `TextCount`,
`UpdatedAt`, `Tags`) when there is at least one board, or just the heading
and a `Metadata:` block when there are none, plus its own `Metadata:` block
naming `page`, `total_pages`, and `total_items`. An earlier version of this
module read both as JSON via a private `_payload`/`_field` pair copied from
`agent/handoff.py`; against real Markdown that always returned `{}`, so
`_find_existing` never found anything and every run created a fresh,
duplicate board. This version parses the same way `agent/format.py` already
does for every read tool: `parse_metadata_block` for the `Metadata:` block,
`parse_results_table` for the table, reused rather than re-implemented.

A query panel needs a non-empty `name`: `create_board`'s schema documents
`name` as optional with a default, but the live server rejects a board
containing a query panel that omits it (`Unable to create board`), even
though the same call with a `description` but no `name` fails the same way
and a `name` alone succeeds. Verified live 2026-09-07 by varying one field
at a time against fresh `query_run_pk`s. So every query panel `_panels`
builds carries a `name`, in addition to the `description` it already set;
`Evidence.query_id` still drops straight into a panel's `id` with no
translation, as R12 originally specified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent import format as fmt
from agent.handoff import summary_lines
from agent.report import Report

MAX_EVIDENCE_PANELS = 3

# create_board's own limits (from the R12 issue's live facts): a panel
# description is capped at 1023 characters, a text panel's markdown at
# 10000, a board name at 255, and a panel name shares the description's cap
# (unconfirmed exactly, so it is capped the same way rather than assumed
# unlimited). The caps below guard the fields built from free-text report
# content; the board name this module builds is always well under 255.
MAX_PANEL_DESCRIPTION = 1023
# A panel name is a heading on the board, not the finding itself. The server
# accepts a long one, but a live board built on 2026-09-07 titled every panel
# with a whole paragraph of evidence and was unreadable, so the name is cut to
# a short label here and the full summary stays in `description`.
MAX_PANEL_NAME = 70
MAX_TEXT_PANEL_CONTENT = 10000

# A tag value: a lowercase letter, then letters, digits, "/", or "-", up to
# 128 characters total. `run_id` (gen/emit.py's `new_run_id`) is `run-` plus
# 12 lowercase hex characters, which already satisfies this, but the rule is
# checked rather than assumed: a run id from anywhere else in the system
# that does not fit is left untagged and found by name instead.
_TAG_VALUE = re.compile(r"^[a-z][a-zA-Z0-9/-]{0,127}$")

# A safety bound on `list_boards` pagination, independent of the server's own
# `total_pages`: a guard against an unbounded loop if that field is ever
# missing or absurd, not the primary stop condition (see `_find_existing`).
_MAX_LIST_PAGES_SAFETY = 200


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


def _panel_name(summary: str, index: int) -> str:
    """A non-empty name for a query panel, from the evidence's own summary.

    The live server rejects a query panel with no `name` (see the module
    docstring); a blank or whitespace-only summary must not take the whole
    board down with it, so a numbered fallback stands in when one is needed.

    An evidence summary is a few sentences, and a panel name is a heading, so
    this takes the first sentence and cuts it at `MAX_PANEL_NAME` on a word
    boundary. The full summary is still on the panel, as its `description`.
    """
    text = summary.strip()
    if not text:
        return f"Evidence {index}"
    sentence = text.split(". ", 1)[0].rstrip(".")
    if len(sentence) <= MAX_PANEL_NAME:
        return sentence
    clipped = sentence[:MAX_PANEL_NAME].rsplit(" ", 1)[0]
    return f"{clipped or sentence[:MAX_PANEL_NAME]}..."


def _panels(report: Report) -> list[dict[str, Any]]:
    """Up to `MAX_EVIDENCE_PANELS` query panels from the top hypothesis's
    evidence, in order, plus one text panel last.

    A query panel's `id` is the evidence's `query_id`: `Evidence.query_id`
    already holds the query run PK `create_board` wants for a `type="query"`
    panel, no translation needed. Every query panel also carries `name`
    (required by the live server, not by the documented schema; see the
    module docstring) alongside the `description`. Fewer than three evidence
    items just means fewer query panels; a hypothesis with none produces
    only the text panel. The text panel never carries `id` or `name`, since
    the schema forbids them on `type="text"`.
    """
    evidence = report.hypotheses[0].evidence[:MAX_EVIDENCE_PANELS] if report.hypotheses else []
    panels: list[dict[str, Any]] = [
        {
            "type": "query",
            "id": item.query_id,
            "name": _panel_name(item.summary, i),
            "description": item.summary[:MAX_PANEL_DESCRIPTION],
        }
        for i, item in enumerate(evidence, start=1)
    ]
    panels.append({"type": "text", "content": _summary_markdown(report)})
    return panels


def _result_text(result: Any) -> str:
    """The compact text a `HoneycombMCP.call` result carries, or the empty string.

    Both board tools return Markdown, and `HoneycombMCP.call` puts it (via
    `agent/format.py`'s passthrough formatting for a tool it does not know
    specially) on `.text`; a test's stand-in result gives the same field
    directly. Missing entirely is read as empty rather than raising.
    """
    return getattr(result, "text", None) or ""


def _parse_created_board(result: Any) -> tuple[str | None, str | None]:
    """The `board_id` and `board_url` a successful `create_board` reply names."""
    metadata = fmt.parse_metadata_block(_result_text(result))
    return metadata.get("board_id"), metadata.get("board_url")


async def _find_existing(
    mcp: Any, *, environment_slug: str, name: str, tag: str | None
) -> tuple[str, str | None] | None:
    """The id of a board already named `name`, or None; the url is always
    `None` here (see below), never guessed.

    Filtered by `tag` server-side when one is valid: a tagged listing that
    comes back with no matching row is read as "no board yet", not retried
    without the tag, since `run:<run_id>` is unique to this run and a real
    miss looks the same as a hidden hit either way. Without a valid tag,
    pages are read in order, matched by name on each; how many pages to read
    comes from the first page's own `Metadata:` `total_pages` (capped by
    `_MAX_LIST_PAGES_SAFETY` as a guard against a missing or absurd value,
    not the normal stop condition), not a fixed guess.

    A board found this way carries no url: `list_boards`' table has no URL
    column (confirmed against the live server, 2026-09-07), unlike a freshly
    created board where `create_board` hands one back directly. `ensure_board`
    returns `board_url=None` in that case rather than reconstructing one from
    parts this module was not given.
    """
    page = 1
    total_pages = 1
    while page <= min(total_pages, _MAX_LIST_PAGES_SAFETY):
        args: dict[str, Any] = {"environment_slug": environment_slug, "page": page}
        if tag:
            args["tags"] = [tag]
        result = await mcp.call("list_boards", args)
        if getattr(result, "is_error", False):
            return None
        text = _result_text(result)
        if page == 1:
            metadata = fmt.parse_metadata_block(text)
            try:
                total_pages = max(1, int(metadata.get("total_pages", "1")))
            except ValueError:
                total_pages = 1
        table = fmt.parse_results_table(text, heading="# Boards")
        if table is not None:
            headers, rows = table
            if "ID" in headers and "Name" in headers:
                id_idx = headers.index("ID")
                name_idx = headers.index("Name")
                for row in rows:
                    if len(row) > max(id_idx, name_idx) and row[name_idx] == name:
                        return row[id_idx], None
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
                board_id=None, board_url=None, created=False, error=_result_text(result) or None
            )
        board_id, board_url = _parse_created_board(result)
        return BoardResult(board_id=board_id, board_url=board_url, created=True)
    except Exception as exc:
        return BoardResult(
            board_id=None, board_url=None, created=False, error=f"{type(exc).__name__}: {exc}"
        )
