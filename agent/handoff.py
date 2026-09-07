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
returns a `Handoff`, never an exception: a Canvas error, a busy server, or
the `DEFAULT_DEADLINE_S` deadline expiring is a fact about this call,
recorded on the `Handoff` and handed back, not a reason to fail the run that
already produced the report being handed off.

Both `canvas_agent_invoke` and `canvas_agent_poll_response` are new as of the
management key gaining `mcp:write` on 2026-09-07 (see CLAUDE.md's Honeycomb
facts and the R12 issue notes). Canvas itself needs an OAuth session, not the
management key: `canvas_agent_invoke` under the key fails with
`actor_user_hcid is required`, since a management key has no user actor
(verified live 2026-09-07; `agent/auth.py` and `agent/mcp_client.py`'s
`_open_streams` are what select OAuth). Both tools return JSON text, and both
were captured live: `canvas_agent_invoke`'s success payload carries `status`,
`investigation_id`, `investigation_url`, `session_id`, and
`investigation_created` (a bool, new information over what Honeycomb's own
docs describe); `canvas_agent_poll_response`'s completed payload carries
`status` and the reply under `chat`, not `response` (an earlier draft of this
module guessed `response` first and never saw `chat` at all, which would
have read every real reply as empty). `_payload` and `_field` below still
read that JSON a little defensively, in case a future server version adds a
field under a different name, but `chat` is tried first for the reply and
the rest of the candidate list is now a fallback rather than a guess.
Sanitized fixtures of both live captures are in `tests/fixtures/mcp/`.

The `DEFAULT_DEADLINE_S` budget (300s) is spent as up to six polls:
`wait_seconds` is capped at `MAX_POLL_WAIT_S` (50, the server's own cap) so
the loop asks for `min(50, time left)` each time and stops the moment the
deadline (measured against an injectable `clock`, so a test can drive the
whole budget without a real wait) is passed. `canvas_agent_poll_response`'s
four statuses (`completed`, `error`, `busy`, `running`) and the deadline
expiring are each their own outcome on `Handoff.status`; `running` is the
only one the loop repolls without waiting further, since the server's own
long poll already spent up to `wait_seconds` getting to that answer. A
status the docs do not name is different: nothing says the server honored
`wait_seconds` for it, and a live run against one answered back instantly,
so the loop slept (through an injectable `sleep`, defaulting to
`asyncio.sleep`) between repolls rather than hammering the server at
whatever pace the caller's own MCP client would allow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.report import Report

logger = logging.getLogger(__name__)

# The whole budget. The issue said 120 seconds, and a live run on 2026-09-07
# showed that is not enough: handed a real report, the Canvas agent runs its
# own investigation (schema discovery, a BubbleUp, a cart-size breakdown, a
# trace) and took 182 seconds to answer. At 120 it timed out with the
# investigation still running, which scores as no_response and reads as
# Canvas having nothing to say, the opposite of what happened. 300 covers
# that measurement with headroom. A smoke-test prompt still comes back in
# seconds, since the loop returns on the first completed poll and does not
# wait out the budget. Measured against `clock`, not wall-clock sleep, so a
# test can exhaust it without waiting.
DEFAULT_DEADLINE_S = 300.0

# The server's own cap on one poll's wait_seconds (its default is 30).
MAX_POLL_WAIT_S = 50.0

ClockFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]

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

    `investigation_created` is `canvas_agent_invoke`'s own bool, carried
    through unchanged: `True` when this call started a new Canvas
    investigation, `False` when it continued an existing one, `None` when
    the field never arrived (every no-response path before an `invoke` reply
    was parsed at all, or a server version that omits it).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    prompt: str = Field(description="The message this session sent to Canvas.")

    status: PollStatus
    classification: Classification
    raw_text: str | None = None

    investigation_id: str | None = None
    investigation_url: str | None = None
    investigation_created: bool | None = None
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
# A negated form of an agreement word ("I do not agree", "can't confirm
# this", "doesn't check out") is a disagreement, but `_AGREE` below has no
# way to tell "agree" apart from "not agree" inside its own match: it just
# finds "agree". This pattern catches the negation first, so it has to be
# checked before `_AGREE`, the same as `_DISAGREE` is.
_NEGATED_AGREE = re.compile(
    r"\b(?:don't|do\s+not|doesn't|does\s+not|can't|cannot|not)\s+"
    r"(?:agree|correct|confirm(?:ed)?|check(?:s)?\s+out|make(?:s)?\s+sense|hold|right)\b",
    re.IGNORECASE,
)
_AGREE = re.compile(
    r"\b(agree|agrees|agreed|correct|confirmed|checks\s+out|sounds\s+right|makes\s+sense)\b",
    re.IGNORECASE,
)


def classify(text: str) -> Classification:
    """Agree or disagree, by the first of `_DISAGREE`/`_NEGATED_AGREE` and
    `_AGREE` to match anywhere in `text`; `extend` for anything else
    non-empty; `no_response` for empty or whitespace-only text.

    Disagree is checked first: a reply that hedges an agreement with a
    disagreement ("I agree the span is right, but I disagree on the region")
    is read as a disagreement, since missing one is the worse mistake for
    what this classifier is for. `_NEGATED_AGREE` is checked alongside
    `_DISAGREE`, not folded into `_AGREE`: a bare `_AGREE` search over "I do
    not agree with this" still finds "agree" and would read it as
    agreement, missing the "not" in front of it entirely. There is no
    separate keyword pattern for `extend`: the question always asks what to
    check next, so any reply that takes neither side but still has content,
    whether it names something to check or not (a live Canvas reply can be
    pure commentary with no concrete next step named at all), counts as
    Canvas engaging rather than as no answer. An earlier version of this
    function kept an unused `_EXTEND` pattern that its own docstring claimed
    was consulted; matching on it here would have missed real replies like
    that.
    """
    if _DISAGREE.search(text) or _NEGATED_AGREE.search(text):
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


def _bool_field(payload: dict[str, Any], name: str) -> bool | None:
    """`payload[name]` if it is a JSON bool, else None (missing, or some other type)."""
    value = payload.get(name)
    return value if isinstance(value, bool) else None


def _no_response(
    run_id: str,
    prompt: str,
    status: PollStatus,
    *,
    investigation_id: str | None = None,
    investigation_url: str | None = None,
    investigation_created: bool | None = None,
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
        investigation_created=investigation_created,
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
    sleep: SleepFn = asyncio.sleep,
) -> Handoff:
    """Send `report`'s findings to Canvas and poll for its reply.

    `mcp` is anything with an async `call(name, args)` returning an object
    with `.raw` and `.is_error` (a `HoneycombMCP` opened with
    `allow_write=True` in production; a fake in the tests). `board_id` and
    `board_url` are carried straight onto the returned `Handoff`; this
    function does not create the board (`agent/board.py` does). `sleep` is
    injectable so a test can drive the whole deadline without a real wait,
    the same reason `clock` is.

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
            # `title` never carries `report.scenario_id`: CLAUDE.md keeps
            # `scenario.id` off the wire because its values read as
            # answers, and Canvas is the one agent whose reply gets scored
            # as a verdict on this investigation, so it is the last place
            # that answer should leak.
            {"prompt": prompt, "title": title or f"receipts {report.run_id}"},
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
    investigation_created = _bool_field(payload, "investigation_created")
    session_id = _field(payload, "session_id")
    status = _field(payload, "status")

    if getattr(invoke, "is_error", False):
        return _no_response(
            report.run_id,
            prompt,
            "error",
            investigation_id=investigation_id,
            investigation_url=investigation_url,
            investigation_created=investigation_created,
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
            investigation_created=investigation_created,
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
            investigation_created=investigation_created,
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
                investigation_created=investigation_created,
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
                investigation_created=investigation_created,
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
                investigation_created=investigation_created,
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
                investigation_created=investigation_created,
                session_id=session_id,
                board_id=board_id,
                board_url=board_url,
                error=_field(poll_payload, "message"),
            )
        if poll_status == "completed":
            # `chat` is the live server's real field name (verified
            # 2026-09-07; see the module docstring). The rest of the list is
            # a fallback for a server version that names it differently, not
            # a guess about the current one.
            raw_text = (
                _field(poll_payload, "chat", "response", "reply", "message", "text", "content")
                or ""
            )
            return Handoff(
                run_id=report.run_id,
                prompt=prompt,
                status="completed",
                classification=classify(raw_text),
                raw_text=raw_text,
                investigation_id=investigation_id,
                investigation_url=investigation_url,
                investigation_created=investigation_created,
                session_id=session_id,
                board_id=board_id,
                board_url=board_url,
            )
        if poll_status != "running":
            # A status the docs do not name. The server's own long poll is
            # documented for "running" only, so nothing here says this one
            # waited `wait_seconds` before answering; a live one came back
            # instantly. Sleeping the same `wait_seconds` this poll asked
            # for keeps the repoll rate the caller would have gotten from a
            # real "running" wait, instead of repolling as fast as this
            # loop and the MCP client's own pacing allow: a fake server
            # that always answers an unnamed status burned through a
            # whole team-wide rate-limit window this way before this sleep
            # was added (200 polls in one 300s budget).
            await sleep(wait_seconds)
        # "running": the deadline check at the top of the loop is what
        # stops this from polling forever, not a client-side sleep, since
        # the server's own wait already spent the time.
