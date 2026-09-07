# Receipts

## What this is

Receipts is an investigation agent for Honeycomb that has to show its work, plus an eval harness
that grades it on outcomes. A fault-injectable generator emits scripted incidents into a real
Honeycomb environment, so a file holds the true root cause, the affected population, and the onset.
The agent works the incident over the hosted Honeycomb MCP, following Honeycomb's playbook. Every
hypothesis it reports has to cite the query and the rows behind it and a negation query that was
actually run, and the report has to list what was in scope and never checked. The grader charges
more for a confident wrong answer than for a hedged one. The agent's own loop traces into the same
environment with the OpenTelemetry GenAI conventions.

## The argument

Honeycomb publishes evals for its investigation skill in `honeycombio/agent-skill`. The scorer in
`tests/scenarios/evaluator.py` weights required tools 0.30, required argument patterns 0.25,
anti-patterns 0.20, tool ordering 0.15, and recommended tools 0.10, and passes at 0.6. That is a
fair measure of whether an agent worked the problem the way Honeycomb works it, and the right thing
to check for a shipped skill. Nothing in it reads the answer. A run that calls the right tools in
the right order and names the wrong cause at high confidence passes.

Kale Bogdanovs named the failure mode in
[Evaluating Observability Tools for the AI Era](https://www.honeycomb.io/blog/evaluating-observability-tools-for-the-ai-era):
"An AI reasoning from incomplete information gives you confident but incorrect answers." The post
is about what to ask of the data, and it treats sampling as a trade-off to make with care. This
project takes the sentence one step further. With every event stored, the agent still picks which
slice to look at, and it narrates whatever it looked at as though that were the whole picture. The
report reads like a finished answer either way, so a person reading it is not the check.

That leaves two places to put the check. One is the report: a hypothesis is reportable only with a
`query_id` from a `run_query` that actually ran and a negation query that ran too, and the report
has to enumerate the dimensions and services in scope and never queried. Both are validated in code
against the run's own tool log, not by a model.

The other is the score. A wrong top hypothesis costs 0.50 at high confidence and 0.10 at low, so
overclaiming is the expensive move. Being right and hedging costs a little too, 0.05 at medium and
0.10 at low, which stops an agent from marking everything low and tying a confident right answer.
The ordering the project rests on is confident-wrong below hedged-wrong below hedged-right below
confident-right, and a unit test pins it.

## How it works

```
gen/scenarios/*.yml          gen/emit.py            Honeycomb receipts-demo
  fault + ground truth  ---->  OTLP/HTTP  ---------->  dataset receipts-shop
        |                                                    ^        |
        |                                    read tools only |        | hosted MCP
        |                                                    |        v
        |                                              agent/loop.py
        |                             orient -> characterize -> bubbleup -> trace
        |                             -> verify by negation -> report
        |                                                             |
        |                                              agent/telemetry.py
        |                                              gen_ai.* spans back to
        |                                              the same environment
        v                                                             |
  evals/grader.py  <--------------- agent/report.py Report <----------+
        |
        +----> evals/results/<config>/<scenario>/<n>/  ----> evals/report.md
```

The generator is synthetic and deterministic, four services from gateway to inventory-db, and its
ground truth is a YAML file. Root spans carry high-cardinality attributes so BubbleUp has something
to find, and every scenario carries a red herring that is not the cause. The scenario id never
reaches the wire, because its values read as answers.

The investigator runs the six steps of Honeycomb's `production-investigation` skill against the
hosted MCP over an allowlist of read tools, discovering columns before assuming names and combining
calculations into one query the way the skill asks, plus the receipts rule and the not-checked list
above. After a report is filed and graded, `evals/run.py --handoff` hands the investigation to
Honeycomb's Canvas over a user OAuth session, creating a board of the evidence queries and asking
Canvas Agent what it makes of the same window.

The grader scores a filed report against ground truth with no model in the loop, so the same report
always grades to the same number.

| component | weight |
|---|---|
| Top hypothesis dims match ground truth, Jaccard over `key=value` pairs | 0.35 |
| Correct slow or failing span named | 0.15 |
| `incident_present` correct | 0.15 |
| Onset estimate within three minutes of the true onset | 0.10 |
| Every reported hypothesis carries evidence and a negation | 0.15 |
| Not-checked list non-empty and truthful against the tool log | 0.10 |

Penalties are applied after weighting and subtract from that total, which floors at -1.0.

| penalty | cost |
|---|---|
| Top hypothesis wrong at `high` confidence | 0.50 |
| Top hypothesis wrong at `medium` | 0.25 |
| Top hypothesis wrong at `low` | 0.10 |
| Top hypothesis right but hedged to `medium` | 0.05 |
| Top hypothesis right but hedged to `low` | 0.10 |
| Any hypothesis with zero evidence | 0.25, capped at 0.50 |
| A report the validator rejected twice | 0.25 |

Every weight and penalty, and the symptom rule that keeps a true-but-not-causal dimension from
counting either way, are explained in `evals/grader.md`. The agent's own loop is traced with the
OpenTelemetry GenAI semantic conventions into the same environment it is investigating, one
`invoke_agent` span per run keyed by `gen_ai.conversation.id`, which is what Honeycomb's Agent
Timeline groups on.

## Results

Ten scenarios, three repeats each, one config, two providers, one grader. Two of the ten are
controls where nothing happens and the right answer is no incident. In `herring-customer-whale` and
`herring-region-vs-version` the red herring is larger by count than the true cause, and
`trigger-checkout-latency` fires a real Honeycomb trigger during the window. Every number below is
read from `evals/report.md`, which is generated from the `grade.json` files with nothing typed by
hand.

| provider | model | total | outcome | top right | mean calls | mean wall s | total cost USD |
|---|---|---|---|---|---|---|---|
| anthropic | claude-sonnet-4-5 | 0.91 | 0.74 | 30 of 30 | 24.5 | 181 | 16.25 |
| nvidia | nvidia/nemotron-3-super-120b-a12b | -0.01 | 0.16 | 6 of 30 | 33.9 | 352 | 0.00 |

`total` is the grader's full score with penalties, between -1 and 1. `outcome` is the dims, span,
incident, and onset components alone, at most 0.75. `top right` counts runs whose top hypothesis
scored at least 0.5 on dims.

### Scores by scenario

| scenario | full (anthropic) total | full (anthropic) outcome | full (anthropic) top right | full (nvidia) total | full (nvidia) outcome | full (nvidia) top right |
| --- | --- | --- | --- | --- | --- | --- |
| checkout-error-surge-adyen | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.45 (-0.10 to 0.85) | 0.45 (0.15 to 0.60) | 2 of 3 |
| control-noisy | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | -0.25 (-0.25 to -0.25) | 0.00 (0.00 to 0.00) | 0 of 3 |
| control-quiet | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | -0.25 (-0.25 to -0.25) | 0.00 (0.00 to 0.00) | 0 of 3 |
| dependency-inventory-db-timeouts | 0.78 (0.60 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | -0.25 (-0.50 to 0.00) | 0.08 (0.00 to 0.25) | 0 of 3 |
| deploy-regression-v260 | 0.95 (0.85 to 1.00) | 0.70 (0.60 to 0.75) | 3 of 3 | 0.35 (-0.25 to 0.85) | 0.40 (0.00 to 0.60) | 2 of 3 |
| error-surge-exceptions | 0.78 (0.60 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | -0.22 (-0.40 to 0.00) | 0.08 (0.00 to 0.25) | 0 of 3 |
| herring-customer-whale | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.20 (-0.25 to 0.85) | 0.28 (0.00 to 0.60) | 1 of 3 |
| herring-region-vs-version | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.20 (0.00 to 0.60) | 0.20 (0.00 to 0.60) | 1 of 3 |
| payments-stripe-v251-uswest | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0 of 3 |
| trigger-checkout-latency | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | -0.30 (-0.40 to -0.25) | 0.08 (0.00 to 0.25) | 0 of 3 |
| all scenarios | 0.91 (0.60 to 1.00) | 0.74 (0.60 to 0.75) | 30 of 30 | -0.01 (-0.50 to 0.85) | 0.16 (0.00 to 0.60) | 6 of 30 |

### Process by config

| config | runs | crashed | mean calls | mean tokens in | mean tokens out | mean cost USD | total cost USD | mean wall s | coerced | passes theirs, fails ours (total < 0.50) | wall cap s | malformed calls |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full (anthropic) | 30 | 0 | 24.5 | 547 | 9,935 | 0.54 | 16.25 | 181 | 12 | 0 of 30 | 480 |  |
| full (nvidia) | 30 | 0 | 33.9 | 1,501,110 | 26,815 | 0.00 | 0.00 | 352 |  | 25 of 30 | 480 |  |

The rules did their work on the Sonnet column: thirty of thirty runs named the right cause or, on a
control, said there was no incident, and the two scenarios where the herring outweighs the cause
scored 0.92 and 1.00. What the rules did not fix is the report's account of itself. Nine of the
thirty Sonnet runs lost 0.25 to a validator rejection, the answer was right in all nine, and the
not-checked list was the part that was wrong. The nemotron column is the case for grading the
outcome: 25 of its 30 runs pass Honeycomb's process evaluator and score under 0.50 here, and its
mean total is -0.01 against Sonnet's 0.91. It is also slower, 352 seconds a run against 181, and
eighteen of its thirty runs never filed a report at all. The run-by-run reading is in
[`docs/results.md`](docs/results.md).

## Run it

You need a Honeycomb ingest key and a management v2 key with `mcp:read` for the environment, an
Anthropic API key, Python 3.12, and `uv`. Copy `.env.example` to `.env` and fill in the names it
lists.

```
uv sync
uv run python -m gen.emit --scenario payments-stripe-v251-uswest
uv run python -m agent --scenario payments-stripe-v251-uswest --run-id <the id emit printed>
uv run python -m evals.run --scenarios all --configs full --repeats 3
uv run python -m evals.report
```

## What it does not do

- No memory across investigations: every run starts cold and learns nothing from the run before it.
- No spatial awareness of the canvas: the agent can put a board and a question on Canvas, and
  cannot see what is already laid out there.
- Synthetic traffic only: the incidents are scripted, so nothing here says how the agent behaves
  on production traffic it has never seen.
- One team's data model: everything assumes the shape of `receipts-shop`, one dataset with a known
  set of columns.
- Single agent: one loop, one investigation, no work claims and no awareness of other agents
  working the same incident.

## Credits

The investigation method is Honeycomb's, from the `honeycomb-investigator` agent and the
`production-investigation` skill in `honeycombio/agent-skill`. The process score used for contrast
reimplements the `evaluate` function in that repo's `tests/scenarios/evaluator.py`; the commit it
was read from is recorded in `evals/grader.md`. The self-telemetry follows the OpenTelemetry GenAI
semantic conventions rather than a scheme of my own.

The receipts rule is a port of the fact checker in Agent Blue, an operations platform I built for a
conservation nonprofit. A second pass there classifies every sentence of a generated document as
grounded, partial, or unsupported and holds the document until a person clears the flagged ones.
That vocabulary became the confidence levels here, and holding the document became refusing to
report a hypothesis with no query behind it.

Receipts was built with Claude Code: an orchestrator session, an implementer subagent per issue on
the model that issue is labeled for, and an adversarial review before every PR.
`docs/agentic-workflow.md` reports what the transcripts show the work re-deriving by hand and what
`.claude/` now carries so it stops, with every number in it printed by `tools/usage_stats.py` from
the transcripts.
