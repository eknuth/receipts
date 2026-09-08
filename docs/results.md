# Results in detail

The reading behind the tables in the README's Results section. Every number here is read from
`evals/report.md` or from `evals/compare.py` over the same result directories.

The report has four columns. Three are Sonnet, run on 2026-09-08 on one build of main (after PRs
#39, #40, and #41: the schema leak fix, the optional keys, and the docstring history moved out)
and, for the nine scenarios other than the trigger, on one emit of each scenario, so their cells
pair. `trigger-checkout-latency` has to fire its trigger inside the window, so it was emitted once
per repeat for `full` and once per repeat shared by the two ablations, which one `evals.run`
invocation served together; on that scenario the ablations pair with each other and not with
`full`. `full` is the method, `no-negation` drops the negation rule, and `no-notchecked` drops
the not-checked list. The fourth column is nemotron, and it was not rerun. It is the column of
2026-09-07, committed in de65aa4 seven hours before the noise floor step landed in 7d6fb85, so it
met the prompt and the validator as they stood before that step and before the schema fix. The
Nemotron section says why it stayed.

## Sonnet

Thirty of the thirty `full` runs named the right cause or, on a control, said there was no
incident. All six control cells filed a report with no hypothesis at all. Mean total is 0.923 and
mean outcome 0.738 of a possible 0.75, over 24.2 MCP calls and 188 seconds a run, for $16.09
across the column. Eight cells sit under 1.0.

Six are `report (validation failed)` rows, each costing 0.25, and the answer was right in all six.
Three of them are the three `dependency-inventory-db-timeouts` cells, which also lost the receipts
component and so scored 0.60. Each claims `name = db.query`, which the scenario accepts as the
same population as `service.component = inventory-db`, and none of them negated it. Repeat 1 filed
as its negation a query on `name = db.query` broken down by `error`, repeat 2 filtered root spans
to `error != true`, and repeat 3 filtered root spans to `error = false`. Each measures the healthy
remainder and none excludes the dimension the claim rests on, which is what the rule asks for:
`name != db.query`, the same calculation over the same window. The validator said so on the second
rejection in all three runs, and each run filed anyway. Two more rows name an instrumentation
column as not checked, `service.name` in `control-noisy` repeat 2 and `parent_name` in
`herring-region-vs-version` repeat 1, when that list is for the dimensions and spans a person
would investigate. The sixth, `error-surge-exceptions` repeat 1, lists `inventory.reserve` and
`db.query` as partially checked columns. They are span names, values of `name`, and the subject of
a partial check has to be a column.

The other two are `herring-customer-whale` repeats 1 and 3, at 0.825 with no penalty. Both named
`payment.provider = adyen` and `payments.charge`, which is the ground truth, and both put
`name = HTTP POST /checkout` beside it as a second dimension. Jaccard over the pairs is 0.5, the
grader's line for a right top hypothesis, so the cell counts as right and the dims component pays
half. The root span name selects every request in the window and narrows nothing.

Against the column of 2026-09-06, parked at `evals/results/r26-pass/full/`, the paired totals are
0.910 to 0.923 and outcome 0.745 to 0.738, with validation failures down from nine to six. Ten
cells moved by 0.05 or more, six up and four down. Outcome moved in three: `deploy-regression-v260`
repeat 3 up 0.15, where the earlier run had named the root span as the slow one, and the two whale
cells down 0.175 each for the extra dimension. Every other cell scored 0.75 on outcome in both
columns. Between the two columns the schema lost its leaked example and the grader gained the
range rule below; the rest of the movement is the same stochastic agent run twice over the same
data.

## What the two rules are worth

`no-negation` and `no-notchecked` are the same prompt and validator with one rule removed, on the
same emitted data as `full` for nine scenarios and on their own shared emits for the trigger,
paired cell by cell by `evals/compare.py`. An ablation removes the rule's block from the prompt
and its check from the validator. It does not remove the field from the report schema, and it
does not touch the method's nine steps, which sit outside the optional blocks. That shapes what
each one measures.

Removing the negation rule leaves mean outcome where it was, 0.738 in both columns, and takes
total from 0.923 to 0.870. That 0.053 is penalties and receipts, and the receipts did not move
where the rule was. Every hypothesis in the column, 24 across the 24 incident cells, still
carries a negation query, and the grader gave full receipts to 27 of 30 cells in both columns,
mean 0.900 in both, though not the same 27: `full` lost the three
`dependency-inventory-db-timeouts` cells, `no-negation` lost two of those and
`trigger-checkout-latency` repeat 2. Step 8 of the method asks for the negation and the ablation
left step 8 in, so the playbook puts that query in the report and the rule's bullet does not. The
ten validation failures against six are all `partially_checked_contradicted`, a rule the ablation
did not touch; whether dropping the negation bullet changes how the model writes
`partially_checked` is a question thirty runs cannot answer.

What the rule did do is one check inside the validator, `agent/validate.py` near line 760: a
hypothesis that carries a negation has to name dims, or there is nothing for the negation to
exclude, and the check is skipped when the rule is off. In `full` all three
`dependency-inventory-db-timeouts` drafts were rejected on exactly that check and came back naming
`name = db.query`. In `no-negation` repeat 3, the same run id, the check never fired and the run
filed `dims={}` at high confidence with the span, the onset, and eight evidence queries behind it,
and the grader scored it wrong: total 0.00, the one cell where the config has fewer right top
hypotheses than `full`. The one check that forces a hypothesis to name its population went
silent, and `full` had caught the same draft three times.

Removing the not-checked rule moves outcome from 0.738 to 0.750 and total from 0.923 to 0.933,
with four validation failures against six, 22.7 calls a run against 24.2, 167 seconds against 188,
and $14.11 against $16.09 across the thirty runs. The list did not go away. Every `no-notchecked`
report still carries a `not_checked` list, 4 to 13 entries, and the grader still scores it
against the tool log. Seven of the thirty scored 0 for naming something the run had queried
(`control-quiet/2`, `dependency-inventory-db-timeouts/1` and `/3`, `deploy-regression-v260/3`,
`error-surge-exceptions/3`, `herring-customer-whale/2`, `trigger-checkout-latency/1`), against 0
of 30 in `full` and 1 of 30 in `no-negation`. So the rule does not put the list in the report; the
schema does. The rule is what makes the list true.

So on this data neither rule raises the outcome, and the same-rules rerun says how far a rule
would have to move it to be seen at all. From the 2026-09-06 column to this one, with nothing
removed, outcome moved 0.007 and total 0.013. The negation ablation moved outcome 0.000 and the
not-checked ablation 0.012, one below that drift and one near it, and thirty cells cannot tell
either apart from a rerun. The rules were never claimed to raise accuracy. They make the report
checkable, a negation that has to name what it excludes and a not-checked list that has to be
true, and the ablation measured that and nothing else. The grader reads the answer, and the
answer did not change.

One question the ablation as built leaves open is whether it should also drop the schema field.
As run, `no-notchecked` measures what validating the list buys, not what the list buys, and the
same holds for the negation field. Dropping a field is a schema change, and the section on the
tool schema below says what the last schema change did to a pass, which is why the field stayed
this time.

## What the noise floor step moved

This section compares two earlier columns, `evals/results/r18-pass/full/` from 2026-09-05 and the
column of 2026-09-06 that the README described until the rerun, now parked at
`evals/results/r26-pass/full/`. Its numbers are theirs, not the current report's.

The method change between them added a noise floor step to the fixed part of the prompt: before a
rate counts as a finding, name the population it is measured against and put the expected count
next to the observed one. It also made the validator reject a baseline query whose time range
lies entirely outside the run window.

The mean total went from 0.825 to 0.910. Most of that is not the noise floor step. The before
column ran on 2026-09-05, ahead of three commits on `main` (2ba4100, 4a2b286, 883b23e) that went
at the same class of failure, and `total` carries the validator's 0.25 penalty. All three changed
the validator, and two of them also changed the `not_checked` and `partially_checked` rules in the
prompt, which leaves the investigation steps alone.

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

The step also cost something. Two cells in the 2026-09-06 column lost the receipts component and
scored 0.60: `dependency-inventory-db-timeouts/3`, going 0.75 to 0.60, where the claim rested on
`name = db.query` and the negation filtered `name = payments.charge`, and
`error-surge-exceptions/2`, going 1.00 to 0.60, where the claim rested on
`payment.provider = adyen` and the negation filtered `payment.provider in [stripe, paypal]`. Both
measured the right complement and neither wrote it as a negation, and step 5 asks for the rows
outside the candidate over the whole window, which is the query both runs then filed as the
negation the receipts rule rejected. Set against the outcome gain that is -0.55 of `total`, and
EDW-1369 is the fix. Of the rerun's three `dependency-inventory-db-timeouts` cells only repeat 3
has this shape, `error = false` against a claim of `error = true`; repeat 1 broke down by `error`
under `name = db.query`, and repeat 2 filtered `error != true` on a claim naming only `name`.

So the step is worth 0.60 of outcome across thirty runs, won in one cell and partly given back in
another, against a likely -0.55 on total. Both columns are separate runs of a stochastic agent
over the same data, so one cell moving does not prove the change caused it. What can be said is
that the control failure the change was aimed at stopped happening, and that nothing else in the
outcome components moved except one span name.

## Nemotron

The column was not rerun with the Sonnet ones. NVIDIA's NIM endpoint returned HTTP 500 through
three attempts on 2026-09-08. The last attempt's log, local and not in the repo, holds 69 of them;
every cell ran into the 480 second wall cap on retries, and one cell crashed before its first
call. So the
nemotron rows in `evals/report.md` are the column of 2026-09-07, which met the prompt and the
validator from before the noise floor step and ran under the leaked schema. For the nine scenarios
other than the trigger it is on the same emitted data as the Sonnet columns, since the rerun
reused the run ids in `runs.json`; its trigger cells are emits of their own. The reading below is
of that column.

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
the schema, so nothing caught it until 2026-09-07. The Sonnet `full` column of 2026-09-06, now
parked at `r26-pass/full`, ran under that schema, and so did `r17-pass`, `r18-pass`, and the
nemotron column still in `evals/report.md`.

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
against the prompt, the schema, the tool description, and the validator's messages, and the three
Sonnet columns in `evals/report.md` are the rerun on the fixed schema, from 2026-09-08. The
nemotron column is not, for the reason in its section.

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

The regrade moved three cells and nothing else: `full/trigger-checkout-latency/1` and `/3` from
0.15 to 1.00, and `matrix2-ba6518b/no-negation/trigger-checkout-latency/1`, in the 2026-09-05
matrix and not in the report's `no-negation` column, from 0.00 to 1.00.
Before it the `full` column read 0.867 and the paired delta for `no-negation` read +0.003; after
it the column reads 0.923 and the delta -0.053, and `passes theirs, fails ours` for `full` went
from 2 of 30 to 0 of 30. Two spellings of one population were carrying the reading of a whole
ablation.

## Reproducing

```
uv run python -m evals.run --scenarios all --configs full --repeats 3
uv run python -m evals.run --scenarios all --configs no-negation,no-notchecked --repeats 3
uv run python -m evals.run --scenarios all --configs full --repeats 3 --provider nvidia
uv run python -m evals.report
uv run python -m evals.compare full no-negation
uv run python -m evals.compare full no-notchecked
```

The first two commands write `evals/results/full/`, `no-negation/`, and `no-notchecked/`, and share
the emitted data, since each reuses the latest run id per scenario in `evals/results/runs.json`.
The nvidia run writes to `full/` too, so run it with `--results-dir` pointing somewhere else and
copy its `full/` directory in beside the first as `evals/results/full-nvidia/`. The report keys
columns by provider when more than one is present.

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
