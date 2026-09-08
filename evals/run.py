"""Run the eval matrix: scenarios x configs x repeats, one report and one grade per run.

    uv run python -m evals.run --scenarios all --configs full,no-negation --repeats 3
    uv run python -m evals.run --scenarios all --configs full,no-negation --repeats 5 --resume
    uv run python -m evals.run --scenarios payments-stripe-v251-uswest,control-quiet --emit
    uv run python -m evals.run --regrade

`--regrade` rebuilds every `grade.json` under `--results-dir` from its stored
`report.json` and exits, with no `--scenarios` or `--configs`, no
investigation, and nothing spent: it is what a change to `evals/grader.py`
calls for, since the reports it grades did not change. See `regrade`.

Each investigation writes `evals/results/<config>/<scenario>/<n>/report.json`
and `grade.json`, and `evals/report.py` renders `evals/report.md` from the
grades alone. One emit per scenario serves every config and repeat, so the
spread across repeats is the agent's, not the data's.

Layout. `agent/__main__.py` writes a single run to
`evals/results/<run_id>/report.json` and keeps doing so. The runner files by
config, scenario, and repeat instead, because that is how the report reads
them, and a run id is shared across every cell of the matrix. The two layouts
share the directory and do not collide: a run id starts with `run-` and a
config does not. `<n>` is the next free repeat number, so running the matrix
twice appends repeats rather than replacing them, the same choice
`Report.write` makes with `report-2.json`. Delete a scenario's directory to
start it over.

`--resume` fills a matrix's repeats 1 through N and skips any cell that
already holds a `grade.json`, printing one line per skipped cell; a crash
row counts as done and is skipped too (delete the directory to retry it). A
cell with a `report.json` but no `grade.json` was paid for and never graded,
so it is graded from the stored report rather than run again; an unreadable
one is run again. `--resume` does not combine with `--emit`, because a fresh
emit changes the run id and the cells of one group would then differ in data. Without
`--resume`, `--repeats 3` run twice over the same cells appends five
repeats, not three, because each invocation starts counting from the next
free repeat number; `--resume` is what makes a second `--repeats 3`
invocation top a cell up to three rather than appending three more.

`evals/results/runs.json` is an index over the manifests in `gen/runs/`, not
a copy of them. The manifest stays the source of truth for the window, the
counts, and the dataset; the index records which run id each scenario most
recently emitted, plus the window as a courtesy to whoever reads it. The
index is tracked in git and the manifests are not, so the run ids behind a
committed `report.md` are in the history while the regenerable manifests
stay on the machine that emitted them. A run id in the index whose manifest
is missing cannot be investigated or graded, and the runner says so and
stops before anything is spent. When the index has no entry for a scenario
the newest real (not dry run) manifest for it in `gen/runs/` is used and
recorded, which is what makes the index rebuildable from the manifests.

Crashes. A run that raises out of `investigate`, or that the loop ended with
`stop_reason == "error"`, is a row with `total = 0`, the error text, and
whatever process fields were measured. It skips the grader, because an
empty report on a control scenario grades as a correct "no incident" there.
Every other stop reason is graded. A run that filed within the grace turns
after a cap has `stop_reason == "report"`. A run with `schema` (its last
submit_report did not parse as a draft), `call_cap`, `wall_cap`, or
`model_stopped` filed nothing, and its empty report goes through the grader,
which scores it as no answer: the outcome components are 0 on an incident
scenario and on a control alike, because the default `incident_present` is
not a claim the model made. The `stopped by` column in the report shows
which runs those were. If the MCP session fails to close after the report
was filed, the report is graded and the close error is kept in the grade's
notes.

Honeycomb's harness, for the contrast the README draws. In
`honeycombio/agent-skill` (read on 2026-09-03 from `main` at commit
`41214b7dfb97f262adabf295fa6f0fcad85bc0f6`; `tests/scenarios/runner.py`,
`report.py`, `test_scenarios.py`, and `.github/workflows/scenarios.yml` all
last changed in `f115bdf6d928aed4bb3ca5f386cbe5102ab30c2f`) each scenario is
run once with the plugin and once without through the `claude` CLI, scored by
the process evaluator, and compared: the test fails if the plugin's score
falls more than 0.1 below the baseline. The tolerance for nondeterminism is
in the workflow, not the harness: the pytest step runs with `|| true` and a
later step reads the pass and fail counts out of the log and fails the job
only when more than two scenarios failed. There are no repeats. Ours runs
each cell N times and shows the mean and the range, and grades the answer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from agent.auth import OAuthNotAuthorized, require_oauth_provider
from agent.board import BoardResult, ensure_board
from agent.handoff import Handoff, hand_off
from agent.loop import (
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_WALL_S,
    AgentConfig,
    ScenarioRun,
    investigate,
    preflight,
)
from agent.mcp_client import HoneycombMCP, TokenBucket
from agent.providers.base import Provider
from agent.report import Report, load_report
from agent.telemetry import Telemetry
from evals.grader import WRONG_BELOW, Grade, grade, grade_file
from gen.emit import RUNS_DIR, EmitResult, emit, load_manifest
from gen.scenario import Scenario, available_scenarios, load_scenario
from receipts.settings import Settings

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent / "results"
RUNS_INDEX = RESULTS_DIR / "runs.json"

# The configs are `AgentConfig` knobs and nothing else. R10 owns whether the
# prompt changes with them; here `full` is the defaults.
CONFIGS: dict[str, dict[str, Any]] = {
    "full": {},
    "no-negation": {"require_negation": False},
    "no-notchecked": {"require_not_checked": False},
}

PROVIDERS: tuple[str, ...] = ("anthropic", "bedrock", "ollama", "nvidia")

# The ollama provider's own wall budget default: 20 minutes
# rather than the 8 every other provider gets, because prompt eval on a 30
# to 40k token context late in a run is the expected weak spot on local
# hardware. Applied only when `--max-wall-s` was not given on the command
# line; an explicit value always wins, for any provider.
OLLAMA_DEFAULT_MAX_WALL_S = 1200.0


def resolved_max_wall_s(provider: str, max_wall_s: float | None) -> float:
    """The wall budget a run gets: an explicit `--max-wall-s` always wins.

    Left unset (`None`, argparse's default when the flag is not given),
    ollama gets `OLLAMA_DEFAULT_MAX_WALL_S` and every other provider gets
    `DEFAULT_MAX_WALL_S`.
    """
    if max_wall_s is not None:
        return max_wall_s
    return OLLAMA_DEFAULT_MAX_WALL_S if provider == "ollama" else DEFAULT_MAX_WALL_S


# --------------------------------------------------------------------------
# The run index
# --------------------------------------------------------------------------


class IndexEntry(BaseModel):
    """One emitted run, as `runs.json` records it. The manifest has the rest."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    scenario_id: str
    emitted_at: str
    window_start: str
    window_end: str

    @classmethod
    def from_manifest(cls, manifest: EmitResult) -> IndexEntry:
        return cls(
            run_id=manifest.run_id,
            scenario_id=manifest.scenario_id,
            emitted_at=manifest.emitted_at,
            window_start=manifest.window_start,
            window_end=manifest.window_end,
        )


class RunIndex(BaseModel):
    """`evals/results/runs.json`: which run id each scenario was last emitted as."""

    model_config = ConfigDict(extra="forbid")

    runs: list[IndexEntry] = []

    @classmethod
    def load(cls, path: Path = RUNS_INDEX) -> RunIndex:
        if not path.exists():
            return cls()
        return cls.model_validate(json.loads(path.read_text()))

    def save(self, path: Path = RUNS_INDEX) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n")
        return path

    def latest(self, scenario_id: str) -> IndexEntry | None:
        """The most recently emitted run for a scenario, by `emitted_at`."""
        matching = [entry for entry in self.runs if entry.scenario_id == scenario_id]
        if not matching:
            return None
        return max(matching, key=lambda entry: entry.emitted_at)

    def add(self, manifest: EmitResult) -> IndexEntry:
        entry = IndexEntry.from_manifest(manifest)
        self.runs = [existing for existing in self.runs if existing.run_id != entry.run_id]
        self.runs.append(entry)
        self.runs.sort(key=lambda item: (item.scenario_id, item.emitted_at, item.run_id))
        return entry


def newest_manifest(scenario_id: str, runs_dir: Path = RUNS_DIR) -> EmitResult | None:
    """The newest manifest in `runs_dir` for a scenario that actually sent spans."""
    best: EmitResult | None = None
    for path in sorted(runs_dir.glob("run-*.json")):
        try:
            manifest = EmitResult(**json.loads(path.read_text()))
        except (TypeError, ValueError):
            continue
        if manifest.scenario_id != scenario_id or "dry run" in manifest.mode:
            continue
        if best is None or manifest.emitted_at > best.emitted_at:
            best = manifest
    return best


def resolve_run(
    scenario_id: str,
    index: RunIndex,
    runs_dir: Path = RUNS_DIR,
) -> EmitResult:
    """The manifest to investigate for a scenario: the index's latest, or the newest on disk.

    Raises `FileNotFoundError` when the index names a run whose manifest is
    gone, or when there is no run for the scenario at all.
    """
    entry = index.latest(scenario_id)
    if entry is not None:
        manifest = load_manifest(entry.run_id, runs_dir)
        if manifest.scenario_id != scenario_id:
            raise ValueError(
                f"runs.json says {entry.run_id} is {scenario_id!r}; its manifest says "
                f"{manifest.scenario_id!r}"
            )
        return manifest
    manifest = newest_manifest(scenario_id, runs_dir)
    if manifest is None:
        raise FileNotFoundError(
            f"no run for {scenario_id!r}: nothing in runs.json and no manifest in {runs_dir}. "
            "Emit one with --emit."
        )
    index.add(manifest)
    return manifest


# --------------------------------------------------------------------------
# What a run produces
# --------------------------------------------------------------------------


class GradedRun(BaseModel):
    """One cell of the matrix, as `grade.json` records it.

    The flat fields are what `evals/report.py` reads. `grade` is the full
    `Grade` for a run that was graded and None for a crash, and the flat
    fields are copied from it so a crash and a graded run have one shape.
    """

    model_config = ConfigDict(extra="forbid")

    config: str
    scenario_id: str
    run_id: str
    repeat: int
    provider: str
    model: str

    total: float
    outcome_score: float
    receipts_score: float
    dims: float
    """The grader's dims component, 0 to 1. Zero for a crash."""
    top_wrong: bool
    """The grader's flag, copied. True for a crash."""
    top_confidence: str | None

    stop_reason: str
    error: str | None
    validation_failed: bool
    coerced_fields: list[str] = Field(default_factory=list)
    """Fields submit_report sent as a JSON-encoded string and Report.py decoded, copied
    from the report so a run that needed it is visible without opening report.json."""
    permalink: str | None
    """The top evidence query of the top hypothesis, or the first baseline query."""

    tool_calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    wall_s: float
    max_wall_s: float = 0.0
    """The wall budget the run was given, copied from `Report.max_wall_s`. Default 0
    for an older `grade.json` without the field, read by `evals/report.py` as "not
    recorded" rather than a real zero-second budget."""
    malformed_calls: int = 0
    """Tool calls a provider handed back with arguments that could not be parsed at
    all, copied from `Report.malformed_calls`. Zero both when none happened and for
    an older `grade.json` without the field."""
    honeycomb_process_score: float | None
    honeycomb_process_passed: bool | None

    grade: Grade | None
    notes: list[str] = Field(default_factory=list)
    """Runner-side notes, such as a session that failed to close after the report."""

    @property
    def crashed(self) -> bool:
        return self.grade is None

    @property
    def top_right(self) -> bool:
        """Whether the top hypothesis was right, by the grader's own line.

        The grader calls a top hypothesis wrong when its dims component is
        under `WRONG_BELOW`, and that flag is False when there is no
        hypothesis at all. The dims component itself covers that case: on an
        incident scenario an empty report scores 0 there, and on a control a
        quiet report scores 1. A crash has no dims and is never right.
        """
        return not self.crashed and self.dims >= WRONG_BELOW


def top_permalink(report: Report) -> str | None:
    """The permalink the eval report shows for a run.

    The first evidence query of the top hypothesis; with no hypothesis, the
    first baseline query. A run with neither gets None.
    """
    if report.hypotheses:
        for item in report.hypotheses[0].evidence:
            if item.permalink:
                return item.permalink
    for item in report.baseline_evidence:
        if item.permalink:
            return item.permalink
    return None


def graded_run(report: Report, result: Grade, *, config: str, repeat: int) -> GradedRun:
    """The record for a run the grader scored."""
    return GradedRun(
        config=config,
        scenario_id=report.scenario_id,
        run_id=report.run_id,
        repeat=repeat,
        provider=report.provider,
        model=report.model,
        total=result.total,
        outcome_score=result.outcome_score,
        receipts_score=result.receipts_score,
        dims=result.components.dims,
        top_wrong=result.top_wrong,
        top_confidence=result.top_confidence,
        stop_reason=report.stop_reason,
        error=report.error,
        validation_failed=report.validation_failed,
        coerced_fields=report.coerced_fields,
        permalink=top_permalink(report),
        tool_calls=result.process.tool_calls,
        tokens_in=result.process.tokens_in,
        tokens_out=result.process.tokens_out,
        cost_usd=result.process.cost_usd,
        wall_s=result.process.wall_s,
        max_wall_s=report.max_wall_s,
        malformed_calls=report.malformed_calls,
        honeycomb_process_score=result.process.honeycomb_process_score,
        honeycomb_process_passed=result.process.honeycomb_process_passed,
        grade=result,
    )


def crashed_run(
    *,
    config: str,
    scenario_id: str,
    run_id: str,
    repeat: int,
    error: str,
    report: Report | None = None,
    tool_calls: int = 0,
    wall_s: float = 0.0,
    provider: str = "",
    model: str = "",
) -> GradedRun:
    """The record for a run that did not produce a gradable report.

    With a `report` (the loop ended with `stop_reason == "error"`) the process
    fields are the loop's. Without one (the exception escaped `investigate`)
    they are what the runner counted from outside.
    """
    return GradedRun(
        config=config,
        scenario_id=scenario_id,
        run_id=run_id,
        repeat=repeat,
        provider=report.provider if report else provider,
        model=report.model if report else model,
        total=0.0,
        outcome_score=0.0,
        receipts_score=0.0,
        dims=0.0,
        top_wrong=True,
        top_confidence=None,
        stop_reason=report.stop_reason if report else "crash",
        error=error,
        validation_failed=report.validation_failed if report else False,
        coerced_fields=report.coerced_fields if report else [],
        permalink=top_permalink(report) if report else None,
        tool_calls=report.tool_calls if report else tool_calls,
        tokens_in=report.tokens_in if report else 0,
        tokens_out=report.tokens_out if report else 0,
        cost_usd=report.cost_usd if report else 0.0,
        wall_s=report.wall_s if report else round(wall_s, 2),
        max_wall_s=report.max_wall_s if report else 0.0,
        malformed_calls=report.malformed_calls if report else 0,
        honeycomb_process_score=None,
        honeycomb_process_passed=None,
        grade=None,
    )


def run_dir(results_dir: Path, config: str, scenario_id: str, repeat: int) -> Path:
    return results_dir / config / scenario_id / str(repeat)


def next_repeat(results_dir: Path, config: str, scenario_id: str) -> int:
    """The first repeat number with no directory yet."""
    repeat = 1
    while run_dir(results_dir, config, scenario_id, repeat).exists():
        repeat += 1
    return repeat


def write_run(directory: Path, graded: GradedRun, report: Report | None) -> Path:
    """Write `grade.json` and, when there is one, `report.json`. Returns the grade path."""
    directory.mkdir(parents=True, exist_ok=True)
    if report is not None:
        report.write_to(directory / "report.json")
    path = directory / "grade.json"
    path.write_text(graded.model_dump_json(indent=2) + "\n")
    return path


def write_handoff(directory: Path, handoff: Handoff) -> Path:
    """Write `handoff.json` next to `grade.json`. Only called with `--handoff`."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "handoff.json"
    path.write_text(handoff.model_dump_json(indent=2) + "\n")
    return path


def grade_stored(
    directory: Path, config: str, repeat: int, *, runs_dir: Path = RUNS_DIR
) -> GradedRun:
    """Grade a cell that holds a `report.json` and no `grade.json`, and write the grade.

    The report is the paid part of a cell and the grade is a pure function of
    it, so a cell the process left between the two writes is finished by
    grading what is there, not by running the investigation again over the
    stored report. A report the loop ended with an error is a crash row, the
    same as `run_one` makes of it: an empty error report on a control would
    otherwise grade as a correct "no incident". Runner-side `notes` are
    empty: they were never written. Raises `ValueError` or `OSError` when the
    report cannot be read; the caller decides what to do with the cell.
    """
    report_path = directory / "report.json"
    report = load_report(report_path)
    if report.error is not None or report.stop_reason == "error":
        graded = crashed_run(
            config=config,
            scenario_id=report.scenario_id,
            run_id=report.run_id,
            repeat=repeat,
            error=report.error or "loop ended with stop_reason=error",
            report=report,
            provider=report.provider,
            model=report.model,
        )
    else:
        result = grade_file(report_path, runs_dir=runs_dir)
        graded = graded_run(report, result, config=config, repeat=repeat)
    write_run(directory, graded, None)
    return graded


def load_run(path: Path) -> GradedRun:
    return GradedRun.model_validate(json.loads(path.read_text()))


def regrade(
    results_dir: Path = RESULTS_DIR,
    runs_dir: Path = RUNS_DIR,
) -> list[tuple[Path, float, float]]:
    """Rebuild every `grade.json` under `results_dir` from its `report.json`.

    Grading is a pure function of the report and the scenario, so a change
    to `evals/grader.py` leaves every stored `report.json` correct and every
    stored `grade.json` stale. This walks the `<config>/<scenario>/<n>/`
    layout `run_matrix` writes and regrades each cell through `grade_file`,
    the same report-and-manifest lookup `run_one` uses, then overwrites
    `grade.json` with the result; `report.json` and the runner-side `notes`
    are untouched. No investigation runs and nothing is spent.

    A cell with no `report.json`, or whose stored grade was already a crash
    (`grade` is `None`, by design: a run that ended in error skips the
    grader, because an empty report on a control would otherwise read as a
    correct "no incident"), is left alone: there is nothing to regrade it
    from, or it was never graded in the first place.

    Returns the cells whose total changed, as `(grade.json path, old total,
    new total)`, in the sorted order they were found.
    """
    changed: list[tuple[Path, float, float]] = []
    for grade_path in sorted(results_dir.glob("*/*/*/grade.json")):
        old = load_run(grade_path)
        report_path = grade_path.parent / "report.json"
        if old.crashed or not report_path.exists():
            continue
        report = load_report(report_path)
        result = grade_file(report_path, runs_dir=runs_dir)
        new = graded_run(report, result, config=old.config, repeat=old.repeat)
        new.notes = old.notes
        if round(new.total, 6) != round(old.total, 6):
            changed.append((grade_path, old.total, new.total))
        grade_path.write_text(new.model_dump_json(indent=2) + "\n")
    return changed


# --------------------------------------------------------------------------
# Configs
# --------------------------------------------------------------------------


def agent_config(
    name: str,
    *,
    provider: str = "anthropic",
    model: str | None = None,
    max_calls: int = DEFAULT_MAX_CALLS,
    max_wall_s: float = DEFAULT_MAX_WALL_S,
) -> AgentConfig:
    """The `AgentConfig` for a named config. Unknown names are a `KeyError`."""
    knobs = CONFIGS[name]
    return AgentConfig(
        provider=provider,
        model=model,
        max_calls=max_calls,
        max_wall_s=max_wall_s,
        **knobs,
    )


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

OpenMCP = Callable[[Settings], AbstractAsyncContextManager[Any]]
ProviderFactory = Callable[[AgentConfig, Settings], Provider]


class _CountingMCP:
    """Passes `list_tools` and `call` through and counts the calls for the progress line."""

    def __init__(self, inner: Any, on_call: Callable[[int], None] | None = None) -> None:
        self._inner = inner
        self._on_call = on_call
        self.calls = 0

    async def list_tools(self) -> Any:
        return await self._inner.list_tools()

    async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls += 1
        if self._on_call is not None:
            self._on_call(self.calls)
        return await self._inner.call(name, args, **kwargs)


class _SharedSessions:
    """One token bucket for every session the matrix opens.

    The bucket lives on the `HoneycombMCP` instance, so a fresh session per
    run would start with a full bucket. Two runs back to back could then send
    up to 80 calls in one minute against a limit of 50. Sharing one bucket
    across the whole matrix keeps the whole matrix under the cap.
    """

    def __init__(self, bucket: TokenBucket | None = None) -> None:
        self.bucket = bucket or TokenBucket()

    def __call__(self, settings: Settings) -> HoneycombMCP:
        return HoneycombMCP(settings=settings, bucket=self.bucket)

    def write(self, settings: Settings) -> HoneycombMCP:
        """A write-capable session sharing this matrix's token bucket.

        Opened only for `--handoff` (R12) cells, and only for the board and
        Canvas calls that come after an investigation's own read-only
        session has already closed: the model itself is never offered a
        write tool (see `agent/loop.py`'s `INVESTIGATION_TOOLS`), so this is
        the one place in the matrix `allow_write=True` appears.

        Builds its own session from `settings` with `honeycomb_auth`
        forced to `"oauth"`, regardless of what `settings.honeycomb_auth`
        already says: Canvas needs a user actor and a management key has
        none (see `agent/auth.py`), but every read session this class opens
        (`__call__`, above) must stay on whatever `settings` actually
        configures. Without this override, the only way to satisfy
        `canvas_agent_invoke`'s precondition would be to set
        `HONEYCOMB_AUTH=oauth` for the whole process, which routes the
        investigation's own read queries through OAuth too, running them as
        a person rather than the service identity `receipts/settings.py`
        says the eval matrix keeps using.
        """
        write_settings = settings.model_copy(update={"honeycomb_auth": "oauth"})
        return HoneycombMCP(settings=write_settings, allow_write=True, bucket=self.bucket)


async def run_one(
    *,
    config_name: str,
    config: AgentConfig,
    run: ScenarioRun,
    scenario: Scenario,
    window_start: datetime,
    repeat: int,
    settings: Settings,
    results_dir: Path,
    open_mcp: OpenMCP,
    provider_factory: ProviderFactory | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_call: Callable[[int], None] | None = None,
    telemetry: Telemetry | None = None,
    handoff: bool = False,
    open_write_mcp: OpenMCP | None = None,
    write_bucket: TokenBucket | None = None,
) -> GradedRun:
    """One investigation, graded and written. Never raises for the run's own failure.

    `telemetry` opens this run's root span before `investigate` runs and
    carries the evaluation result once `grade` returns; with none given, a
    disabled `Telemetry()` is used, which emits nothing (see
    `agent/telemetry.py`). This is the caller that grades, so it is the one
    that calls `end_with_grade` or `end_with_error`; `agent/loop.py` never
    ends the root span itself.

    The conversation id is `<run_id>.<config_name>.<repeat>`, matching the
    results path this cell writes to (`run_dir`): one emit serves every
    config and repeat since R8, so the run id names the traffic under
    investigation and this is what names the investigation. A dot, not a
    slash: a live check found a `/` in the id broke the Agent Timeline's
    Traces panel.

    `handoff` (R12, EDW-1334) runs strictly after grading, and only for a
    cell that produced a graded (not crashed) report: a crash has nothing
    worth handing to Canvas. It opens its own `open_write_mcp` session,
    separate from the read-only `open_mcp` session the investigation used,
    creates or reuses the run's board, and sends the top hypothesis to
    Canvas. Every failure in that sequence, including the write session
    itself failing to open or close, is caught here and never reaches the
    caller: a handoff is a bolt-on record next to an already-graded run,
    never a reason to change or fail it. `_hand_off_cell` gets this same
    `telemetry` and `conversation_id`, so the handoff's own
    trace lands in the same Agent Timeline conversation as the
    investigation that produced the report, as a second root span opened
    after this one has already ended (see `Telemetry.start_handoff`).
    """
    telemetry = telemetry or Telemetry()
    conversation_id = f"{run.run_id}.{config_name}.{repeat}"
    run_trace = telemetry.start_run(
        run.run_id,
        run.scenario_id,
        conversation_id=conversation_id,
        config_label=config_name,
        provider=config.provider,
    )
    directory = run_dir(results_dir, config_name, run.scenario_id, repeat)
    started = time.monotonic()
    counter: _CountingMCP | None = None
    report: Report | None = None
    notes: list[str] = []
    try:
        try:
            async with open_mcp(settings) as session:
                counter = _CountingMCP(session, on_call)
                provider = provider_factory(config, settings) if provider_factory else None
                report = await investigate(
                    run,
                    config,
                    settings=settings,
                    mcp=counter,
                    provider=provider,
                    clock=clock,
                    trace=run_trace,
                )
        except Exception as exc:
            if report is None:
                raise
            # The investigation finished and the session failed to close on
            # the way out. The report is on hand and is graded; the close
            # error is kept next to it rather than turning a paid-for answer
            # into a crash row.
            message = f"session close failed after the report: {type(exc).__name__}: {exc}"
            logger.warning(message)
            notes.append(message)
        if report.error is not None or report.stop_reason == "error":
            raise _LoopError(report.error or "the loop stopped with an error and no message")
        result = grade(report, scenario, window_start=window_start)
        graded = graded_run(report, result, config=config_name, repeat=repeat)
        graded.notes = notes
    except _LoopError as exc:
        graded = crashed_run(
            config=config_name,
            scenario_id=run.scenario_id,
            run_id=run.run_id,
            repeat=repeat,
            error=str(exc),
            report=report,
        )
    except Exception as exc:
        graded = crashed_run(
            config=config_name,
            scenario_id=run.scenario_id,
            run_id=run.run_id,
            repeat=repeat,
            error=f"{type(exc).__name__}: {exc}",
            report=report,
            tool_calls=counter.calls if counter else 0,
            wall_s=time.monotonic() - started,
        )
    if report is not None:
        run_trace.record_outcome(report)
    if graded.crashed:
        run_trace.end_with_error(_error_type(graded.error), graded.error or "crash")
    else:
        assert graded.grade is not None
        run_trace.end_with_grade(graded.total, graded.grade.components.model_dump())
    if telemetry.enabled and run_trace.trace_id:
        print(
            f"trace: {run_trace.trace_id} conversation: {conversation_id}",
            file=sys.stderr,
        )
    write_run(directory, graded, report)
    if handoff and report is not None and not graded.crashed:
        await _hand_off_cell(
            directory,
            report,
            settings,
            open_write_mcp,
            write_bucket,
            telemetry=telemetry,
            conversation_id=conversation_id,
            config_label=config_name,
            provider=config.provider,
        )
    return graded


async def _hand_off_cell(
    directory: Path,
    report: Report,
    settings: Settings,
    open_write_mcp: OpenMCP | None,
    write_bucket: TokenBucket | None = None,
    *,
    telemetry: Telemetry | None = None,
    conversation_id: str | None = None,
    config_label: str = "full",
    provider: str = "anthropic",
) -> None:
    """Create or reuse the run's board and hand the report to Canvas.

    Wrapped in its own `try`/`finally` so a failure here (opening the write
    session, `ensure_board`, `hand_off`, or writing the file) never turns a
    graded run into a crash, and so the handoff root span is always ended:
    `ensure_board` and `hand_off` already carry their own failures on the
    value they return, so the `except Exception` below only needs to catch
    the write session's own `__aenter__`/`__aexit__`; the `finally` is what
    also covers a `BaseException` (`asyncio.CancelledError`,
    `KeyboardInterrupt`) reaching in from outside this function, which an
    `except Exception` alone does not catch and would otherwise leave
    `handoff_trace` unended and unexported.

    `write_bucket` only matters when `open_write_mcp` is `None`: the
    fallback session built right below must still share the matrix's rate
    limit, not start a fresh, disconnected `TokenBucket` of its own.
    `run_matrix` always resolves `open_write_mcp`
    before a `--handoff` cell reaches here, so this fallback is normally
    exercised only by a caller that invokes `run_one` directly.

    The fallback is `_SharedSessions(write_bucket).write` itself, not a
    second copy of what that method does: a duplicate body here would leave
    two places that had to agree on how a write session is built and no way
    to stop them drifting apart.

    `telemetry`, `conversation_id`, `config_label`, and `provider` open a
    second root span, `invoke_agent canvas`
    (`Telemetry.start_handoff`), sharing `conversation_id` with the
    investigation's own root so both land in the same Agent Timeline
    conversation; see `agent/telemetry.py`'s `start_handoff` for why this is
    a second root rather than a child of that span, which has already ended
    by the time a handoff runs. With none given (a caller invoking this
    directly, as one test does), a disabled `Telemetry()` and `report.run_id`
    stand in for `telemetry` and `conversation_id`, the same fallback
    `run_one` uses for `trace` and `conversation_id` elsewhere, and
    `config_label`/`provider` default to this project's own defaults.
    """
    telemetry = telemetry or Telemetry()
    handoff_trace = telemetry.start_handoff(
        report.run_id,
        conversation_id=conversation_id or report.run_id,
        config_label=config_label,
        provider=provider,
    )
    write_open = open_write_mcp or _SharedSessions(write_bucket).write
    board: BoardResult | None = None
    result: Handoff | None = None
    try:
        try:
            async with write_open(settings) as write_session:
                board = await ensure_board(
                    report,
                    write_session,
                    environment_slug=settings.honeycomb_env,
                    trace=handoff_trace,
                )
                result = await hand_off(
                    report,
                    write_session,
                    board_id=board.board_id,
                    board_url=board.board_url,
                    trace=handoff_trace,
                )
        except Exception as exc:
            logger.warning("handoff failed for %s: %s", report.run_id, exc)
            result = Handoff(
                run_id=report.run_id,
                prompt="",
                status="error",
                classification="no_response",
                board_id=board.board_id if board else None,
                board_url=board.board_url if board else None,
                error=f"{type(exc).__name__}: {exc}",
            )
    finally:
        if result is None:
            # A BaseException (CancelledError, KeyboardInterrupt) reached in
            # before `hand_off` or the `except Exception` above produced a
            # Handoff: still close the trace and still record what
            # happened, rather than leaving the handoff root span open and
            # no handoff.json written at all.
            result = Handoff(
                run_id=report.run_id,
                prompt="",
                status="error",
                classification="no_response",
                board_id=board.board_id if board else None,
                board_url=board.board_url if board else None,
                error="handoff interrupted before completing",
            )
        handoff_trace.end_with_handoff(
            status=result.status,
            classification=result.classification,
            reply=result.raw_text,
            board_id=result.board_id,
            board_url=result.board_url,
            error=result.error,
            board_error=board.error if board else None,
        )
        write_handoff(directory, result)


def _error_type(error: str | None) -> str:
    """A low-cardinality `error.type` from a crash message.

    Crash messages are built as `f"{type(exc).__name__}: {exc}"`, so the part
    before the first colon is usually the exception's class name; anything
    else is used whole, which is still better than no label at all.
    """
    if not error:
        return "error"
    return error.split(":", 1)[0].strip() or "error"


class _LoopError(Exception):
    """The loop returned a report with `stop_reason == "error"`."""


async def run_matrix(
    scenario_ids: Sequence[str],
    config_names: Sequence[str],
    repeats: int,
    *,
    settings: Settings,
    results_dir: Path = RESULTS_DIR,
    runs_dir: Path = RUNS_DIR,
    index_path: Path | None = None,
    emit_first: bool = False,
    provider: str = "anthropic",
    model: str | None = None,
    max_calls: int = DEFAULT_MAX_CALLS,
    max_wall_s: float = DEFAULT_MAX_WALL_S,
    open_mcp: OpenMCP | None = None,
    provider_factory: ProviderFactory | None = None,
    emitter: Callable[[Scenario, Settings], EmitResult] | None = None,
    clock: Callable[[], float] = time.monotonic,
    console: Console | None = None,
    telemetry: Telemetry | None = None,
    resume: bool = False,
    handoff: bool = False,
    open_write_mcp: OpenMCP | None = None,
) -> list[GradedRun]:
    """Run every cell in order and return the graded runs, one per cell.

    Sequential on purpose: the MCP rate limit is per team. Every run id is
    resolved (or emitted) before the first investigation, so a missing
    manifest stops the matrix before anything is spent.

    `telemetry` is shared across every cell, one export queue for the whole
    matrix rather than one per run, the way `_SharedSessions` shares one MCP
    token bucket. With none given, a disabled `Telemetry()` is used and the
    matrix emits nothing; the CLI in this module builds a real one from
    `settings` before calling this.

    `resume` changes only which repeat numbers run. Without it, each of the
    `repeats` iterations calls `next_repeat` and appends after whatever is
    already on disk, so a second invocation over a cell that already has
    repeats 1 and 2 produces 3, 4, 5. With `resume`, repeats 1 through
    `repeats` are visited in order and a repeat whose directory already
    holds a `grade.json` is skipped, one line per skipped cell, through
    `console`; a repeat with a `report.json` but no `grade.json` was paid
    for and never graded, so it is graded from the stored report and not
    run again. Skipped cells are not re-run, not re-graded, and not in the
    returned list, since the report reads them from disk anyway.

    `handoff` (R12) hands each cell's graded report to Canvas after it is
    written; off by default, so the matrix behaves exactly as it did before
    R12 unless a caller asks for it. `open_write_mcp` is the session
    `agent/board.py` and `agent/handoff.py` write through; left unset, it
    shares one `TokenBucket` with `open_mcp`'s reads (`open_mcp.bucket` when
    `open_mcp` is the default `_SharedSessions`, a fresh bucket handed to a
    caller-supplied `open_mcp` that is not, since a custom `open_mcp` does
    not expose one to share), so a matrix run with `--handoff` still stays
    under the one team-wide rate limit. That same bucket is also handed to
    each cell as `write_bucket`, so `_hand_off_cell`'s own default session
    (built only when a caller invokes `run_one` directly, bypassing this
    resolution) draws from it too, instead of a second bucket nothing else
    knows about.
    """
    index_path = index_path or results_dir / "runs.json"
    console = console or Console()
    open_mcp = open_mcp or _SharedSessions()
    write_bucket = open_mcp.bucket if isinstance(open_mcp, _SharedSessions) else TokenBucket()
    if handoff and open_write_mcp is None:
        open_write_mcp = _SharedSessions(write_bucket).write
    telemetry = telemetry or Telemetry()
    index = RunIndex.load(index_path)

    scenarios = {scenario_id: load_scenario(scenario_id) for scenario_id in scenario_ids}
    manifests: dict[str, EmitResult] = {}
    for scenario_id in scenario_ids:
        if emit_first:
            do_emit = emitter or (lambda scenario, settings: emit(scenario, settings))
            console.print(f"emitting {scenario_id}")
            manifest = do_emit(scenarios[scenario_id], settings)
            manifest.write(runs_dir)
            index.add(manifest)
            # Saved per scenario, so a failed emit later in the list cannot
            # orphan the spans this one just paid for.
            index.save(index_path)
            console.print(
                f"  {manifest.run_id}: {manifest.exported} spans, "
                f"{manifest.window_start} to {manifest.window_end}"
            )
        else:
            manifest = resolve_run(scenario_id, index, runs_dir)
            console.print(f"{scenario_id}: reusing {manifest.run_id} ({manifest.emitted_at})")
        manifests[scenario_id] = manifest
    index.save(index_path)

    configs = {
        name: agent_config(
            name, provider=provider, model=model, max_calls=max_calls, max_wall_s=max_wall_s
        )
        for name in config_names
    }

    results: list[GradedRun] = []
    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        TextColumn("calls {task.fields[calls]}"),
        TimeElapsedColumn(),
        console=console,
    )

    async def run_cell(
        scenario_id: str,
        config_name: str,
        repeat: int,
        run: ScenarioRun,
        scenario: Scenario,
        window_start: datetime,
    ) -> None:
        task = progress.add_task(
            f"{scenario_id} {config_name} repeat {repeat}", calls=0, total=None
        )

        def on_call(count: int, task_id: Any = task) -> None:
            progress.update(task_id, calls=count)

        graded = await run_one(
            config_name=config_name,
            config=configs[config_name],
            run=run,
            scenario=scenario,
            window_start=window_start,
            repeat=repeat,
            settings=settings,
            results_dir=results_dir,
            open_mcp=open_mcp,
            provider_factory=provider_factory,
            clock=clock,
            on_call=on_call,
            telemetry=telemetry,
            handoff=handoff,
            open_write_mcp=open_write_mcp,
            write_bucket=write_bucket,
        )
        progress.remove_task(task)
        results.append(graded)
        progress.console.print(_summary_line(graded))

    try:
        with progress:
            for scenario_id in scenario_ids:
                manifest = manifests[scenario_id]
                run = ScenarioRun.from_manifest(manifest)
                window_start = datetime.fromtimestamp(manifest.window_start_s, tz=UTC)
                for config_name in config_names:
                    if resume:
                        for repeat in range(1, repeats + 1):
                            cell = run_dir(results_dir, config_name, scenario_id, repeat)
                            head = f"{scenario_id} {config_name} {repeat}"
                            if (cell / "grade.json").exists():
                                try:
                                    stored = load_run(cell / "grade.json")
                                except (ValueError, OSError) as exc:
                                    stored = None
                                    progress.console.print(
                                        f"{head}: grade.json unreadable ({exc}), redoing"
                                    )
                                if stored is not None:
                                    if stored.crashed:
                                        progress.console.print(
                                            f"{head}: crashed, skipped (delete the directory "
                                            "to retry)"
                                        )
                                    else:
                                        progress.console.print(f"{head}: done, skipped")
                                    continue
                            if (cell / "report.json").exists():
                                try:
                                    graded = grade_stored(
                                        cell, config_name, repeat, runs_dir=runs_dir
                                    )
                                except (ValueError, OSError) as exc:
                                    progress.console.print(
                                        f"{head}: report.json unreadable ({exc}), running again"
                                    )
                                else:
                                    results.append(graded)
                                    notes = graded.grade.notes if graded.grade else []
                                    tail = f" ({'; '.join(notes)})" if notes else ""
                                    progress.console.print(
                                        f"{head}: graded from the stored report{tail}"
                                    )
                                    continue
                            await run_cell(
                                scenario_id,
                                config_name,
                                repeat,
                                run,
                                scenarios[scenario_id],
                                window_start,
                            )
                    else:
                        for _ in range(repeats):
                            repeat = next_repeat(results_dir, config_name, scenario_id)
                            await run_cell(
                                scenario_id,
                                config_name,
                                repeat,
                                run,
                                scenarios[scenario_id],
                                window_start,
                            )
    finally:
        # However the loop above ends, including an exception this function
        # does not otherwise handle, pending spans still get exported and
        # the provider still shuts down cleanly.
        telemetry.flush()
        telemetry.shutdown()
    return results


def _summary_line(graded: GradedRun) -> str:
    head = f"{graded.scenario_id} {graded.config} {graded.repeat}: "
    if graded.crashed:
        return head + (
            f"[red]crashed[/red] after {graded.tool_calls} calls: {escape(graded.error or '')}"
        )
    return head + (
        f"total {graded.total:.3f} (outcome {graded.outcome_score:.3f}), "
        f"{graded.tool_calls} calls, {graded.wall_s:.0f}s, ${graded.cost_usd:.2f}, "
        f"stopped by {graded.stop_reason}"
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Run scenarios x configs x repeats and render evals/report.md.",
    )
    parser.add_argument(
        "--scenarios",
        default=None,
        help=(
            "'all' or a comma-separated list from: "
            f"{available_scenarios()}. Required unless --regrade is given."
        ),
    )
    parser.add_argument(
        "--configs",
        default="full",
        help=f"comma-separated list from: {list(CONFIGS)} (default: full)",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "fill repeats 1..N and skip any cell that already has a grade.json, "
            "printing one line per skipped cell; without it, a second invocation "
            "appends new repeats after whatever is already on disk"
        ),
    )
    parser.add_argument("--provider", choices=PROVIDERS, default="anthropic")
    parser.add_argument("--model", default=None, help="overrides ANTHROPIC_MODEL")
    parser.add_argument(
        "--emit",
        action="store_true",
        help="emit a fresh run per scenario first; otherwise reuse the latest from runs.json",
    )
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument(
        "--max-wall-s",
        type=float,
        default=None,
        help=(
            f"default {DEFAULT_MAX_WALL_S:.0f} "
            f"({OLLAMA_DEFAULT_MAX_WALL_S:.0f} for --provider ollama)"
        ),
    )
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR, help="where manifests live")
    parser.add_argument(
        "--regrade",
        action="store_true",
        help=(
            "rebuild every grade.json under --results-dir from its report.json and exit; "
            "no scenarios or configs, no investigation runs, nothing spent"
        ),
    )
    parser.add_argument(
        "--handoff",
        action="store_true",
        help=(
            "after grading, hand each cell's report to Canvas and create or reuse its board "
            "(R12); writes handoff.json next to grade.json. Off by default. Not applied to a "
            "cell --resume skips or grades from a stored report.json. Needs a Honeycomb OAuth "
            "token on file (`python -m agent.auth login`); adds up to DEFAULT_DEADLINE_S "
            "(300s) per cell on top of the pass, up to 2.5 hours across a 30-cell matrix"
        ),
    )
    return parser.parse_args(argv)


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 when every run graded, 1 when any crashed, 2 on a usage error.

    `--regrade` short-circuits everything else: it neither emits nor
    investigates, so none of the scenario, config, provider, or Settings
    checks below apply to it.
    """
    from evals.report import write_report

    args = _parse_args(argv)

    if args.regrade:
        changed = regrade(args.results_dir, args.runs_dir)
        for path, old_total, new_total in changed:
            rel = path.relative_to(args.results_dir)
            print(f"{rel}: {old_total:.3f} -> {new_total:.3f}")
        if not changed:
            print("no grades changed")
        return 0

    if not args.scenarios:
        print("error: --scenarios is required unless --regrade is given", file=sys.stderr)
        return 2

    known = available_scenarios()
    scenario_ids = known if args.scenarios == "all" else _split(args.scenarios)
    unknown = [item for item in scenario_ids if item not in known]
    if unknown or not scenario_ids:
        print(f"error: unknown scenarios {unknown}; choose from {known}", file=sys.stderr)
        return 2
    config_names = _split(args.configs)
    bad = [item for item in config_names if item not in CONFIGS]
    if bad or not config_names:
        print(f"error: unknown configs {bad}; choose from {list(CONFIGS)}", file=sys.stderr)
        return 2
    if args.repeats < 1:
        print("error: --repeats must be at least 1", file=sys.stderr)
        return 2
    if args.provider == "bedrock":
        print(f"error: provider {args.provider!r} arrives in R11", file=sys.stderr)
        return 2
    max_wall_s = resolved_max_wall_s(args.provider, args.max_wall_s)
    if args.resume and args.emit:
        print(
            "error: --resume and --emit do not combine: a fresh emit changes the run id, and a "
            "resumed cell group would mix data. Emit without --resume, or resume without --emit.",
            file=sys.stderr,
        )
        return 2

    try:
        settings = Settings()
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]) for err in exc.errors())
        print(f"error: missing or invalid in .env: {missing}", file=sys.stderr)
        return 2

    # No key is required on Settings, since gen/emit.py runs with the ingest
    # key alone. The keys the matrix needs are checked here, all at once and
    # before anything is emitted or spent, by the same `preflight` the agent
    # CLI uses; with --emit that includes the ingest key. Without this, a
    # missing key would surface as thirty total=0 rows after the emit
    # already ran. On the OAuth path preflight skips the MCP key; the OAuth
    # token is checked below, and only when --handoff is given, since that
    # is the one path that needs it.
    problems = preflight(
        settings, AgentConfig(provider=args.provider, model=args.model), emit=args.emit
    )
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2

    if args.handoff:
        # The precondition is a usable OAuth token on file, not
        # `settings.honeycomb_auth == "oauth"`: `_SharedSessions.write`
        # forces OAuth on the write session it builds regardless of that
        # setting (see its docstring), so flipping it globally is no longer
        # needed and no longer checked here. `require_oauth_provider` is
        # the same status check `agent/mcp_client.py`'s OAuth path itself
        # uses; running it before the matrix starts means a missing or
        # expired token fails fast, naming the login command, instead of
        # every one of thirty cells recording an OAuthNotAuthorized crash
        # after the matrix already ran.
        try:
            asyncio.run(require_oauth_provider(settings))
        except OAuthNotAuthorized as exc:
            print(
                f"error: --handoff talks to Canvas (canvas_agent_invoke), which needs a "
                f"Honeycomb OAuth session; a management key has no user actor and fails with "
                f"'actor_user_hcid is required'. {exc}",
                file=sys.stderr,
            )
            return 2

    console = Console()
    telemetry = Telemetry(settings)
    try:
        results = asyncio.run(
            run_matrix(
                scenario_ids,
                config_names,
                args.repeats,
                settings=settings,
                results_dir=args.results_dir,
                runs_dir=args.runs_dir,
                emit_first=args.emit,
                provider=args.provider,
                model=args.model,
                max_calls=args.max_calls,
                max_wall_s=max_wall_s,
                console=console,
                telemetry=telemetry,
                resume=args.resume,
                handoff=args.handoff,
            )
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    path = write_report(args.results_dir)
    console.print(f"\nreport: {path}")
    return 1 if any(item.crashed for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
