# Results in detail

The reading behind the tables in the README's Results section. Every number here is read from
`evals/report.md` or from `evals/compare.py` over the same result directories.

The two columns were graded by the same grader, and they did not run the same agent. The
nemotron rows have been in the committed report since de65aa4, seven hours before the noise
floor step below landed in 7d6fb85, so that column exercises the prompt and the validator as
they stood before the step.

## Sonnet

Thirty of the thirty runs named the right cause or, on a control, said there was no incident. All
six control cells filed a report with no hypothesis at all. Mean outcome is 0.74 of a possible
0.75, over 24.5 MCP calls and 181 seconds a run, for $16.25 across the column.

Nine of the thirty are `report (validation failed)` rows, each costing 0.25, and the answer was
right in all nine. Four list an instrumentation column as not checked (`span.kind`,
`span.num_events`, `span.num_links`, `parent_name`, `scenario.id`) when that list is for the
dimensions and spans a person would investigate. Four contradict themselves about a reading: the
report says a column's values were never compared, or were only read inside another filter, and
one of the run's own queries did exactly that. One of those four also names three span names as
if they were columns. What was wrong in every case is the run's account of what it had and had
not checked, and that account is the part a reader has to trust.

Two runs also lost the receipts component, scoring 0.60 instead of 0.75. In
`dependency-inventory-db-timeouts` repeat 3 the claim rests on `name = db.query` and the negation
query filtered `name = payments.charge`. In `error-surge-exceptions` repeat 2 the claim rests on
`payment.provider = adyen` and the negation filtered `payment.provider in [stripe, paypal]`. Both
measured the right complement and neither wrote it as a negation, so the rule that a hypothesis
carries a query with `!=` or `not-in` on one of its own dimensions rejected them. Writing that
query the same way in both places is EDW-1369.

## What the noise floor step moved

The most recent method change added a noise floor step to the fixed part of the prompt: before a
rate counts as a finding, name the population it is measured against and put the expected count
next to the observed one. It also made the validator reject a baseline query whose time range
lies entirely outside the run window.

The column before it is parked at `evals/results/r18-pass/full/`, and the mean total went from
0.825 to 0.910. Most of that is not the noise floor step. The before column ran on 2026-09-05,
ahead of three commits on `main` (2ba4100, 4a2b286, 883b23e) that went at the same class of
failure, and `total` carries the validator's 0.25 penalty. All three changed the validator, and
two of them also changed the `not_checked` and `partially_checked` rules in the prompt, which
leaves the investigation steps alone.

The steps, though, are not the whole of what the model reads. One of those commits, 4a2b286,
changed a line it reads on every query. The `Queried so far` footer under each query result
lists, from that commit on, the values a breakdown returned as well as the terms the query asked
for, so the after column's model read a longer footer: 25 terms against 11 on the first
`payments-stripe-v251-uswest` cell, and a median of 16.5 more per cell across the column. The
footer is there for the `not_checked` list, and it also sits in the context the next query is
chosen from, so it could have moved a query. This pass did not measure whether it did.

Result directories are gitignored except for `runs.json`, so a clone has the code and the
scenarios and not these cells.

What isolates the method change is `outcome`. One grader commit landed between the two columns,
0ca70f8, and it changes only how a run that filed nothing scores; every run in both columns
filed, so it cannot move either one. Outcome went from 0.725 to 0.745, and two of the thirty
cells moved:

- `control-noisy` repeat 2, outcome 0.00 to 0.75 and total -0.50 to 0.75. Before the change the
  run filed a high confidence incident on `payments.charge` errors with no dimension narrower
  than the span name, reading 99 errors in 18,000 `payments.charge` spans, a rate of 0.55
  percent, as an ongoing failure. The generator fails 0.5 percent of requests for ordinary
  reasons and puts half of those on `payments.charge`, so the same count on `control-quiet` is
  36. The other 63 come from the one minute burst of paypal errors this scenario injects, which
  stops on its own. The run added the two together and called the sum an incident that was still
  running. After the change it said there was no incident, which the other two repeats on the
  same data had already said.
- `deploy-regression-v260` repeat 3, outcome 0.75 to 0.60. The run named `HTTP POST /checkout` as
  the slow span where the run before it named `checkout.process`, which is the ground truth. The
  root cause dimension was right in both.

The step also cost something. Both negations described above are new in this column,
`dependency-inventory-db-timeouts/3` going 0.75 to 0.60 and `error-surge-exceptions/2` going 1.00
to 0.60, and step 5 asks for the rows outside the candidate over the whole window, which is the
query both runs then filed as the negation the receipts rule rejected. Set against the outcome
gain that is -0.55 of `total`, and EDW-1369 is the fix.

So the step is worth 0.60 of outcome across thirty runs, won in one cell and partly given back in
another, against a likely -0.55 on total. Both columns are separate runs of a stochastic agent
over the same data, so one cell moving does not prove the change caused it. What can be said is
that the control failure the change was aimed at stopped happening, and that nothing else in the
outcome components moved except one span name.

## Nemotron

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
loop now stops on `schema` and the grader treats every stop but `report` as no answer. No Sonnet
run has stopped that way, so that column is untouched by the fix; the nemotron mean is -0.01.

A low score here is what the harness is for. The same method on a weaker model produces reports
the receipts rule rejects, and the grader says so instead of averaging it away.

## Ollama

The provider is built behind the same interface and verified for tool call shape on
`qwen3.8:27b`. One cell ran, `trigger-checkout-latency`: it hit the 20 minute wall cap at 40
calls with 854k input tokens and filed nothing, total 0.000. At about 14 tokens per second the
model re-reads the whole context every turn and cannot finish inside the budget, so the full pass
was not run; it would have been about ten hours of zeros. The cell is parked at
`evals/results/ollama-partial/` and is not in the report.

## NVIDIA NIM

`moonshotai/kimi-k2-instruct` returned HTTP 410, end of life 2026-05-12. `moonshotai/kimi-k3`
made a well formed `run_query` call on the first probe, then a smoke run died after five
completions on HTTP 429 with no Retry-After and stayed at 429 for over five minutes while other
models on the same key answered: a per-model quota, not the 40 requests per minute limit.
`nvidia/nemotron-3-super-120b-a12b` is the default. The developer tier is rate limited, not
metered, so its cost column reads $0.

## An answer in the tool schema, 2026-09-07

The `submit_report` tool schema is generated from `ReportDraft`, and pydantic copies class
docstrings and field descriptions into it. From ce34615 (EDW-1365, 2026-09-05 16:04) the
`PartialCheck` docstring carried "I broke down on `cart.size` and did not look at individual
values below 8" as its worked example, and the `subject` description named `cart.size`,
`deployment.version`, and `us-west-2` as examples. `cart.size >= 8` is the ground truth of
`trigger-checkout-latency`, and the other two are root-cause dimensions elsewhere. No test read
the schema, so nothing caught it until 2026-09-07. The `full` column in `evals/report.md` ran on
2026-09-06 under that schema, and so did `r17-pass`, `r18-pass`, and `full-nvidia`.

The recorded cells say how far it reached. Eight reports across those columns carry `cart.size`
as a `partially_checked` subject, six of them in scenarios where cart size is not the answer
(`deploy-regression-v260` twice, `herring-region-vs-version` twice, `payments-stripe-v251-uswest`,
`control-quiet`). That is the example being copied, since nothing in those runs' data points at
that column. On the trigger scenario itself the example had nothing to give. Every Sonnet `full`
cell on `trigger-checkout-latency` scored dims 1.0 from matrix2 (2026-09-05 00:55) on, fifteen
cells across five columns, and the earliest of those predates the commit by fifteen hours. The
matrix2 ablation columns hold one 0.0 and one 0.5 on the same scenario, also before the leak.

The sentence itself came from the model. `r16-pass2/full/trigger-checkout-latency/3`, written at
or before 15:49 on 2026-09-05 and with no `partially_checked` field in it at all, lists
"individual cart.size values below 8 to see if there's a threshold between 7 and 8" under
`not_checked`, where the validator rejected it for naming a column the run had queried. The
docstring quoted that rejected entry back as the example of what the new field was for. So the
example did not manufacture the trigger result, and it did put one column name in front of every
run that had no reason to mention it. The schema now uses `http.route` and an invented
`/api/v2/search`, `tests/test_answer_leak.py` checks every ground-truth value and fault column
against the prompt, the schema, the tool description, and the validator's messages, and the
columns are being rerun on the fixed schema.

## The grader was brittle on spelling, 2026-09-08

The truth for `trigger-checkout-latency` is `cart.size: ">=8"`, and the grader compared a
reported value to it three ways: the same string, the same range written the same way, or a
single number inside the range. In the rerun on the fixed schema two Sonnet cells found the
right span, the right onset, ran the negation, and wrote the population as `cart.size: 8-12`
(`full/trigger-checkout-latency/1`) and `cart.size: 8, 9, 10, 11, 12`
(`full/trigger-checkout-latency/3`). Cart sizes run 1 to 12, so both select exactly the rows
`>=8` does. The grader scored dims 0 and charged 0.50 for confident and wrong, so two cells that
were right all the way through scored 0.15. An earlier cell,
`matrix2-ba6518b/no-negation/trigger-checkout-latency/1` from before the schema leak, wrote
`8, 9, 10, 11, or 12` and was graded wrong the same way.

Every other trigger cell wrote `>=8` or `>= 8`, and the reason is the leak in the section above:
the `PartialCheck` docstring taught that spelling. Once the example was gone the model wrote the
same answer in its own words and the grader could not read it. So the leak was covering for a
grader that only accepted one spelling of a range.

The rule now is that a reported value satisfies a ground-truth range when it selects exactly the
same rows. `gen.topology.selects` reads a range, a single integer, or a spelled-out set as the set
of values it names on a column with a fixed value set, and the grader and the validator both use
it, so a claim written as `8-12` is negated by `cart.size < 8` the way `>=8` is. Same rows, same
claim. A subset such as `8, 9` claims a narrower population and does not match, a superset such as
`7, 8, 9, 10, 11, 12` claims a wider one and does not match, and a value on a column with no fixed
value set falls back to the old spelling rule. The nvidia cell
`r26-pass/full-nvidia/trigger-checkout-latency/1` wrote `8-11`, a subset, and stays wrong. The
report schema was not changed for this; the grader reads the spellings the model already uses,
and a second schema change would have confounded the next pass against the last one. Each report
now records `schema_hash`, the first twelve hex characters of the sha256 of the `submit_report`
schema it was shown, and the grade carries it, so cells from different schemas can be told apart.

The grader's knowledge of the value domain comes from the generator, which wrote every cart size
on the wire. In a real dataset that knowledge would be a distinct-values query the agent has to
run before it can say two spellings are one claim. Every column under `evals/results/` was
regraded under this rule before the numbers in the README were read.

<!-- regrade numbers -->

## Reproducing

```
uv run python -m evals.run --scenarios all --configs full --repeats 3
uv run python -m evals.run --scenarios all --configs full --repeats 3 --provider nvidia
uv run python -m evals.report
uv run python -m evals.compare r18-pass/full full
```

The two run commands write to `evals/results/full/`, so run the second with `--results-dir` pointing
somewhere else and copy its `full/` directory in beside the first as `evals/results/full-nvidia/`.
The report keys columns by provider when more than one is present.

## Server behaviors the client works around

Facts about the hosted Honeycomb MCP and Agent Timeline that shaped the client code, with the
date each was checked. CLAUDE.md's "Honeycomb facts" holds the rest.

- 2026-09-04: `run_query`'s `query_spec` takes its time bounds as `from` and `to`. The older
  `start_time` and `end_time` names are rejected with an error naming the rename. Stored tool
  logs from before the rename carry the old names, and `agent/format.py` renders both.
- 2026-09-04: `run_bubbleup` takes no `dataset_slug`. A client guard that requires one refuses
  every real call, so `agent/mcp_client.py` scopes it by provenance instead: the `query_pk` or
  `bubbleup_result_id` has to be one this session received from a guarded query.
- 2026-09-04: a BubbleUp group selection needs both the column's JSON type for the value and a
  source query that broke down on that column. Either one missing fails with the same
  `failed to calculate group indices` message, so the client's error hint adds both facts.
- 2026-09-07: Agent Timeline breaks its Traces panel on a `/` in `gen_ai.conversation.id`. Two
  root spans that share one conversation id render as one conversation with a lane per agent,
  which is how the Canvas handoff sits next to the investigation that produced the report.
- 2026-09-07: `canvas_agent_poll_response` can answer `running` at once instead of holding the
  long poll for `wait_seconds`. A loop that trusts the documented wait repolls as fast as the
  client's pacing allows and spends the team's rate-limit window inside one handoff budget, so
  `agent/handoff.py` measures the elapsed time around each poll and sleeps the remainder.
- 2026-09-07: `list_boards` returns a Markdown table with no URL column, so a board found by name
  carries `board_url=None`; only a freshly created board gets a url back from `create_board`.
