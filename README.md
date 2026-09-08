# Receipts

## What this is

Receipts is an investigation agent for Honeycomb that has to show its work, plus an eval harness
that grades it on outcomes. A fault-injectable generator emits scripted incidents into a real
Honeycomb environment, so a file holds the true root cause, the affected population, and the onset.
The agent works the incident over the hosted Honeycomb MCP with Honeycomb's playbook. Every
hypothesis cites the query, the rows behind it, and a negation query that ran, and the report
lists what was in scope and never checked. The grader charges more for a confident wrong answer
than a hedged one.

## Recording

A three-minute recording is at the
[v0.1 release](https://github.com/eknuth/receipts/releases/download/v0.1/receipts-demo-2026-09.mp4).
It shows a scripted incident emitted, the Honeycomb heatmap that finds it, one investigation
ending on the filed report, a graded run in Agent Timeline with its score on the root span, and
the eval tables below.

## The argument

Honeycomb publishes evals for its investigation skill in `honeycombio/agent-skill`. The scorer in
`tests/scenarios/evaluator.py` weights required tools 0.30, required argument patterns 0.25,
anti-patterns 0.20, tool ordering 0.15, and recommended tools 0.10, and passes at 0.6. That is a
fair check on whether an agent worked the problem the way Honeycomb does. Nothing in it reads the
answer. A run that calls the right tools in the right order and names the wrong cause at high
confidence passes.

Kale Bogdanovs named the failure mode in
[Evaluating Observability Tools for the AI Era](https://www.honeycomb.io/blog/evaluating-observability-tools-for-the-ai-era):
a tool "needs complete data, because an AI reasoning from incomplete information gives you
confident but incorrect answers." The post is a buyer's checklist; complete data is its first
item. With every event stored, the agent still picks which slice to look at and narrates that
slice as though it were the whole picture. The report reads like a finished answer either way, so
a person reading it is not the check.

That leaves two places to put the check. One is the report: a hypothesis is reportable only with a
`query_id` from a `run_query` that ran and a negation query that ran too, and the report has to
enumerate the dimensions and services in scope and never queried. Both are validated in code
against the run's own tool log, not a model.

The other is the score. A wrong top hypothesis costs 0.50 at high confidence and 0.10 at low, so
overclaiming is the expensive move. Being right and hedging costs a little too, 0.05 at medium and
0.10 at low, which stops an agent from marking everything low and tying a confident right answer.
The ordering the project rests on is confident-wrong below hedged-wrong below hedged-right below
confident-right, pinned by a unit test.

## How it works

![architecture](docs/diagrams/architecture.svg)

The generator is synthetic and deterministic, four services from gateway to inventory-db, and its
ground truth is a YAML file. Root spans carry high-cardinality attributes for BubbleUp to find,
and every scenario carries a red herring that is not the cause. The scenario id never reaches the
wire, since its values read as answers.

The investigator runs the six steps of Honeycomb's `production-investigation` skill against the
hosted MCP over an allowlist of read tools, discovering columns before assuming names and combining
calculations into one query, plus the receipts rule and the not-checked list above. After a
report is filed and graded, `evals/run.py --handoff` hands it to Honeycomb's Canvas over a user
OAuth session, building a board of the evidence queries and asking what Canvas Agent makes of the
window.

![investigation](docs/diagrams/investigation.svg)

The grader scores a filed report against ground truth with no model in the loop, so a report
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

Every weight and penalty, and the symptom rule for a true-but-not-causal dimension, are explained
in `evals/grader.md`. The agent's own loop is traced with OpenTelemetry GenAI semantic conventions
into the same environment it is investigating: one `invoke_agent` span per investigation and, with
`--handoff`, a second for the Canvas exchange, both keyed by `gen_ai.conversation.id`, which is
what Honeycomb's Agent Timeline groups on.

## Results

Ten scenarios, three repeats each, one config, two providers, one grader. Two of the ten are
controls where nothing happens and the right answer is no incident. In `herring-customer-whale` and
`herring-region-vs-version` the red herring is larger by count than the true cause, and
`trigger-checkout-latency` fires a real Honeycomb trigger during the window. Every number below is
read from `evals/report.md`, generated from the grade files.

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

Thirty of thirty Sonnet runs named the right cause or, on a control, said there was no incident,
and the two scenarios where the herring outweighs the cause scored 0.92 and 1.00. One config, so
the table says what the method does, not what each rule is worth. What it did not fix is the
report's account of itself: nine of the thirty runs paid the 0.25 validation penalty
with the right answer filed, seven for a not-checked list that named an instrumentation column or
contradicted the run's own queries, and two for a negation query that excluded nothing, which also
cost those two the receipts weight and left them at 0.60. The nemotron column is the case for
grading the outcome: 25 of its 30 runs pass Honeycomb's process evaluator and score under 0.50
here, its mean total is -0.01 against Sonnet's 0.91, it takes 352 seconds a run against 181, and
eighteen of its thirty runs never filed a report. That column ran seven hours before the noise
floor step landed, so it met the earlier prompt and validator. The run-by-run reading is in
[`docs/results.md`](docs/results.md).

## Run it

You need a Honeycomb ingest key, a management v2 key with `mcp:read` for the environment, an
Anthropic API key, Python 3.12, and `uv`. Copy `.env.example` to `.env`. The environment
`HONEYCOMB_ENV` names, default `receipts-demo`, must already exist in your team.
`trigger-checkout-latency` grades low unless your team has the trigger its scenario file's
`trigger:` block describes. The fourth command emits and grades every scenario, $16.25 on Sonnet.

```
uv sync
uv run python -m gen.emit --scenario payments-stripe-v251-uswest
uv run python -m agent --scenario payments-stripe-v251-uswest --run-id <the id emit printed>
uv run python -m evals.run --scenarios all --configs full --repeats 3 --emit
uv run python -m evals.report
```

## What it does not do

- No memory across investigations: every run starts cold.
- No spatial awareness of Canvas: the agent can post a board and a question, not see what is
  already laid out.
- Synthetic traffic only: scripted incidents say nothing about how it does in production.
- One team's data model: everything assumes `receipts-shop`, one dataset with a known set of
  columns.
- Single agent: one loop, one investigation, no work claims, no awareness of others on the
  incident.

## Credits

The investigation method is Honeycomb's, from the `honeycomb-investigator` agent and the
`production-investigation` skill in `honeycombio/agent-skill`. The process score for contrast
reimplements the `evaluate` function in that repo's `tests/scenarios/evaluator.py`; the commit it
was read from is in `evals/grader.md`. The self-telemetry follows OpenTelemetry's GenAI semantic
conventions.

The receipts rule is a port of the fact checker in Agent Blue, an operations platform I built for a
conservation nonprofit. A second pass there classifies every claim in a document as grounded,
partial, or unsupported and holds it until a person clears the flagged ones. That vocabulary
became the confidence levels here; holding a document became refusing a hypothesis with no query
behind it.

Receipts was built with Claude Code: an orchestrator session, an implementer subagent per issue on
its labeled model, and an adversarial review before every PR.
`docs/agentic-workflow.md` reports what the transcripts show the work re-deriving by hand and what
`.claude/` now carries so it stops, every number printed by `tools/usage_stats.py`.

## License

MIT, see LICENSE.
