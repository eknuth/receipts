---
name: pass-run
description: Run a before-and-after eval pass in the shape every method change since EDW-1364 has used. Checks the branch, month-to-date ingest, and that no emit is running; parks the current evals/results/full column four levels deep; emits the trigger scenario once per cell for three cells; runs the other nine scenarios at three repeats; renders the report. Use when Ed says "start the pass", "run the after-pass", or an issue's acceptance calls for a before-and-after on all ten scenarios.
---

# Run a pass

What this replaces. Six scratchpad scripts (`afterpass.sh`, `afterpass2.sh`, `ollamapass.sh`,
`nvidiapass.sh`, `r19pass.sh`, `r19wait.sh`) carry this shape, each retyped in a new session.
The transcripts show sixteen emit commands and a dozen moves of a results column. One park was made at
three levels, which the report glob counted as a live config, and one NVIDIA pass was queued
so that its emits could overlap the Anthropic pass. Hooks now block both; this skill is the
shape that does not trip them.

Arguments: `<park>` the name for the column being replaced (`r18-pass`, `matrix2-ba6518b`),
and optionally `--provider <name>` for a non-Anthropic column.

## Before starting

1. Branch: `git branch --show-current` must be the issue branch, never `main`. The code
   under test is what is checked out; write its short SHA into the log's first line.
2. No emit running: `pgrep -fl 'evals.run|gen.emit'` must print nothing. The hook
   `block_double_emit.py` refuses the emit command otherwise; do not wait it out with a
   sleep loop that starts the moment the other pass ends, the ingest cap is per second and
   the tail of one pass overlaps the head of the next.
3. Ingest against the free plan (20M events a month). Month-to-date from the manifests:

   ```
   uv run python -c "import json,glob,datetime; m=datetime.datetime.now(datetime.UTC).strftime('%Y-%m'); ds=[json.load(open(p)) for p in glob.glob('gen/runs/*.json')]; print(sum(d.get('exported',0) for d in ds if d.get('emitted_at','').startswith(m)))"
   ```

   A pass emits three trigger runs of about 90,000 events each plus the agent's own spans.
   Refuse the pass and tell Ed when month-to-date plus 300,000 would pass 20M. The 1.3M a
   day figure in older handoffs was self-imposed pacing; Honeycomb lists no daily cap.
4. Cost and time on `claude-sonnet-4-5`: about $16 and 95 minutes for thirty cells. Say so
   before starting; ten of Ed's turns in the transcripts ask about credits, so the number
   belongs in the first line of the reply.

## The pass

Write it to the session scratchpad as one `zsh` script named `<issue>pass.sh` (any
`*pass*.sh`), with `set -e`, run it in the background with output to a log, then poll the log
for the DONE line. The name matters: `block_double_emit.py` sees the launch command, not what
the script will run, and a script launched with `nohup` shows in `pgrep` under its own name
until its first `evals.run` child starts. The hook matches `*pass*.sh` in the command to close
that gap; a pass launched under another name is not covered. Never two passes at once. The shape, with `NINE` the nine non-trigger scenario ids from `gen/scenarios/`:

```
set -e
cd <repo>
test "$(git branch --show-current)" = "<branch>"
echo "code $(git rev-parse --short HEAD) start $(date -u +%FT%TZ)"
if [ -d evals/results/full ]; then
  test ! -e evals/results/<park>
  mkdir -p evals/results/<park>
  mv evals/results/full evals/results/<park>/full
fi
for i in 1 2 3; do
  uv run python -m evals.run --scenarios trigger-checkout-latency --emit --repeats 1
done
uv run python -m evals.run --scenarios $NINE --repeats 3
uv run python -m evals.report
echo "PASS_DONE $(date -u +%FT%TZ)"
```

Why this shape. The trigger scenario is emitted once per cell so the Honeycomb trigger is
firing while that cell investigates; `--emit` and `--resume` do not combine, so it is
three `--repeats 1` invocations. The nine reuse the run ids in `evals/results/runs.json`
so the before and after columns investigate the same data. The park is four levels
(`<park>/full/<scenario>/<n>`) because `evals/report.py` reads `*/*/*/grade.json` and a
three-level park is counted as a live config named `<park>`. A column for another provider
(`full-nvidia`) stays live so the report keeps its columns.

## After

`/compare <park>/full full`, then `/cell full <scenario> <n>` for each cell that moved or
sits under 1.0. The paired mean is the number to report, and every loss is traced to a
query the model ran or did not run before it becomes a finding. Commit `evals/report.md`
and `evals/results/runs.json` on the issue branch and put the numbers on the issue.
