# Receipts

An investigation agent for Honeycomb that has to show its work, plus an eval harness that grades
it on outcomes.

The agent follows Honeycomb's own investigation playbook (Orient, Characterize, BubbleUp, Traces,
Verify by negation, Record) against the hosted Honeycomb MCP. Every hypothesis it reports must
cite the query and the rows that support it, must carry a negation query that was actually run,
and the report must list what was in scope but not checked.

Alongside it, a fault-injectable telemetry generator emits scripted incidents into a real Honeycomb
environment, so the grader knows the true root cause, the affected population, and the onset. The
score rewards a correct, cited conclusion and punishes a confident wrong one harder than a hedged
wrong one. The agent's own loop is traced with the OpenTelemetry GenAI semantic conventions, so
each investigation renders in Honeycomb's Agent Timeline.

## Results

Ten scenarios, three repeats each, one config, two providers, the same prompt and the same grader
for both. Two of the ten are controls where nothing happens and the right answer is no incident.
Every scenario carries a red herring, a pre-existing pattern that is not the cause, and in two
(`herring-customer-whale` and `herring-region-vs-version`) the herring is larger by count than
the true cause. `trigger-checkout-latency` has a Honeycomb trigger that fires during the window.
The scenario files are in `gen/scenarios/`. The generated report is `evals/report.md`, and every
number in this section is read from it.

| provider | model | total | outcome | top right | mean calls | mean wall s | total cost USD |
|---|---|---|---|---|---|---|---|
| anthropic | claude-sonnet-4-5 | 0.82 | 0.72 | 29 of 30 | 25.3 | 187 | 16.19 |
| nvidia | nvidia/nemotron-3-super-120b-a12b | -0.01 | 0.16 | 6 of 30 | 33.9 | 352 | 0.00 |

`total` is the grader's full score between -1 and 1; `evals/grader.md` explains the weights and
penalties. `outcome` is the dims, span, incident, and onset components alone, at most 0.75.
`top right` counts runs whose top hypothesis scored at least 0.5 on dims. A run that filed
nothing, on a cap or on a `submit_report` that never matched the schema, scores as no answer on
a control and on an incident alike. A wrong top hypothesis at high confidence costs 0.50, at
medium 0.25, at low 0.10; a report the validator rejected twice costs 0.25.

### Sonnet

29 of 30 runs named the right cause or, on a control, held back. The miss is `control-noisy`
repeat 2, total -0.50. That scenario's herring is a one minute burst of paypal errors that stops
on its own, and the agent filed a high confidence incident on `payments.charge` errors with no
dimension narrower than the span name. The other two repeats on the same data said no incident.

Thirteen of the 30 runs are `report (validation failed)` rows, each costing 0.25. Most are
`partially_checked` entries claiming a dimension was never read a certain way when one of the
run's own queries had read it exactly that way. Several list instrumentation columns such as
`span.kind` as not checked. One, `dependency-inventory-db-timeouts` repeat 1, ran a negation
that excluded nothing, so the receipts rule failed and the run scored 0.50. The answer was right
in twelve of the thirteen; the thirteenth is the `control-noisy` miss above. In the twelve, what
was wrong was the agent's account of what it had and had not checked, and that account is the
part a reader has to trust.

### Nemotron

Of 30 runs, 10 never filed: the last `submit_report` failed the schema, with the hypotheses list
sent as a JSON string under a field named `hypothesis`, or `onset_estimate` sent as the string
`none`. Eight more stopped with nothing filed, three on the call cap and five on the 480 second
wall cap. Twelve filed. Six of those named the right top hypothesis; four named a wrong one at
high confidence, one at medium, one at low with an empty dims map. Six of the twelve failed the
receipts rule on the negation itself: two cited their evidence query as its own negation, three
ran a negation that excluded nothing, one had no dims to negate. In the column's tool logs, 158
of 766 `run_query` calls came back flagged as errors by the server. 25 of the 30 runs pass
Honeycomb's process evaluator and score under 0.50 here.

The column also exposed a grader bug. A second schema rejection used to end the loop with
`stop_reason` `report` and an empty report whose default `incident_present` of false scored as
restraint on a control; five nemotron control cells scored 0.50 that way without ever filing. The
loop now stops on `schema` and the grader treats every stop but `report` as no answer. The Sonnet
column regrades to the same 30 totals; the nemotron mean is -0.01.

A low score here is what the harness is for. The same prompt on a weaker model produces reports
the receipts rule rejects, and the grader says so instead of averaging it away.

### Ollama

The provider is built behind the same interface and verified for tool call shape on
`qwen3.8:27b`. One cell ran, `trigger-checkout-latency`: it hit the 20 minute wall cap at 40
calls with 854k input tokens and filed nothing, total 0.000. At about 14 tokens per second the
model re-reads the whole context every turn and cannot finish inside the budget, so the full pass
was not run; it would have been about ten hours of zeros. The cell is parked at
`evals/results/ollama-partial/` and is not in the report.

### NVIDIA NIM

`moonshotai/kimi-k2-instruct` returned HTTP 410, end of life 2026-05-12. `moonshotai/kimi-k3`
made a well formed `run_query` call on the first probe, then a smoke run died after five
completions on HTTP 429 with no Retry-After and stayed at 429 for over five minutes while other
models on the same key answered: a per-model quota, not the 40 requests per minute limit.
`nvidia/nemotron-3-super-120b-a12b` is the default. The developer tier is rate limited, not
metered, so its cost column reads $0.

### Reproducing

```
uv run python -m evals.run --scenarios all --configs full --repeats 3
uv run python -m evals.run --scenarios all --configs full --repeats 3 --provider nvidia
uv run python -m evals.report
```

The second command writes its column under a separate results directory; the report keys columns
by provider when more than one is present.

## Status

Built: the generator with ten scenarios, the investigator on three providers, the grader, the eval
runner, self-telemetry into Agent Timeline, and the generated report. Next is the noise floor step
(EDW-1366), with a before and after pass on the same ten scenarios.
