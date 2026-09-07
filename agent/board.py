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

R23 (EDW-1370) gives `ensure_board` an optional `trace`: `list_boards` and
`create_board` both go through `agent.telemetry.traced_call`, so they show
up as `execute_tool` spans under the handoff's trace instead of leaving no
telemetry at all. `evals/run.py`'s `_hand_off_cell` puts the board's id and
url on the trace itself once this function returns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent import format as fmt
from agent.handoff import summary_lines
from agent.report import Report
from agent.telemetry import RunTrace, disabled_run_trace, traced_call

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


class _LookupFailed:
    """`_find_existing` could not tell whether a board already exists.

    Distinct from `None` ("looked, and there is no such board"): a
    `list_boards` error (a 429, say) must not be read as "nothing found", or
    `ensure_board` would fall through to `create_board` and mint a duplicate
    mid-matrix, which is exactly what acceptance criterion 3 forbids.
    """

    def __init__(self, error: str) -> None:
        self.error = error


def _scrub_scenario_id(text: str, scenario_id: str) -> str:
    """`text` with every occurrence of `scenario_id` replaced by a placeholder.

    Telemetry only, applied to what a `create_board`/`list_boards` span
    shows (R23, EDW-1370): a board's own name carries `report.scenario_id`
    by design (`board_name`, above), so tracing those calls' real arguments
    and results verbatim would put the scenario id on a surface CLAUDE.md
    means to keep it off (Agent Timeline, readable before a run is graded).
    The real call to Honeycomb, and what `ensure_board` returns, are both
    unaffected; only the copy handed to `agent.telemetry.traced_call`'s
    `trace_args`/`redact_result` is scrubbed.
    """
    return text.replace(scenario_id, "<scenario>") if scenario_id else text


async def _find_existing(
    mcp: Any,
    *,
    environment_slug: str,
    name: str,
    tag: str | None,
    run_id: str,
    scenario_id: str,
    trace: RunTrace,
) -> tuple[str, str | None] | _LookupFailed | None:
    """The id of a board already named `name`; `None` when the listing
    completed and found nothing; a `_LookupFailed` when the listing itself
    could not be trusted. The url is always `None` on a found board (see
    below), never guessed.

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

    A listing that is not an error but whose table this function cannot
    read (unexpected headers, say, instead of the documented `ID`/`Name`)
    is different from a listing that is genuinely empty: the first page's
    own `Metadata:` `total_items` says how many boards the server thinks
    there are, and if that is greater than zero while not one row anywhere
    in the pages read could actually be checked against `name`, this
    function has no way to tell whether the board being looked for is one
    of them. Reading that as "not found", as an earlier version did, is
    indistinguishable from a real miss and lets `ensure_board` fall through
    to `create_board`, minting a duplicate; it is read as a `_LookupFailed`
    instead, the same as a `list_boards` error.
    """
    page = 1
    total_pages = 1
    total_items = 0
    rows_parsed = 0
    while page <= min(total_pages, _MAX_LIST_PAGES_SAFETY):
        args: dict[str, Any] = {"environment_slug": environment_slug, "page": page}
        if tag:
            args["tags"] = [tag]
        result = await traced_call(
            trace,
            mcp,
            "list_boards",
            args,
            f"{run_id}-list-boards-{page}",
            redact_result=lambda text: _scrub_scenario_id(text, scenario_id),
        )
        if getattr(result, "is_error", False):
            return _LookupFailed(_result_text(result) or "list_boards failed")
        text = _result_text(result)
        if page == 1:
            metadata = fmt.parse_metadata_block(text)
            try:
                total_pages = max(1, int(metadata.get("total_pages", "1")))
            except ValueError:
                total_pages = 1
            try:
                total_items = int(metadata.get("total_items", "0"))
            except ValueError:
                total_items = 0
        table = fmt.parse_results_table(text, heading="# Boards")
        if table is not None:
            headers, rows = table
            if "ID" in headers and "Name" in headers:
                id_idx = headers.index("ID")
                name_idx = headers.index("Name")
                rows_parsed += len(rows)
                for row in rows:
                    if len(row) > max(id_idx, name_idx) and row[name_idx] == name:
                        return row[id_idx], None
        page += 1
    if total_items > 0 and rows_parsed == 0:
        return _LookupFailed(
            f"list_boards reported total_items={total_items} but no row could be read "
            "against the documented ID/Name headers"
        )
    return None


async def ensure_board(
    report: Report, mcp: Any, *, environment_slug: str, trace: RunTrace | None = None
) -> BoardResult:
    """The board for this run: an existing one by name, or a freshly created one.

    Never raises: a `list_boards` or `create_board` failure comes back as a
    `BoardResult` with `created=False` and `error` set, the same "bolt-on,
    not a gate" contract `agent/handoff.py`'s `hand_off` follows, since a
    board is a courtesy on top of a graded report, not part of it. A
    `list_boards` failure specifically must not fall through to
    `create_board`: it is recorded as an error, not treated as "no board
    found yet" (see `_LookupFailed`).

    `trace` (R23, EDW-1370) is the handoff's `RunTrace`, from
    `Telemetry.start_handoff`; both `list_boards` and `create_board` go
    through `traced_call`, which wraps them in `RunTrace.tool_span`, the
    same as `agent/handoff.py`'s calls to Canvas. A disabled one when the
    caller does not open telemetry.
    """
    trace = trace or disabled_run_trace()
    name = board_name(report)
    tag = run_tag(report.run_id)
    try:
        existing = await _find_existing(
            mcp,
            environment_slug=environment_slug,
            name=name,
            tag=tag,
            run_id=report.run_id,
            scenario_id=report.scenario_id,
            trace=trace,
        )
        if isinstance(existing, _LookupFailed):
            return BoardResult(board_id=None, board_url=None, created=False, error=existing.error)
        if existing is not None:
            board_id, board_url = existing
            return BoardResult(board_id=board_id, board_url=board_url, created=False)

        create_args = {
            "environment_slug": environment_slug,
            "name": name,
            "panels": _panels(report),
            "tags": [tag] if tag else [],
        }
        result = await traced_call(
            trace,
            mcp,
            "create_board",
            create_args,
            f"{report.run_id}-create-board",
            trace_args={**create_args, "name": _scrub_scenario_id(name, report.scenario_id)},
            redact_result=lambda text: _scrub_scenario_id(text, report.scenario_id),
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
