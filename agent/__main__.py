"""Run one investigation from the command line.

    uv run python -m agent --scenario payments-stripe-v251-uswest --run-id run-4155490e2a44

The scenario is bookkeeping. It locates the run manifest, it fills
`Report.scenario_id` for the grader, and it is checked against the manifest so
a run cannot be graded against the wrong ground truth. It is not shown to the
model; `agent/loop.py` builds the prompt from the run id, the window, the
dataset, and the environment alone.

The report is printed and written to `evals/results/<run_id>/report.json`.
Exit codes: 0 when a report was filed and passed validation, 1 when a report
was filed but failed it or the run ended without one, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from agent.loop import (
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_WALL_S,
    AgentConfig,
    ScenarioRun,
    config_label,
    investigate,
    preflight,
)
from agent.report import Report
from agent.telemetry import Telemetry
from gen.emit import RUNS_DIR, load_manifest
from gen.scenario import available_scenarios, load_scenario
from receipts.settings import Settings

RESULTS_DIR = Path(__file__).resolve().parents[1] / "evals" / "results"


def render(report: Report, console: Console) -> None:
    """Print one report."""
    console.print(f"[bold]run[/bold]      {report.run_id}  ({report.scenario_id})")
    console.print(f"[bold]model[/bold]    {report.provider}/{report.model}")
    console.print(f"[bold]stopped[/bold]  {report.stop_reason}")
    if report.error:
        console.print(f"[red]error[/red]    {report.error}")

    verdict = "an incident" if report.incident_present else "no incident"
    console.print(f"\n[bold]{verdict}[/bold]")
    if report.affected_population:
        console.print(f"affected: {report.affected_population}")
    if report.onset_estimate:
        console.print(f"onset:    {report.onset_estimate.isoformat()}")

    for index, hypothesis in enumerate(report.hypotheses, start=1):
        console.print(f"\n[bold]{index}. {hypothesis.claim}[/bold]  ({hypothesis.confidence})")
        if hypothesis.dims:
            dims = ", ".join(f"{k}={v}" for k, v in hypothesis.dims.items())
            console.print(f"   dims: {dims}")
        if hypothesis.slow_or_failing_span:
            console.print(f"   span: {hypothesis.slow_or_failing_span}")
        for item in hypothesis.evidence:
            console.print(f"   evidence {item.query_id}: {item.summary}")
            if item.permalink:
                console.print(f"     {item.permalink}")
        if hypothesis.negation:
            console.print(
                f"   negation {hypothesis.negation.query_id}: {hypothesis.negation.summary}"
            )
            if hypothesis.negation.permalink:
                console.print(f"     {hypothesis.negation.permalink}")

    for item in report.baseline_evidence:
        console.print(f"\nbaseline {item.query_id}: {item.summary}")
        if item.permalink:
            console.print(f"  {item.permalink}")

    if report.not_checked:
        console.print("\n[bold]not checked[/bold]")
        for entry in report.not_checked:
            console.print(f"  {entry}")

    if report.validation_failed:
        console.print("\n[red]validation failed[/red]")
        for message in report.validation_messages:
            console.print(f"  {message}")

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_row("mcp calls", str(report.tool_calls))
    table.add_row("model turns", str(report.model_turns))
    table.add_row("tokens in", f"{report.tokens_in:,}")
    table.add_row("tokens out", f"{report.tokens_out:,}")
    table.add_row("cache read", f"{report.cache_read_tokens:,}")
    table.add_row("cache write", f"{report.cache_write_tokens:,}")
    table.add_row("cost", f"${report.cost_usd:.4f}")
    table.add_row("wall", f"{report.wall_s:.1f}s")
    console.print("")
    console.print(table)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description="Investigate one emitted run through the hosted Honeycomb MCP.",
    )
    parser.add_argument("--scenario", required=True, help=f"one of: {available_scenarios()}")
    parser.add_argument("--run-id", required=True, help="the run id gen/emit.py printed")
    parser.add_argument("--model", default=None, help="overrides ANTHROPIC_MODEL")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--max-wall-s", type=float, default=DEFAULT_MAX_WALL_S)
    parser.add_argument(
        "--no-negation",
        action="store_true",
        help="drop the negation rule from the prompt and the validator (R10 ablation)",
    )
    parser.add_argument(
        "--no-not-checked",
        action="store_true",
        help="drop the not-checked rule from the prompt and the validator (R10 ablation)",
    )
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--quiet", action="store_true", help="log warnings only")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO, format="%(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        scenario = load_scenario(args.scenario)
        manifest = load_manifest(args.run_id, args.runs_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if manifest.scenario_id != scenario.id:
        print(
            f"error: run {args.run_id} is a {manifest.scenario_id!r} run, not {scenario.id!r}",
            file=sys.stderr,
        )
        return 2

    try:
        settings = Settings()
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]) for err in exc.errors())
        print(f"error: missing or invalid in .env: {missing}", file=sys.stderr)
        return 2

    config = AgentConfig(
        model=args.model,
        require_negation=not args.no_negation,
        require_not_checked=not args.no_not_checked,
        max_calls=args.max_calls,
        max_wall_s=args.max_wall_s,
    )
    run = ScenarioRun.from_manifest(manifest)

    # The same key check evals/run.py makes, before telemetry starts and
    # before an MCP session is opened, printing every missing key at once.
    problems = preflight(settings, config)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2

    # `python -m agent` never grades, so the root span here never carries
    # gen_ai.evaluation.result; evals/run.py is the caller that does.
    #
    # One emit serves every investigation of a run id, so the conversation
    # id adds a timestamp: two CLI runs against the same run id are two
    # separate conversations.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    conversation_id = f"{run.run_id}.cli.{stamp}"
    telemetry = Telemetry(settings)
    run_trace = telemetry.start_run(
        run.run_id,
        run.scenario_id,
        conversation_id=conversation_id,
        config_label=config_label(config),
        provider=config.provider,
    )
    try:
        report = asyncio.run(investigate(run, config, settings=settings, trace=run_trace))
    except BaseException as exc:
        # investigate() itself failed, so there is no Report to grade or to
        # read a stop_reason from: this is a crash, and the root span should
        # say so rather than export as if the run had simply ended quietly.
        run_trace.end_with_error(type(exc).__name__, str(exc))
        raise
    else:
        run_trace.record_outcome(report)
        if report.error is not None or report.stop_reason == "error":
            error_type = (report.error or "error").split(":", 1)[0].strip() or "error"
            run_trace.end_with_error(error_type, report.error or "the loop stopped with an error")
        else:
            run_trace.end()
    finally:
        telemetry.flush()
        telemetry.shutdown()

    console = Console()
    render(report, console)
    path = report.write(args.results_dir)
    console.print(f"\nreport: {path}")
    if telemetry.enabled and run_trace.trace_id:
        print(f"trace: {run_trace.trace_id} conversation: {conversation_id}", file=sys.stderr)

    if report.error or report.stop_reason != "report":
        return 1
    return 1 if report.validation_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
