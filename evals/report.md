# Eval report

Every number here is read from a `grade.json` under `evals/results/`, written by `evals/run.py` and graded by `evals/grader.py` (weights and penalties are explained in `evals/grader.md`). Nothing is typed by hand. A run that crashed, or that the loop ended with an error, is a row with a total of 0 and the error text; it was not put through the grader.

`total` is the grader's full score, penalties included, between -1 and 1. `outcome` is the weighted dims, span, incident, and onset components alone, at most 0.75, and sits next to `total` because an ablation that removes a rule loses that rule's weight by construction; whether it changed the answer is a question about `outcome`. `top right` counts runs whose top hypothesis scored at least 0.5 on the grader's dims component, which is the grader's own line for a wrong hypothesis. On a control a report with no hypothesis, or only low ones, scores 1 there and counts; on an incident scenario a report with no hypothesis scores 0 and does not; a crash never does. Ranges are the lowest and highest single run.

## Scores by scenario

| scenario | full total | full outcome | full top right |
| --- | --- | --- | --- |
| checkout-error-surge-adyen | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 1 of 1 |
| control-noisy | -0.25 (-0.25 to -0.25) | 0.00 (0.00 to 0.00) | 0 of 1 |
| control-quiet | 0.44 (-0.60 to 1.00) | 0.50 (0.00 to 0.75) | 4 of 6 |
| dependency-inventory-db-timeouts | -0.20 (-0.20 to -0.20) | 0.30 (0.30 to 0.30) | 0 of 1 |
| deploy-regression-v260 | 0.25 (0.25 to 0.25) | 0.00 (0.00 to 0.00) | 0 of 1 |
| error-surge-exceptions | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 1 of 1 |
| herring-customer-whale | 0.82 (0.82 to 0.82) | 0.57 (0.57 to 0.57) | 1 of 1 |
| herring-region-vs-version | 0.90 (0.90 to 0.90) | 0.65 (0.65 to 0.65) | 1 of 1 |
| payments-stripe-v251-uswest | -0.04 (-0.25 to 0.24) | 0.16 (0.00 to 0.49) | 0 of 3 |
| trigger-checkout-latency | -0.10 (-0.10 to -0.10) | 0.15 (0.15 to 0.15) | 0 of 1 |
| all scenarios | 0.35 (-0.60 to 1.00) | 0.39 (0.00 to 0.75) | 8 of 17 |

## Process by config

Means over every run in the config, crashes included. `passes theirs, fails ours` counts runs that pass Honeycomb's process evaluator (a reimplementation of `tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, pass at 0.6) and score a `total` under 0.50 on ours. A crash has no process score and is not counted as passing theirs. The line is on `total`, so under an ablation config the removed rule's weight (0.15 for the negation, which the grader requires whatever the config) counts against the run here; read `outcome` in the scenario table for whether the answer changed. `tokens in` is uncached input, as the grade records it; the prompt cache reads that make up most of what the model read are in each `report.json` and are already priced into the cost.

| config | runs | crashed | mean calls | mean tokens in | mean tokens out | mean cost USD | mean wall s | passes theirs, fails ours (total < 0.50) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full | 17 | 0 | 36.8 | 630 | 12,403 | 0.65 | 228 | 9 of 17 |

## Runs

One row per investigation. `query` links to the first evidence query of the top hypothesis, or to the first baseline query when the report names no hypothesis. `theirs` is Honeycomb's process score with its pass mark applied.

| scenario | config | n | run id | model | total | outcome | receipts | top confidence | stopped by | calls | cost USD | wall s | theirs | query |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| checkout-error-surge-adyen | full | 1 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 37 | 0.68 | 235 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/3Sm7JmdikB9) |
| control-noisy | full | 1 | run-ae8e20c5f076 | claude-sonnet-4-5 | -0.25 | 0.00 | 0.25 | high | report | 32 | 0.60 | 208 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/zc1J5wCwzJd) |
| control-quiet | full | 1 | run-ebc9c1e4be3d | claude-sonnet-4-5 | -0.25 | 0.00 | 0.25 | high | report | 28 | 0.47 | 179 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/nuLWfLwdr6i) |
| control-quiet | full | 2 | run-ebc9c1e4be3d | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 38 | 0.58 | 249 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/gMTLyFqM3cE) |
| control-quiet | full | 3 | run-ebc9c1e4be3d | claude-sonnet-4-5 | 0.50 | 0.75 | 0.00 |  | report (validation failed) | 40 | 0.68 | 271 | 1.00 pass |  |
| control-quiet | full | 4 | run-ebc9c1e4be3d | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 36 | 0.67 | 211 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/qtvUKayzcUj) |
| control-quiet | full | 5 | run-ebc9c1e4be3d | claude-sonnet-4-5 | -0.60 | 0.00 | 0.15 | high | report (validation failed) | 35 | 0.66 | 229 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/cq7AbWj3x6y) |
| control-quiet | full | 6 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 38 | 0.60 | 228 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/e1uwRGrEgUR) |
| dependency-inventory-db-timeouts | full | 1 | run-e10b6c1bf2dc | claude-sonnet-4-5 | -0.20 | 0.30 | 0.00 | medium | report (validation failed) | 40 | 0.73 | 252 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/bWUeN27N6Lp) |
| deploy-regression-v260 | full | 1 | run-2ef344b96d57 | claude-sonnet-4-5 | 0.25 | 0.00 | 0.25 |  | report | 37 | 0.66 | 201 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/iRoyqV6VDuM) |
| error-surge-exceptions | full | 1 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 34 | 0.62 | 212 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/mw23Pa6PKeu) |
| herring-customer-whale | full | 1 | run-29580981edf1 | claude-sonnet-4-5 | 0.82 | 0.57 | 0.25 | high | report | 39 | 0.65 | 221 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/ADWPrjtEnG8) |
| herring-region-vs-version | full | 1 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 0.90 | 0.65 | 0.25 | high | report | 40 | 0.74 | 248 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/37sdEEGHWeF) |
| payments-stripe-v251-uswest | full | 1 | run-4155490e2a44 | claude-sonnet-4-5 | -0.10 | 0.00 | 0.15 |  | report (validation failed) | 35 | 0.65 | 222 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/eRVtrJcodnJ) |
| payments-stripe-v251-uswest | full | 2 | run-4155490e2a44 | claude-sonnet-4-5 | 0.24 | 0.49 | 0.25 | high | report | 40 | 0.61 | 232 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/5UG9bVevB4J) |
| payments-stripe-v251-uswest | full | 3 | run-f0d47eec0615 | claude-sonnet-4-5 | -0.25 | 0.00 | 0.00 |  | report (validation failed) | 40 | 0.76 | 251 | 1.00 pass |  |
| trigger-checkout-latency | full | 1 | run-79b76551ad21 | claude-sonnet-4-5 | -0.10 | 0.15 | 0.25 | high | report | 37 | 0.67 | 222 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/nTKevJaww2P) |
