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

Both `canvas_agent_invoke` and `canvas_agent_poll_response` need `mcp:write`
on the key (see CLAUDE.md's Honeycomb facts). Canvas itself needs an OAuth session, not the
management key: `canvas_agent_invoke` under the key fails with
`actor_user_hcid is required`, since a management key has no user actor
(verified live 2026-09-07; `agent/auth.py` and `agent/mcp_client.py`'s
`_open_streams` are what select OAuth). Both tools return JSON text, and both
were captured live: `canvas_agent_invoke`'s success payload carries `status`,
`investigation_id`, `investigation_url`, `session_id`, and
`investigation_created` (a bool Honeycomb's own docs do not describe);
`canvas_agent_poll_response`'s completed payload carries `status` and the
reply under `chat`, not `response`, and a reader that tried `response` first
would take every real reply as empty. `_payload` and `_field` below still
read that JSON a little defensively, in case a future server version adds a
field under a different name, but `chat` is tried first for the reply and
the rest of the candidate list is a fallback.
Sanitized fixtures of both live captures are in `tests/fixtures/mcp/`.

The `DEFAULT_DEADLINE_S` budget (300s) is spent as up to six polls:
`wait_seconds` is capped at `MAX_POLL_WAIT_S` (50, the server's own cap) so
the loop asks for `min(50, time left)` each time and stops the moment the
deadline (measured against an injectable `clock`, so a test can drive the
whole budget without a real wait) is passed. `canvas_agent_poll_response`'s
four statuses (`completed`, `error`, `busy`, `running`) and the deadline
expiring are each their own outcome on `Handoff.status`. `running` and any
status the docs do not name are both repolled the same way: the server's
long poll is documented to spend up to `wait_seconds` getting to a
`running` answer, but nothing here trusts that claim: the server has
answered `running` instantly, and a loop that repolled on every such answer
would spend the team's whole rate-limit window doing so. The loop instead
measures
the elapsed time around the call (against the same injectable `clock`) and
sleeps (through an injectable `sleep`, defaulting to `asyncio.sleep`) only
what is left of `wait_seconds`, so the repoll rate stays capped regardless
of whether the server actually held the connection.

`hand_off` takes an optional `trace` (a `RunTrace` from
`Telemetry.start_handoff`): both MCP calls this function makes go through
`traced_call`, which wraps them in `RunTrace.tool_span`, the same
`execute_tool` span shape `agent/loop.py`'s own calls get, so the exchange
with Canvas shows up in Agent Timeline. `evals/run.py`'s `_hand_off_cell`
still writes the classification, the reply, and the board's id and url onto
the trace itself once this function returns; see `RunTrace.end_with_handoff`.
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
from agent.telemetry import RunTrace, disabled_run_trace, traced_call

logger = logging.getLogger(__name__)

# The whole budget. Handed a real report, the Canvas agent runs its own
# investigation (schema discovery, a BubbleUp, a breakdown, a trace) and has
# taken 182 seconds to answer (measured 2026-09-07). A budget of 120 times
# out with that investigation still running, which scores as no_response
# and reads as Canvas having nothing to say, the opposite of what happened.
# 300 covers the measurement with headroom. A smoke-test prompt still comes back in
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
# rule by reading these four patterns and the loop in `classify` below, not
# by tracing a scoring function.
#
# A first version of this classifier matched `_AGREE`/`_DISAGREE` directly
# against the whole reply, with one extra pattern for an agreement word
# sitting immediately next to a negation word ("I do not agree"). That
# pattern only fired when the two words were adjacent: "I wouldn't agree",
# "I'm not sure I agree", "I don't fully agree", "not entirely correct", and
# "hardly correct" all still read as `agree`, because nothing sat between
# the negation and the agreement word close enough for a fixed adjacency
# pattern to catch, and the mistake only runs one direction (toward
# inflating "agree"), which is exactly the number this work reports.
#
# The fix drops adjacency entirely: each clause is checked for an agreement
# word, a disagreement word, and a negation token independently, and the
# three are combined by `classify`, not by a fourth regex.
_CLAUSE_BOUNDARY = re.compile(r"[.;,]|\b(?:but|however|although|though)\b", re.IGNORECASE)

_AGREE_WORD = re.compile(
    r"\b(?:agrees?|agreed|agreement|correct|confirmed|confirm|checks?\s+out|"
    r"sounds?\s+right|makes?\s+sense|holds?)\b",
    re.IGNORECASE,
)
_DISAGREE_WORD = re.compile(
    r"\b(?:disagrees?|disagreed|disagreement|incorrect|mistaken|wrong)\b",
    re.IGNORECASE,
)
# The negation tokens the spec calls for, plus `n't` matched without a
# leading `\b`: the apostrophe already sits between two word characters in
# a contraction ("doesn't", "isn't", "wouldn't"), so there is no word
# boundary immediately before the `n` for `\b` to anchor on.
_NEGATION = re.compile(
    r"\b(?:not|never|hardly|barely|scarcely|unable|partially|unsure|unconvinced)\b"
    r"|n't\b|\bfar\s+from\b",
    re.IGNORECASE,
)


def _clauses(text: str) -> list[str]:
    """`text` split on sentence and clause boundaries, empty pieces dropped."""
    return [clause for clause in _CLAUSE_BOUNDARY.split(text) if clause.strip()]


def classify(text: str) -> Classification:
    """`disagree` if any clause is a clean disagreement, else `agree` if any
    clause is a clean agreement, else `extend` for anything else non-empty,
    else `no_response` for empty or whitespace-only text.

    A clause is read independently of every other clause in `text`, and
    each clause is checked for three things: an `_AGREE_WORD`, a
    `_DISAGREE_WORD`, and a `_NEGATION` token, all three searched for
    anywhere in the clause rather than next to each other. A clause counts
    as a clean disagreement when it has a disagreement word and no negation
    ("this is wrong"), or an agreement word with a negation ("I wouldn't
    agree", "hardly correct", "not entirely correct": the negation and the
    agreement word do not have to be adjacent, which is the fix over the
    version that missed all of those). A clause counts as a clean agreement
    only when it has an agreement word and no negation at all.

    A clause with both a disagreement word and a negation ("not incorrect",
    "I do not disagree") is neither: it is a double negative, and a keyword
    rule has no way to tell whether the negation cancels the disagreement
    word, weakens it, or was aimed at something else in the clause entirely.
    Reading it as `agree` (the literal double-negative meaning) would put a
    guess into a number this work reports as observed fact; reading it as
    `extend` costs nothing but a slightly lower agree/disagree count, so
    that is the deliberate choice here, not an oversight.

    `disagree` beats `agree` across the whole reply, not just within one
    clause: a reply that hedges an agreement with a disagreement ("I agree
    the span is right, but I disagree on the region") is read as a
    disagreement, since missing one is the worse mistake for what this
    classifier is for. There is no separate keyword pattern for `extend`:
    the question always asks what to check next, so any reply that takes
    neither side but still has content, whether it names something to check
    or not (a live Canvas reply can be pure commentary with no concrete next
    step named at all), counts as Canvas engaging rather than as no answer.
    """
    clean_disagree = False
    clean_agree = False
    for clause in _clauses(text):
        has_agree = bool(_AGREE_WORD.search(clause))
        has_disagree = bool(_DISAGREE_WORD.search(clause))
        has_negation = bool(_NEGATION.search(clause))
        if has_disagree and not has_negation:
            clean_disagree = True
        elif has_agree and has_negation:
            clean_disagree = True
        elif has_agree and not has_negation:
            clean_agree = True
        # `has_disagree and has_negation`: the double negative above, which
        # contributes to neither count on purpose.
    if clean_disagree:
        return "disagree"
    if clean_agree:
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
    trace: RunTrace | None = None,
) -> Handoff:
    """Send `report`'s findings to Canvas and poll for its reply.

    `mcp` is anything with an async `call(name, args)` returning an object
    with `.raw` and `.is_error` (a `HoneycombMCP` opened with
    `allow_write=True` in production; a fake in the tests). `board_id` and
    `board_url` are carried straight onto the returned `Handoff`; this
    function does not create the board (`agent/board.py` does). `sleep` is
    injectable so a test can drive the whole deadline without a real wait,
    the same reason `clock` is. `trace` is the handoff's own `RunTrace`
    (from `Telemetry.start_handoff`, R23); a disabled one when the caller
    does not open telemetry, the same default `agent/loop.py`'s
    `investigate` uses for the investigation's own trace.

    Never raises. Every failure mode this function can hit on its own
    (a call that raises, a status the docs do not name, the deadline
    expiring) becomes a `Handoff` with `classification == "no_response"`
    and, where there is one, a message in `error`.
    """
    trace = trace or disabled_run_trace()
    prompt = render_message(report)
    deadline = clock() + deadline_s

    try:
        invoke = await traced_call(
            trace,
            mcp,
            "canvas_agent_invoke",
            # `title` never carries `report.scenario_id`: CLAUDE.md keeps
            # `scenario.id` off the wire because its values read as
            # answers, and Canvas is the one agent whose reply gets scored
            # as a verdict on this investigation, so it is the last place
            # that answer should leak. The same reasoning is why nothing
            # here, or in `RunTrace.end_with_handoff`, ever sets it as a
            # span attribute either.
            {"prompt": prompt, "title": title or f"receipts {report.run_id}"},
            f"{report.run_id}-canvas-invoke",
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

    poll_count = 0
    while True:
        poll_started = clock()
        remaining = deadline - poll_started
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
        poll_count += 1
        try:
            poll = await traced_call(
                trace,
                mcp,
                "canvas_agent_poll_response",
                {
                    "investigation_id": investigation_id,
                    "session_id": session_id,
                    "wait_seconds": wait_seconds,
                },
                f"{report.run_id}-canvas-poll-{poll_count}",
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
        # Neither terminal ("completed", "error", "busy") nor a fast enough
        # answer to skip sleeping: "running" and any status the docs do not
        # name are handled the same way. The server's own long poll is
        # documented to hold the connection for `wait_seconds` on a
        # "running" reply, but nothing here measures that, and a fake (or a
        # real server under load) can answer "running" immediately; treating
        # "running" as already having spent the wait repolls as fast as this
        # loop and the MCP client's own pacing allow, which is enough to spend
        # a whole team-wide rate-limit window inside one 300s budget.
        # Measuring the
        # elapsed time around the call and sleeping the rest of `wait_seconds`
        # keeps the repoll rate capped regardless of what the server actually
        # honored. The deadline check at the top of the loop is still what
        # stops this from polling forever; this sleep only paces it.
        elapsed = clock() - poll_started
        await sleep(max(0.0, wait_seconds - elapsed))
