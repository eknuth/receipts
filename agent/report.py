"""The report the investigator produces, and the tool log it is checked against.

Two shapes live here. `ReportDraft` is what the model fills in through the
`submit_report` tool: the findings and nothing else. `Report` is what the run
writes to disk: the draft plus the process fields the loop measured (calls,
tokens, cost, wall time) and the tool log every claim is validated against.

The split matters. A model can write any number into a field called
`tool_calls`, so the model never gets to. Everything countable is counted by
`agent/loop.py` from what actually happened, and `agent/validate.py` checks
the draft's claims against the log rather than trusting them.

`baseline_evidence` is not in the issue's sketch of the schema. It is here
because a report saying nothing happened has to carry receipts too, and the
per-hypothesis evidence has nowhere to live when there are no hypotheses.

Confidence borrows the vocabulary from `charles/api/src/services/fact-checker.ts`:

  high    grounded. The claim is carried by the rows of a query that was run,
          and the negation query was run and came back the way the claim needs.
  medium  partial. The evidence points this way but goes further than the rows
          strictly support, or the negation is weaker than the claim.
  low     unsupported by anything decisive. Worth writing down, not worth acting
          on without another query.

The grader punishes a confident wrong answer harder than a hedged one, so the
levels have to mean something.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

Confidence = Literal["high", "medium", "low"]

# Tools whose result carries an identifier a hypothesis may cite as evidence.
# `run_query` is the one that returns rows; the other two ride on a query.
EVIDENCE_TOOLS: frozenset[str] = frozenset({"run_query", "run_bubbleup", "get_trace"})

# The tool a piece of evidence has to come from to count as rows on the record.
PRIMARY_EVIDENCE_TOOL = "run_query"


def _coerce_json_container(value: Any, expected: type, info: ValidationInfo) -> Any:
    """Accept a JSON-encoded string in place of a list or a dict.

    A live run had a model call `submit_report` with `hypotheses` as the
    JSON text of a list (`'[{"claim": ...}]'`) rather than a list. The call
    budget was already spent by the time that was rejected, so an
    investigation that had found the answer filed nothing. This decodes a
    string field before pydantic's own type check runs, but only when it
    parses as JSON and the result is the container the field expects; any
    other string, or JSON that decodes to the wrong shape (an object where a
    list was wanted), falls through unchanged and fails validation the
    normal way. When `model_validate` is called with a `context` dict, the
    field name is appended to `context["coerced_fields"]` so a run that
    needed this is visible on the report rather than silent.
    """
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except ValueError:
        return value
    if not isinstance(decoded, expected):
        return value
    if info.context is not None:
        info.context.setdefault("coerced_fields", []).append(info.field_name)
    return decoded


class Evidence(BaseModel):
    """One query that was run, and what its rows showed.

    `query_id` is the identifier the Honeycomb MCP returned for a query run.
    A `query_id` that is not in this run's tool log is rejected: the report
    may only cite work that happened.
    """

    model_config = ConfigDict(extra="forbid")

    query_id: str = Field(
        description=(
            "The query_id from a tool result in this run. Copy it exactly. "
            "A query_id that was not returned by a call you made is rejected."
        )
    )
    summary: str = Field(
        description="One line saying what the rows of that query showed. Numbers, not adjectives."
    )
    permalink: str | None = Field(
        default=None, description="The permalink from the same tool result, when it carried one."
    )


class Hypothesis(BaseModel):
    """One candidate explanation, with the queries that back it."""

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(description="What you believe happened, in one or two sentences.")
    dims: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "The dimensions that select the affected population, as column to value. "
            "Use the column names the dataset actually has."
        ),
    )
    slow_or_failing_span: str | None = Field(
        default=None,
        description="The span name that got slow or started failing, if you found one.",
    )
    confidence: Confidence = Field(
        description=(
            "high when the rows carry the claim and the negation query came back clean, "
            "medium when the evidence points this way but goes further than the rows show, "
            "low when nothing decisive supports it yet."
        )
    )
    evidence: list[Evidence] = Field(
        default_factory=list,
        description=(
            "At least one query that was run, including one run_query, whose rows carry the claim."
        ),
    )
    negation: Evidence | None = Field(
        default=None,
        description=(
            "The query that tested the claim by excluding it: the same measurement "
            "WHERE NOT the claimed dimensions. It must have been run."
        ),
    )

    @field_validator("dims", mode="before")
    @classmethod
    def _coerce_dims(cls, value: Any, info: ValidationInfo) -> Any:
        return _coerce_json_container(value, dict, info)

    @field_validator("evidence", mode="before")
    @classmethod
    def _coerce_evidence(cls, value: Any, info: ValidationInfo) -> Any:
        return _coerce_json_container(value, list, info)

    @field_validator("negation", mode="before")
    @classmethod
    def _coerce_negation(cls, value: Any, info: ValidationInfo) -> Any:
        return _coerce_json_container(value, dict, info)


class RejectedCandidate(BaseModel):
    """Something that looked like the cause and was ruled out.

    Without this there is no place to put a candidate that was examined and
    found not to be the incident. A model that measures a suspicious number,
    checks it, and concludes it was already there had two options: drop the
    finding, or report it as an incident. Neither is the truth.
    """

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(description="What you considered and did not report, in one sentence.")
    dims: dict[str, str] = Field(
        default_factory=dict,
        description="The dimensions that selected it, as column to value, if it had any.",
    )
    reason: str = Field(
        description=(
            "Why it is not the incident. For example that it was already at that level "
            "before anything changed."
        )
    )
    evidence: list[Evidence] = Field(
        default_factory=list,
        description="The query that ruled it out. Same rules as any other citation.",
    )


class ReportDraft(BaseModel):
    """What the model submits. The process fields are not its to write."""

    model_config = ConfigDict(extra="forbid")

    incident_present: bool = Field(
        description="Whether anything in this window is an incident. False is a real answer."
    )
    hypotheses: list[Hypothesis] = Field(
        default_factory=list, description="Ranked, best first. Empty when there is no incident."
    )
    affected_population: str | None = Field(
        default=None,
        description="Who was affected and how much of the traffic, with the number you measured.",
    )
    onset_estimate: datetime | None = Field(
        default=None, description="When the change started, ISO-8601 UTC. Null if nothing changed."
    )
    not_checked: list[str] = Field(
        default_factory=list,
        description=(
            "Dimensions, services, spans, and time windows that were in scope and that you "
            "did not query. Every entry must name something absent from your tool calls."
        ),
    )
    baseline_evidence: list[Evidence] = Field(
        default_factory=list,
        description=(
            "Queries that establish what the window looked like before whatever you are "
            "reporting. Required either way: at least one run_query. When there is no "
            "incident, rows showing the measurement flat from the start of the window to "
            "the end. When there is one, rows showing the level it was at beforehand, "
            "which is what makes the incident a change rather than a level."
        ),
    )
    rejected_candidates: list[RejectedCandidate] = Field(
        default_factory=list,
        description=(
            "Things you looked at and ruled out. Not required, and not a place for "
            "everything you did not check, which is what not_checked is for."
        ),
    )

    @field_validator(
        "hypotheses", "not_checked", "baseline_evidence", "rejected_candidates", mode="before"
    )
    @classmethod
    def _coerce_lists(cls, value: Any, info: ValidationInfo) -> Any:
        return _coerce_json_container(value, list, info)


class ToolCall(BaseModel):
    """One MCP call the loop made, as the record everything is checked against."""

    model_config = ConfigDict(extra="forbid")

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    query_id: str | None = None
    permalink: str | None = None
    is_error: bool = False
    t: float = 0.0
    """Seconds since the run started."""


class Report(BaseModel):
    """One investigation, findings and process both."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    scenario_id: str
    provider: str
    model: str

    incident_present: bool = False
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    affected_population: str | None = None
    onset_estimate: datetime | None = None
    not_checked: list[str] = Field(default_factory=list)
    baseline_evidence: list[Evidence] = Field(default_factory=list)
    rejected_candidates: list[RejectedCandidate] = Field(default_factory=list)

    tool_calls: int = 0
    model_turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    wall_s: float = 0.0
    cost_usd: float = 0.0
    tool_log: list[ToolCall] = Field(default_factory=list)

    stop_reason: str = "unknown"
    """Why the loop ended: report, call_cap, wall_cap, model_stopped, or error."""

    model_stop_reason: str | None = None
    """What the provider said about the last turn, such as `refusal` or
    `max_tokens`. Kept separate from `stop_reason`: a refusal and a model that
    simply stopped calling tools both end the loop the same way, and the
    record should still tell them apart."""

    validation_failed: bool = False
    validation_messages: list[str] = Field(default_factory=list)
    error: str | None = None

    @classmethod
    def from_draft(cls, draft: ReportDraft, **fields: Any) -> Report:
        """A report carrying a draft's findings plus the process fields."""
        return cls(**draft.model_dump(), **fields)

    def write(self, results_dir: Path, *, overwrite: bool = False) -> Path:
        """Write the report under `<results_dir>/<run_id>/` and return the path.

        The first run of a run id writes `report.json`. A second writes
        `report-2.json`, and so on, because repeats of the same scenario share
        a run id and silently overwriting them loses the spread that repeats
        exist to measure. Pass `overwrite` to keep the plain name.
        """
        directory = results_dir / self.run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "report.json"
        if not overwrite:
            attempt = 2
            while path.exists():
                path = directory / f"report-{attempt}.json"
                attempt += 1
        return self.write_to(path)

    def write_to(self, path: Path) -> Path:
        """Write the report to exactly `path`. The eval runner chooses its own layout."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n")
        return path


def submit_report_schema() -> dict[str, Any]:
    """The JSON schema for the `submit_report` tool, generated from `ReportDraft`.

    Generated rather than written out, so the tool the model sees and the model
    the code validates against cannot drift apart.
    """
    schema = ReportDraft.model_json_schema()
    schema.pop("title", None)
    return schema


def load_report(path: Path) -> Report:
    """Read a report back off disk."""
    return Report.model_validate(json.loads(path.read_text()))
