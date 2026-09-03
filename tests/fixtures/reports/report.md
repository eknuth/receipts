# Eval report

Every number here is read from a `grade.json` under `evals/results/`, written by `evals/run.py` and graded by `evals/grader.py` (weights and penalties are explained in `evals/grader.md`). Nothing is typed by hand. A run that crashed, or that the loop ended with an error, is a row with a total of 0 and the error text; it was not put through the grader.

`total` is the grader's full score, penalties included, between -1 and 1. `outcome` is the weighted dims, span, incident, and onset components alone, at most 0.75, and sits next to `total` because an ablation that removes a rule loses that rule's weight by construction; whether it changed the answer is a question about `outcome`. `top right` counts runs whose top hypothesis scored at least 0.5 on the grader's dims component, which is the grader's own line for a wrong hypothesis. On a control a report with no hypothesis, or only low ones, scores 1 there and counts; on an incident scenario a report with no hypothesis scores 0 and does not; a crash never does. Ranges are the lowest and highest single run.

## Scores by scenario

| scenario | full total | full outcome | full top right |
| --- | --- | --- | --- |
| control-quiet | 0.69 (-0.25 to 1.00) | 0.56 (0.00 to 0.75) | 3 of 4 |
| payments-stripe-v251-uswest | 0.51 (0.24 to 0.90) | 0.59 (0.49 to 0.65) | 3 of 4 |
| all scenarios | 0.60 (-0.25 to 1.00) | 0.58 (0.00 to 0.75) | 6 of 8 |

## Process by config

Means over every run in the config, crashes included. `passes theirs, fails ours` counts runs that pass Honeycomb's process evaluator (a reimplementation of `tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, pass at 0.6) and score a `total` under 0.50 on ours. A crash has no process score and is not counted as passing theirs. The line is on `total`, so under an ablation config the removed rule's weight (0.15 for the negation, which the grader requires whatever the config) counts against the run here; read `outcome` in the scenario table for whether the answer changed. `tokens in` is uncached input, as the grade records it; the prompt cache reads that make up most of what the model read are in each `report.json` and are already priced into the cost.

| config | runs | crashed | mean calls | mean tokens in | mean tokens out | mean cost USD | mean wall s | passes theirs, fails ours (total < 0.50) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full | 8 | 0 | 34.6 | 610 | 11,254 | 0.57 | 211 | 3 of 8 |

## Runs

One row per investigation. `query` links to the first evidence query of the top hypothesis, or to the first baseline query when the report names no hypothesis. `theirs` is Honeycomb's process score with its pass mark applied.

| scenario | config | n | run id | model | total | outcome | receipts | top confidence | stopped by | calls | cost USD | wall s | theirs | query |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| control-quiet | full | 1 | run-ebc9c1e4be3d | claude-sonnet-4-5 | -0.25 | 0.00 | 0.25 | high | report | 31 | 0.46 | 170 | 1.00 pass | [query](https://ui.honeycomb.io/team/environments/receipts-demo/datasets/receipts-shop/result/7BJPbjH1mji) |
| control-quiet | full | 2 | run-ebc9c1e4be3d | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 34 | 0.57 | 183 | 0.95 pass | [query](https://ui.honeycomb.io/team/environments/receipts-demo/datasets/receipts-shop/result/7Z5vEmmvoyf) |
| control-quiet | full | 3 | run-ebc9c1e4be3d | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 32 | 0.53 | 224 | 1.00 pass | [query](https://ui.honeycomb.io/team/environments/receipts-demo/datasets/receipts-shop/result/tgB5d8dvy1C) |
| control-quiet | full | 4 | run-ebc9c1e4be3d | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 28 | 0.38 | 150 | 0.90 pass | [query](https://ui.honeycomb.io/team/environments/receipts-demo/datasets/receipts-shop/result/tetSH95EPMN) |
| payments-stripe-v251-uswest | full | 1 | run-4155490e2a44 | claude-sonnet-4-5 | 0.90 | 0.65 | 0.25 | high | report | 37 | 0.63 | 237 | 1.00 pass | [query](https://ui.honeycomb.io/team/environments/receipts-demo/datasets/receipts-shop/result/3QtwuURhM6g) |
| payments-stripe-v251-uswest | full | 2 | run-4155490e2a44 | claude-sonnet-4-5 | 0.63 | 0.63 | 0.25 | high | report (validation failed) | 38 | 0.71 | 239 | 1.00 pass | [query](https://ui.honeycomb.io/team/environments/receipts-demo/datasets/receipts-shop/result/9tbBWa1yqWP) |
| payments-stripe-v251-uswest | full | 3 | run-4155490e2a44 | claude-sonnet-4-5 | 0.24 | 0.49 | 0.25 | high | report | 37 | 0.64 | 234 | 1.00 pass | [query](https://ui.honeycomb.io/team/environments/receipts-demo/datasets/receipts-shop/result/Ej6NNq2NrAm) |
| payments-stripe-v251-uswest | full | 4 | run-4155490e2a44 | claude-sonnet-4-5 | 0.25 | 0.60 | 0.00 | low | report (validation failed) | 40 | 0.63 | 254 | 1.00 pass | [query](https://ui.honeycomb.io/team/environments/receipts-demo/datasets/receipts-shop/result/96GW37QXQsy) |
