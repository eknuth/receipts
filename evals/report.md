# Eval report

Every number here is read from a `grade.json` under `evals/results/`, written by `evals/run.py` and graded by `evals/grader.py` (weights and penalties are explained in `evals/grader.md`). Nothing is typed by hand. A run that crashed, or that the loop ended with an error, is a row with a total of 0 and the error text; it was not put through the grader.

`total` is the grader's full score, penalties included, between -1 and 1. `outcome` is the weighted dims, span, incident, and onset components alone, at most 0.75, and sits next to `total` because an ablation that removes a rule loses that rule's weight by construction; whether it changed the answer is a question about `outcome`. `top right` counts runs whose top hypothesis scored at least 0.5 on the grader's dims component, which is the grader's own line for a wrong hypothesis. On a control a report with no hypothesis, or only low ones, scores 1 there and counts; on an incident scenario a report with no hypothesis scores 0 and does not; a crash never does. Ranges are the lowest and highest single run.

## Scores by scenario

| scenario | full total | full outcome | full top right |
| --- | --- | --- | --- |
| checkout-error-surge-adyen | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 2 of 2 |
| control-noisy | -0.25 (-0.25 to -0.25) | 0.00 (0.00 to 0.00) | 0 of 2 |
| control-quiet | 0.52 (-0.60 to 1.00) | 0.54 (0.00 to 0.75) | 5 of 7 |
| dependency-inventory-db-timeouts | 0.02 (-0.13 to 0.17) | 0.50 (0.47 to 0.52) | 1 of 2 |
| deploy-regression-v260 | 0.49 (0.25 to 0.72) | 0.24 (0.00 to 0.47) | 1 of 2 |
| error-surge-exceptions | 0.82 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 2 of 2 |
| herring-customer-whale | 0.69 (0.55 to 0.82) | 0.61 (0.57 to 0.65) | 2 of 2 |
| herring-region-vs-version | 0.95 (0.90 to 1.00) | 0.70 (0.65 to 0.75) | 2 of 2 |
| payments-stripe-v251-uswest | 0.18 (-0.25 to 0.82) | 0.27 (0.00 to 0.57) | 1 of 4 |
| trigger-checkout-latency | -0.05 (-0.10 to 0.00) | 0.28 (0.15 to 0.40) | 0 of 2 |
| all scenarios | 0.43 (-0.60 to 1.00) | 0.46 (0.00 to 0.75) | 16 of 27 |

## Process by config

Means over every run in the config, crashes included. `passes theirs, fails ours` counts runs that pass Honeycomb's process evaluator (a reimplementation of `tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, pass at 0.6) and score a `total` under 0.50 on ours. A crash has no process score and is not counted as passing theirs. The line is on `total`, so under an ablation config the removed rule's weight (0.15 for the negation, which the grader requires whatever the config) counts against the run here; read `outcome` in the scenario table for whether the answer changed. `tokens in` is uncached input, as the grade records it; the prompt cache reads that make up most of what the model read are in each `report.json` and are already priced into the cost. `coerced` counts submit_report fields that arrived as a JSON-encoded string and were decoded rather than rejected, across every submit attempt in the config; blank when none were.

| config | runs | crashed | mean calls | mean tokens in | mean tokens out | mean cost USD | mean wall s | coerced | passes theirs, fails ours (total < 0.50) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full | 27 | 0 | 37.3 | 631 | 12,613 | 0.66 | 232 |  | 12 of 27 |

## Runs

One row per investigation. `query` links to the first evidence query of the top hypothesis, or to the first baseline query when the report names no hypothesis. `theirs` is Honeycomb's process score with its pass mark applied.

| scenario | config | n | run id | model | total | outcome | receipts | top confidence | stopped by | calls | cost USD | wall s | theirs | query |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| checkout-error-surge-adyen | full | 1 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 37 | 0.68 | 235 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/3Sm7JmdikB9) |
| checkout-error-surge-adyen | full | 2 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 39 | 0.61 | 212 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/JmM58U9fxY9) |
| control-noisy | full | 1 | run-ae8e20c5f076 | claude-sonnet-4-5 | -0.25 | 0.00 | 0.25 | high | report | 32 | 0.60 | 208 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/zc1J5wCwzJd) |
| control-noisy | full | 2 | run-ae8e20c5f076 | claude-sonnet-4-5 | -0.25 | 0.00 | 0.25 | high | report | 40 | 0.73 | 260 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/FcaY8b9BL5N) |
| control-quiet | full | 1 | run-ebc9c1e4be3d | claude-sonnet-4-5 | -0.25 | 0.00 | 0.25 | high | report | 28 | 0.47 | 179 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/nuLWfLwdr6i) |
| control-quiet | full | 2 | run-ebc9c1e4be3d | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 38 | 0.58 | 249 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/gMTLyFqM3cE) |
| control-quiet | full | 3 | run-ebc9c1e4be3d | claude-sonnet-4-5 | 0.50 | 0.75 | 0.00 |  | report (validation failed) | 40 | 0.68 | 271 | 1.00 pass |  |
| control-quiet | full | 4 | run-ebc9c1e4be3d | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 36 | 0.67 | 211 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/qtvUKayzcUj) |
| control-quiet | full | 5 | run-ebc9c1e4be3d | claude-sonnet-4-5 | -0.60 | 0.00 | 0.15 | high | report (validation failed) | 35 | 0.66 | 229 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/cq7AbWj3x6y) |
| control-quiet | full | 6 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 38 | 0.60 | 228 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/e1uwRGrEgUR) |
| control-quiet | full | 7 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 36 | 0.60 | 198 | 0.95 pass |  |
| dependency-inventory-db-timeouts | full | 1 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 0.17 | 0.47 | 0.00 | medium | report (validation failed) | 40 | 0.73 | 252 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/bWUeN27N6Lp) |
| dependency-inventory-db-timeouts | full | 2 | run-e10b6c1bf2dc | claude-sonnet-4-5 | -0.13 | 0.52 | 0.10 | high | report (validation failed) | 38 | 0.69 | 262 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/q8YsYg8ufXo) |
| deploy-regression-v260 | full | 1 | run-2ef344b96d57 | claude-sonnet-4-5 | 0.25 | 0.00 | 0.25 |  | report | 37 | 0.66 | 201 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/iRoyqV6VDuM) |
| deploy-regression-v260 | full | 2 | run-2ef344b96d57 | claude-sonnet-4-5 | 0.72 | 0.47 | 0.25 | high | report | 40 | 0.79 | 273 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/ivwmpkdjiKA) |
| error-surge-exceptions | full | 1 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 34 | 0.62 | 212 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/mw23Pa6PKeu) |
| error-surge-exceptions | full | 2 | run-a6353e4f46f8 | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 36 | 0.66 | 225 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/8qcQ7jyhfTw) |
| herring-customer-whale | full | 1 | run-29580981edf1 | claude-sonnet-4-5 | 0.82 | 0.57 | 0.25 | high | report | 39 | 0.65 | 221 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/ADWPrjtEnG8) |
| herring-customer-whale | full | 2 | run-29580981edf1 | claude-sonnet-4-5 | 0.55 | 0.65 | 0.15 | high | report (validation failed) | 32 | 0.59 | 197 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/adGg8zouda5) |
| herring-region-vs-version | full | 1 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 0.90 | 0.65 | 0.25 | high | report | 40 | 0.74 | 248 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/37sdEEGHWeF) |
| herring-region-vs-version | full | 2 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 40 | 0.70 | 243 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/jnrp2GFGAVX) |
| payments-stripe-v251-uswest | full | 1 | run-4155490e2a44 | claude-sonnet-4-5 | -0.10 | 0.00 | 0.15 |  | report (validation failed) | 35 | 0.65 | 222 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/eRVtrJcodnJ) |
| payments-stripe-v251-uswest | full | 2 | run-4155490e2a44 | claude-sonnet-4-5 | 0.24 | 0.49 | 0.25 | high | report | 40 | 0.61 | 232 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/5UG9bVevB4J) |
| payments-stripe-v251-uswest | full | 3 | run-f0d47eec0615 | claude-sonnet-4-5 | -0.25 | 0.00 | 0.00 |  | report (validation failed) | 40 | 0.76 | 251 | 1.00 pass |  |
| payments-stripe-v251-uswest | full | 4 | run-f0d47eec0615 | claude-sonnet-4-5 | 0.82 | 0.57 | 0.25 | high | report | 39 | 0.75 | 240 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/CA8hyxMGknh) |
| trigger-checkout-latency | full | 1 | run-79b76551ad21 | claude-sonnet-4-5 | -0.10 | 0.15 | 0.25 | high | report | 37 | 0.67 | 222 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/nTKevJaww2P) |
| trigger-checkout-latency | full | 2 | run-144995228783 | claude-sonnet-4-5 | 0.00 | 0.40 | 0.10 | medium | report (validation failed) | 40 | 0.76 | 274 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/b84MCiLnH1k) |
