"""Hand a filed report to Honeycomb's Canvas and record what it said back.

    handoff = await hand_off(report, mcp, environment_slug=settings.honeycomb_env)

R12 (EDW-1334). Canvas is Honeycomb's multiplayer investigation surface;
`canvas_agent_invoke` starts (or continues) a single-turn run of its own
agent there, and `canvas_agent_poll_response` is how the caller reads the
result back. Handing our report to Canvas and recording whether it agrees is
what makes the investigator a participant in Honeycomb's own model of an
agent, rather than a thing that only writes files.

This is a bolt-on, not a gate: `hand_off` runs after a report is already
graded, and nothing it does changes the report or the grade. Every exit path
returns a `Handoff`, never an exception: a Canvas error, a busy server, or the
120 second deadline expiring is a fact about this call, recorded on the
`Handoff` and handed back, not a reason to fail the run that already produced
the report being handed off.

Both `canvas_agent_invoke` and `canvas_agent_poll_response` are new as of the
management key gaining `mcp:write` on 2026-09-07 (see CLAUDE.md's Honeycomb
facts and the R12 issue notes), and neither tool's exact JSON response shape
was captured against the live server before this was written, only the
statuses and fields Honeycomb's own docs describe. `_payload` and `_field`
below read that JSON defensively, trying a short list of plausible key names
per field rather than assuming one; if the live shape uses a name outside
that list, a field comes back `None` instead of raising, and the caller
still gets a `Handoff` with whatever it found. Confirming the real key names
against a live call is the orchestrator's job, not this module's, per
CLAUDE.md's ban on live MCP calls from an implementer.

The 120 second budget is spent as up to three polls: `wait_seconds` is
capped at 50 by the server, so the loop asks for `min(50, time left)` each
time and stops the moment the deadline (measured against an injectable
`clock`, so a test can drive the whole budget without a real wait) is
passed. `canvas_agent_poll_response`'s four statuses (`completed`, `error`,
`busy`, `running`) and the deadline expiring are each their own outcome on
`Handoff.status`; `running` is the only one that loops.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.report import Report

logger = logging.getLogger(__name__)

# The whole budget, per the spec: three 40s polls or two 50s polls plus a
# short one. Measured against `clock`, not wall-clock sleep, so a test can
# exhaust it without waiting.
DEFAULT_DEADLINE_S = 120.0

# The server's own cap on one poll's wait_seconds (its default is 30).
MAX_POLL_WAIT_S = 50.0

ClockFn = Callable[[], float]

PollStatus = Literal["completed", "error", "busy", "timeout"]
"""How a hand-off ended. `completed` is the only status that carries a reply
to classify; the other three are `Handoff.classification == "no_response"`
by construction (see `classify`)."""

Classification = Literal["agree", "disagree", "extend", "no_response"]


class Handoff(BaseModel):
    """One report handed to Canvas, and what came back.

    `raw_text` is stored whatever the classification says, including a
    `no_response`, where it is `None`: there is nothing to store on a
    timeout, an error, or a busy server, and a `None` here is what tells that
    apart from a completed reply that happened to be empty text.

    `board_id` and `board_url` are filled in by the caller from
    `agent/board.py`'s `BoardResult`, not looked up here: creating the board
    and talking to Canvas are two different write calls, and a board failure
    should not stop this one from recording what Canvas said, or vice versa.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    prompt: str = Field(description="The message this session sent to Canvas.")

    status: PollStatus
    classification: Classification
    raw_text: str | None = None

    investigation_id: str | None = None
    investigation_url: str | None = None
    session_id: str | None = None

    board_id: str | None = None
    board_url: str | None = None

    error: str | None = None


# --------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------


def summary_lines(report: Report) -> list[str]:
    """The findings, as a list of plain sentences: what `hand_off` and
    `agent/board.py`'s text panel both build their rendering from.

    The top hypothesis only, since that is what Canvas is being asked to
    check; a report with more than one hypothesis still hands over just the
    one this investigation is standing behind. A report with none says so
    plainly rather than being silently empty.
    """
    lines: list[str] = []
    if report.hypotheses:
        top = report.hypotheses[0]
        lines.append(f"Top hypothesis ({top.confidence} confidence): {top.claim}")
        if top.dims:
            dims = ", ".join(f"{key}={value}" for key, value in top.dims.items())
            lines.append(f"Dimensions: {dims}")
        if top.slow_or_failing_span:
            lines.append(f"Slow or failing span: {top.slow_or_failing_span}")
        if top.evidence:
            for item in top.evidence:
                link = item.permalink or item.query_id
                lines.append(f"Evidence: {item.summary} ({link})")
        if top.negation:
            link = top.negation.permalink or top.negation.query_id
            lines.append(f"Negation: {top.negation.summary} ({link})")
        else:
            lines.append("Negation: none was run.")
    elif report.incident_present:
        lines.append("Incident present, but no hypothesis was filed.")
    else:
        lines.append("No incident found in this window.")
    if report.not_checked:
        lines.append("Not checked: " + "; ".join(report.not_checked))
    else:
        lines.append("Not checked: nothing recorded.")
    return lines


def render_message(report: Report) -> str:
    """The prompt sent to `canvas_agent_invoke`: the findings plus the ask.

    Ends with the exact question the spec asks for, on its own line, so a
    reader (and the test pinning this) can tell the findings from the ask.
    """
    return "\n".join([*summary_lines(report), "Do you agree? What would you check next?"])


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------

# Small and literal on purpose: a reader should be able to see the whole
# rule by reading these three patterns, not by tracing a scoring function.
# Word-bounded and case-insensitive so "Agreed" and "disagreement" both hit.
_DISAGREE = re.compile(
    r"\b(disagree|disagrees|disagreed|incorrect|mistaken|not\s+right|doesn't\s+hold|"
    r"does\s+not\s+hold|wrong)\b",
    re.IGNORECASE,
)
_AGREE = re.compile(
    r"\b(agree|agrees|agreed|correct|confirmed|checks\s+out|sounds\s+right|makes\s+sense)\b",
    re.IGNORECASE,
)
_EXTEND = re.compile(
    r"\b(also\s+check|i'?d\s+check|i\s+would\s+check|next\s+i'?d|consider\s+checking|"
    r"worth\s+checking|you\s+should\s+also)\b",
    re.IGNORECASE,
)


def classify(text: str) -> Classification:
    """Agree, disagree, or extend, by the first of `_DISAGREE`, `_AGREE`, and
    `_EXTEND` (in that order) to match anywhere in `text`.

    Disagree is checked first: a reply that hedges an agreement with a
    disagreement ("I agree the span is right, but I disagree on the region")
    is read as a disagreement, since missing one is the worse mistake for
    what this classifier is for. A reply that matches neither agree nor
    disagree, but has some text, is `extend`: the question always asks what
    to check next, so a reply that answers only that part, without taking a
    side, still counts as Canvas engaging rather than as no answer. Empty or
    whitespace-only text is `no_response`, the same as no reply at all.
    """
    if _DISAGREE.search(text):
        return "disagree"
    if _AGREE.search(text):
        return "agree"
    if text.strip():
        return "extend"
    return "no_response"


# --------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------


def _payload(result: Any) -> dict[str, Any]:
    """A tool result's structured payload as a dict, however it arrived.

    `HoneycombMCP.call` puts the server's `structured_content` in `.raw`
    when it sent any, otherwise the joined text (see `agent/mcp_client.py`).
    Canvas's write tools are read the same defensive way: a dict `.raw` is
    used as is; a string `.raw` is tried as JSON; anything else, or JSON
    that is not an object, is an empty dict rather than a crash.
    """
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


def _field(payload: dict[str, Any], *names: str) -> str | None:
    """The first truthy value in `payload` among `names`, stringified, or None."""
    for name in names:
        value = payload.get(name)
        if value:
            return str(value)
    return None


def _no_response(
    run_id: str,
    prompt: str,
    status: PollStatus,
    *,
    investigation_id: str | None = None,
    investigation_url: str | None = None,
    session_id: str | None = None,
    board_id: str | None = None,
    board_url: str | None = None,
    error: str | None = None,
) -> Handoff:
    return Handoff(
        run_id=run_id,
        prompt=prompt,
        status=status,
        classification="no_response",
        investigation_id=investigation_id,
        investigation_url=investigation_url,
        session_id=session_id,
        board_id=board_id,
        board_url=board_url,
        error=error,
    )


async def hand_off(
    report: Report,
    mcp: Any,
    *,
    board_id: str | None = None,
    board_url: str | None = None,
    title: str | None = None,
    deadline_s: float = DEFAULT_DEADLINE_S,
    clock: ClockFn = time.monotonic,
) -> Handoff:
    """Send `report`'s findings to Canvas and poll for its reply.

    `mcp` is anything with an async `call(name, args)` returning an object
    with `.raw` and `.is_error` (a `HoneycombMCP` opened with
    `allow_write=True` in production; a fake in the tests). `board_id` and
    `board_url` are carried straight onto the returned `Handoff`; this
    function does not create the board (`agent/board.py` does).

    Never raises. Every failure mode this function can hit on its own
    (a call that raises, a status the docs do not name, the deadline
    expiring) becomes a `Handoff` with `classification == "no_response"`
    and, where there is one, a message in `error`.
    """
    prompt = render_message(report)
    deadline = clock() + deadline_s

    try:
        invoke = await mcp.call(
            "canvas_agent_invoke",
            {"prompt": prompt, "title": title or f"receipts {report.scenario_id} {report.run_id}"},
        )
    except Exception as exc:
        logger.warning("canvas_agent_invoke failed for %s: %s", report.run_id, exc)
        return _no_response(
            report.run_id,
            prompt,
            "error",
            board_id=board_id,
            board_url=board_url,
            error=f"{type(exc).__name__}: {exc}",
        )

    payload = _payload(invoke)
    investigation_id = _field(payload, "investigation_id")
    investigation_url = _field(payload, "investigation_url")
    session_id = _field(payload, "session_id")
    status = _field(payload, "status")

    if getattr(invoke, "is_error", False):
        return _no_response(
            report.run_id,
            prompt,
            "error",
            investigation_id=investigation_id,
            investigation_url=investigation_url,
            board_id=board_id,
            board_url=board_url,
            error=_field(payload, "message") or getattr(invoke, "text", None),
        )
    if status == "busy":
        return _no_response(
            report.run_id,
            prompt,
            "busy",
            investigation_id=investigation_id,
            investigation_url=investigation_url,
            board_id=board_id,
            board_url=board_url,
            error=_field(payload, "message"),
        )
    if status != "running" or not session_id:
        return _no_response(
            report.run_id,
            prompt,
            "error",
            investigation_id=investigation_id,
            investigation_url=investigation_url,
            board_id=board_id,
            board_url=board_url,
            error=f"unexpected canvas_agent_invoke status {status!r}",
        )

    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return _no_response(
                report.run_id,
                prompt,
                "timeout",
                investigation_id=investigation_id,
                investigation_url=investigation_url,
                session_id=session_id,
                board_id=board_id,
                board_url=board_url,
            )
        wait_seconds = max(1, min(int(MAX_POLL_WAIT_S), int(remaining)))
        try:
            poll = await mcp.call(
                "canvas_agent_poll_response",
                {
                    "investigation_id": investigation_id,
                    "session_id": session_id,
                    "wait_seconds": wait_seconds,
                },
            )
        except Exception as exc:
            logger.warning("canvas_agent_poll_response failed for %s: %s", report.run_id, exc)
            return _no_response(
                report.run_id,
                prompt,
                "error",
                investigation_id=investigation_id,
                investigation_url=investigation_url,
                session_id=session_id,
                board_id=board_id,
                board_url=board_url,
                error=f"{type(exc).__name__}: {exc}",
            )

        poll_payload = _payload(poll)
        poll_status = _field(poll_payload, "status")

        if getattr(poll, "is_error", False) or poll_status == "error":
            return _no_response(
                report.run_id,
                prompt,
                "error",
                investigation_id=investigation_id,
                investigation_url=investigation_url,
                session_id=session_id,
                board_id=board_id,
                board_url=board_url,
                error=_field(poll_payload, "message") or getattr(poll, "text", None),
            )
        if poll_status == "busy":
            return _no_response(
                report.run_id,
                prompt,
                "busy",
                investigation_id=investigation_id,
                investigation_url=investigation_url,
                session_id=session_id,
                board_id=board_id,
                board_url=board_url,
                error=_field(poll_payload, "message"),
            )
        if poll_status == "completed":
            raw_text = _field(poll_payload, "response", "reply", "message", "text", "content") or ""
            return Handoff(
                run_id=report.run_id,
                prompt=prompt,
                status="completed",
                classification=classify(raw_text),
                raw_text=raw_text,
                investigation_id=investigation_id,
                investigation_url=investigation_url,
                session_id=session_id,
                board_id=board_id,
                board_url=board_url,
            )
        # "running", or a status the docs do not name: keep polling. The
        # deadline check at the top of the loop is what stops this from
        # spinning on an unrecognized status forever.
