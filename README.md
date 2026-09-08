# Receipts

## What this is

Receipts is an investigation agent for Honeycomb that has to show its work, plus an eval harness
that grades it on outcomes. A fault-injectable generator emits scripted incidents into a real
Honeycomb environment, so a file holds the true root cause, population, and onset. The agent works
it over the hosted Honeycomb MCP with Honeycomb's playbook. Every
hypothesis cites the query, its rows, and a negation query that ran, and the report
lists what was in scope and never checked. The grader charges more for a confident wrong answer
than a hedged one.

## Recording

The three-minute recording at the
[v0.1 release](https://github.com/eknuth/receipts/releases/download/v0.1/receipts-demo-2026-09.mp4)
shows an incident emitted, the heatmap that finds it, an investigation ending on the filed report,
a graded run in Agent Timeline, and the tables below.

## The argument

Honeycomb's evals for its investigation skill, in `honeycombio/agent-skill`, score process:
`tests/scenarios/evaluator.py` weights required tools 0.30, required argument patterns 0.25,
anti-patterns 0.20, tool ordering 0.15, and recommended tools 0.10, and passes at 0.6. It is a
fair check, and nothing in it reads the answer: a run that calls the right tools in the
right order and names the wrong cause at high confidence passes.

Kale Bogdanovs named the failure mode in
[Evaluating Observability Tools for the AI Era](https://www.honeycomb.io/blog/evaluating-observability-tools-for-the-ai-era):
a tool "needs complete data, because an AI reasoning from incomplete information gives you
confident but incorrect answers." With every event stored, the agent still picks a slice and
narrates it as the whole, and the report reads as finished either way, so a person reading it is
not the check.

That leaves two places to make the report checkable. One is the report itself: a hypothesis needs a `query_id`
from a `run_query` that ran and a negation query that ran too, and the report has to list the
dimensions and services in scope and never queried, both validated in code against the run's tool
log, not a model.

The other is the score. A wrong top hypothesis costs 0.50 at high confidence and 0.10 at low, and
being right but hedged costs 0.05 at medium and 0.10 at low. The ordering, confident-wrong below hedged-wrong below hedged-right below confident-right, is
pinned by a unit test.

## How it works

![architecture](docs/diagrams/architecture.svg)

The generator is deterministic, with ground truth in a YAML file. Root spans carry high-cardinality attributes for BubbleUp, every scenario carries a red herring, and the scenario id never reaches the wire, since its values
read as answers.

The investigator runs the six steps of Honeycomb's `production-investigation` skill against the
hosted MCP over an allowlist of read tools, plus the receipts rule and the not-checked list above.
After grading, `evals/run.py --handoff` hands the report to Honeycomb's Canvas over a user OAuth
session as a board and a question.

![investigation](docs/diagrams/investigation.svg)

The grader scores a filed report against ground truth with no model in the loop.

| component | weight |
|---|---|
| Top hypothesis dims match ground truth, Jaccard over `key=value` pairs | 0.35 |
| Correct slow or failing span named | 0.15 |
| `incident_present` correct | 0.15 |
| Onset estimate within three minutes of the true onset | 0.10 |
| Every reported hypothesis carries evidence and a negation | 0.15 |
| Not-checked list non-empty and truthful against the tool log | 0.10 |

Penalties subtract from the weighted total, floored at -1.0.

| penalty | cost |
|---|---|
| Top hypothesis wrong at `high` confidence | 0.50 |
| Top hypothesis wrong at `medium` | 0.25 |
| Top hypothesis wrong at `low` | 0.10 |
| Top hypothesis right but hedged to `medium` | 0.05 |
| Top hypothesis right but hedged to `low` | 0.10 |
| Any hypothesis with zero evidence | 0.25, capped at 0.50 |
| A report the validator rejected twice | 0.25 |

Every weight and penalty is explained in `evals/grader.md`. The agent's own loop is traced with
OpenTelemetry GenAI conventions into the same environment, keyed by `gen_ai.conversation.id`,
which Agent Timeline groups on.

## Results

Ten scenarios, three repeats each, three configs on Sonnet and one on nemotron, one grader. Two
scenarios are controls where the right answer is no incident, and in two the red herring outweighs
the cause by count. `no-negation` drops the
negation rule, `no-notchecked` drops the not-checked list, and the Sonnet columns ran on 2026-09-08
on the same code and, apart from the trigger scenario, the same emitted data. Every number below is
read from `evals/report.md`, generated from the grade files.

| config | model | total | outcome | top right | mean calls | mean wall s | total cost USD |
|---|---|---|---|---|---|---|---|
| full | claude-sonnet-4-5 | 0.92 | 0.74 | 30 of 30 | 24.2 | 188 | 16.09 |
| no-negation | claude-sonnet-4-5 | 0.87 | 0.74 | 29 of 30 | 25.6 | 189 | 16.58 |
| no-notchecked | claude-sonnet-4-5 | 0.93 | 0.75 | 30 of 30 | 22.7 | 167 | 14.11 |
| full | nvidia/nemotron-3-super-120b-a12b | -0.01 | 0.16 | 6 of 30 | 33.9 | 352 | 0.00 |

`outcome` is the answer components alone, at most 0.75; `top right` counts top hypotheses at or
above 0.5 on dims.

### Scores by scenario

| scenario | full (anthropic) total | full (anthropic) outcome | full (anthropic) top right | full (nvidia) total | full (nvidia) outcome | full (nvidia) top right | no-negation (anthropic) total | no-negation (anthropic) outcome | no-negation (anthropic) top right | no-notchecked (anthropic) total | no-notchecked (anthropic) outcome | no-notchecked (anthropic) top right |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| checkout-error-surge-adyen | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.45 (-0.10 to 0.85) | 0.45 (0.15 to 0.60) | 2 of 3 | 0.83 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| control-noisy | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | -0.25 (-0.25 to -0.25) | 0.00 (0.00 to 0.00) | 0 of 3 | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| control-quiet | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | -0.25 (-0.25 to -0.25) | 0.00 (0.00 to 0.00) | 0 of 3 | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.88 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| dependency-inventory-db-timeouts | 0.60 (0.60 to 0.60) | 0.75 (0.75 to 0.75) | 3 of 3 | -0.25 (-0.50 to 0.00) | 0.08 (0.00 to 0.25) | 0 of 3 | 0.53 (0.00 to 1.00) | 0.63 (0.40 to 0.75) | 2 of 3 | 0.67 (0.50 to 0.90) | 0.75 (0.75 to 0.75) | 3 of 3 |
| deploy-regression-v260 | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.35 (-0.25 to 0.85) | 0.40 (0.00 to 0.60) | 2 of 3 | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.97 (0.90 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| error-surge-exceptions | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | -0.22 (-0.40 to 0.00) | 0.08 (0.00 to 0.25) | 0 of 3 | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.97 (0.90 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| herring-customer-whale | 0.88 (0.82 to 1.00) | 0.63 (0.57 to 0.75) | 3 of 3 | 0.20 (-0.25 to 0.85) | 0.28 (0.00 to 0.60) | 1 of 3 | 0.80 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.88 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| herring-region-vs-version | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.20 (0.00 to 0.60) | 0.20 (0.00 to 0.60) | 1 of 3 | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| payments-stripe-v251-uswest | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0 of 3 | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| trigger-checkout-latency | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | -0.30 (-0.40 to -0.25) | 0.08 (0.00 to 0.25) | 0 of 3 | 0.87 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 | 0.97 (0.90 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| all scenarios | 0.92 (0.60 to 1.00) | 0.74 (0.57 to 0.75) | 30 of 30 | -0.01 (-0.50 to 0.85) | 0.16 (0.00 to 0.60) | 6 of 30 | 0.87 (0.00 to 1.00) | 0.74 (0.40 to 0.75) | 29 of 30 | 0.93 (0.50 to 1.00) | 0.75 (0.75 to 0.75) | 30 of 30 |

### Process by config

| config | runs | crashed | mean calls | mean tokens in | mean tokens out | mean cost USD | total cost USD | mean wall s | coerced | passes theirs, fails ours (total < 0.50) | wall cap s | malformed calls |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full (anthropic) | 30 | 0 | 24.2 | 544 | 9,925 | 0.54 | 16.09 | 188 | 7 | 0 of 30 | 480 |  |
| full (nvidia) | 30 | 0 | 33.9 | 1,501,110 | 26,815 | 0.00 | 0.00 | 352 |  | 25 of 30 | 480 |  |
| no-negation (anthropic) | 30 | 0 | 25.6 | 550 | 10,300 | 0.55 | 16.58 | 189 | 7 | 1 of 30 | 480 |  |
| no-notchecked (anthropic) | 30 | 0 | 22.7 | 528 | 8,755 | 0.47 | 14.11 | 167 | 4 | 0 of 30 | 480 |  |

Thirty of thirty Sonnet runs in the full column named the right cause or, on a control, said
there was no incident, and the two herring scenarios scored 0.88 and 0.92. Neither rule raises the
outcome. The yardstick is the same-rules rerun, which moved outcome 0.007 and total 0.013 from
the 2026-09-06 column to this one; the ablations moved outcome 0.000 and 0.012, neither separable
from that drift on thirty cells. Without the negation rule total falls
from 0.923 to 0.870, all of it penalties and receipts. The ten validation failures against six all
fell on the partially-checked rule, which the ablation left in place; whether the negation bullet
changes how the model writes that list is not claimable from thirty runs. Every hypothesis
in the column still carries a negation query, with full receipts in 27 of 30 cells in both columns,
so the playbook puts that query there, not the rule. What the rule did is one validator check, off
with it: a hypothesis with a negation has to name its population. In full that check rejected all
three `dependency-inventory-db-timeouts` drafts, which came back naming `name = db.query`; in
no-negation the same run filed no dimension at high confidence and scored 0.00. Without the
not-checked rule total rises by 0.010, with four validation failures against six, 22.7 calls a run
against 24.2, and $14.11 against $16.09. The list stays in every report, since the ablation removes
the prompt block and the validator check, not the field; the rule is what makes it true:
7 of 30 lists false without it, 0 of 30 with it. The rules were never claimed to raise accuracy.
They make the report checkable, and that is what the ablation measured.

What it did not fix is the report's account of itself: in the full column six of the thirty runs
paid the 0.25 validation penalty with the right answer filed, three for a negation that excluded
nothing on the claim's own dimension, which also cost the receipts weight and left them at 0.60,
and three for the report's lists of what it checked, naming an instrumentation column or a span
name as a column. The nemotron column is the case for grading the outcome: 25 of its 30 runs pass
Honeycomb's process evaluator and score under 0.50 here, and eighteen of its thirty runs never
filed a report. NVIDIA's endpoint spent 2026-09-08 returning server errors, so that column is from
2026-09-07, before the noise floor step and the schema fix. The run-by-run reading is in
[`docs/results.md`](docs/results.md).

## Run it

You need a Honeycomb ingest key, a management v2 key with `mcp:read`, an Anthropic API key,
Python 3.12, and `uv`. Copy `.env.example` to `.env`; the environment
`HONEYCOMB_ENV` names, default `receipts-demo`, must already exist.
`trigger-checkout-latency` grades low unless your team has the trigger its scenario file
describes. The fourth command emits and grades everything, $16.09 on Sonnet.

```
uv sync
uv run python -m gen.emit --scenario payments-stripe-v251-uswest
uv run python -m agent --scenario payments-stripe-v251-uswest --run-id <the id emit printed>
uv run python -m evals.run --scenarios all --configs full --repeats 3 --emit
uv run python -m evals.report
```

## What it does not do

- No memory: every run starts cold.
- No view of Canvas: the agent posts and cannot see what is there.
- Synthetic traffic only: scripted incidents say nothing about production.
- One data model: everything assumes `receipts-shop`.
- Single agent: one loop, no awareness of others on the incident.

## Credits

The method is Honeycomb's, from the `honeycomb-investigator` agent and the
`production-investigation` skill in `honeycombio/agent-skill`. The process score reimplements
`evaluate` in that repo's `tests/scenarios/evaluator.py`, at the commit named in `evals/grader.md`.

The receipts rule is a port of the fact checker in Agent Blue, an operations platform I built for a
conservation nonprofit, which classifies every claim in a document as grounded, partial, or
unsupported and holds it until a person clears the flagged ones; that vocabulary became the
confidence levels here, and the hold became refusing a hypothesis with no query behind it.

Receipts was built with Claude Code: an orchestrator session, an implementer subagent per issue,
and an adversarial review before every PR. `docs/agentic-workflow.md` has the numbers, from
`tools/usage_stats.py`.

## License

MIT, see LICENSE.
