# Eval report

Every number here is read from a `grade.json` under `evals/results/`, written by `evals/run.py` and graded by `evals/grader.py` (weights and penalties are explained in `evals/grader.md`). Nothing is typed by hand. A run that crashed, or that the loop ended with an error, is a row with a total of 0 and the error text; it was not put through the grader.

`total` is the grader's full score, penalties included, between -1 and 1. `outcome` is the weighted dims, span, incident, and onset components alone, at most 0.75, and sits next to `total` because an ablation that removes a rule can lose that rule's weight without changing an answer; whether it changed the answer is a question about `outcome`. `top right` counts runs whose top hypothesis scored at least 0.5 on the grader's dims component, which is the grader's own line for a wrong hypothesis. On a control a report with no hypothesis, or only low ones, scores 1 there and counts; on an incident scenario a report with no hypothesis scores 0 and does not; a crash never does. Ranges are the lowest and highest single run.

## Scores by scenario

| scenario | full total | full outcome | full top right |
| --- | --- | --- | --- |
| checkout-error-surge-adyen | 0.88 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| control-noisy | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| control-quiet | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| dependency-inventory-db-timeouts | 0.87 (0.60 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| deploy-regression-v260 | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| error-surge-exceptions | 0.95 (0.85 to 1.00) | 0.70 (0.60 to 0.75) | 3 of 3 |
| herring-customer-whale | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| herring-region-vs-version | 0.94 (0.82 to 1.00) | 0.69 (0.57 to 0.75) | 3 of 3 |
| payments-stripe-v251-uswest | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| trigger-checkout-latency | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| all scenarios | 0.96 (0.60 to 1.00) | 0.74 (0.57 to 0.75) | 30 of 30 |

## Process by config

Means over every run in the config, crashes included. `passes theirs, fails ours` counts runs that pass Honeycomb's process evaluator (a reimplementation of `tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, pass at 0.6) and score a `total` under 0.50 on ours. A crash has no process score and is not counted as passing theirs. The line is on `total`, so under an ablation config the removed rule's weight (0.15 for the negation, 0.10 for the not-checked list, both scored whatever the config) can count against the run here; read `outcome` in the scenario table for whether the answer changed. `tokens in` is uncached input, as the grade records it; the prompt cache reads that make up most of what the model read are in each `report.json` and are already priced into the cost. `coerced` counts two kinds of fix, across every attempt and every tool call in the config: submit_report fields decoded from a JSON-encoded string or unwrapped from a stray wrapper key around the whole report, and BubbleUp group values the MCP client retyped from the column schema rather than sending on as the model wrote them; blank when none were. `total cost USD` sums the same cost column instead of averaging it, and the line under the table sums that column again across every config.

| config | runs | crashed | mean calls | mean tokens in | mean tokens out | mean cost USD | total cost USD | mean wall s | coerced | passes theirs, fails ours (total < 0.50) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full | 30 | 0 | 25.2 | 540 | 9,580 | 0.51 | 15.30 | 182 | 4 | 0 of 30 |

Total Anthropic spend across every run in this results directory: $15.30.

## Runs

One row per investigation. `query` links to the first evidence query of the top hypothesis, or to the first baseline query when the report names no hypothesis. `theirs` is Honeycomb's process score with its pass mark applied.

| scenario | config | n | run id | model | total | outcome | receipts | top confidence | stopped by | calls | cost USD | wall s | theirs | query |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| checkout-error-surge-adyen | full | 1 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 37 | 0.79 | 232 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/FeHu8hrJyp9) |
| checkout-error-surge-adyen | full | 2 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 25 | 0.45 | 163 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/fmCd2gn8gAw) |
| checkout-error-surge-adyen | full | 3 | run-974f4e6bd0ed | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 25 | 0.50 | 189 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/ALo39Zezvyj) |
| control-noisy | full | 1 | run-ae8e20c5f076 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 25 | 0.51 | 170 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/bi2YQr22aKc) |
| control-noisy | full | 2 | run-ae8e20c5f076 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 18 | 0.27 | 111 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/vKgjrvCYYBy) |
| control-noisy | full | 3 | run-ae8e20c5f076 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 26 | 0.45 | 176 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/jjBUc9BkjP3) |
| control-quiet | full | 1 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 29 | 0.61 | 173 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/pupwsvZut9x) |
| control-quiet | full | 2 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 30 | 0.55 | 202 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/4j1fQGyDFWN) |
| control-quiet | full | 3 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 28 | 0.51 | 192 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/yCg5HhHaTff) |
| dependency-inventory-db-timeouts | full | 1 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 31 | 0.78 | 278 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/aEMMdrQBU4e) |
| dependency-inventory-db-timeouts | full | 2 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 25 | 0.47 | 180 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/hjm9xCmrwvL) |
| dependency-inventory-db-timeouts | full | 3 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 0.60 | 0.75 | 0.10 | high | report (validation failed) | 34 | 0.74 | 267 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/DU1kP29vKJK) |
| deploy-regression-v260 | full | 1 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 21 | 0.44 | 140 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/drp46NdDy2u) |
| deploy-regression-v260 | full | 2 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 18 | 0.30 | 132 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/fHaB4DFgnyt) |
| deploy-regression-v260 | full | 3 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 20 | 0.38 | 145 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/JqDymQRU4FL) |
| error-surge-exceptions | full | 1 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 26 | 0.53 | 167 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/G6PrxAxCTds) |
| error-surge-exceptions | full | 2 | run-a6353e4f46f8 | claude-sonnet-4-5 | 0.85 | 0.60 | 0.25 | high | report | 31 | 0.72 | 251 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/mvhK4Vp938y) |
| error-surge-exceptions | full | 3 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 23 | 0.43 | 158 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/HazfJSgFF1s) |
| herring-customer-whale | full | 1 | run-29580981edf1 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 27 | 0.58 | 185 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/6sMUUz9GBrm) |
| herring-customer-whale | full | 2 | run-29580981edf1 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 27 | 0.48 | 175 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/qbUoKinBaSJ) |
| herring-customer-whale | full | 3 | run-29580981edf1 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 28 | 0.50 | 196 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/q4kkaewDznQ) |
| herring-region-vs-version | full | 1 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 23 | 0.56 | 209 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/ojVB4QtMP1s) |
| herring-region-vs-version | full | 2 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 0.82 | 0.57 | 0.25 | high | report | 23 | 0.43 | 182 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/tJVyEESypi9) |
| herring-region-vs-version | full | 3 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 19 | 0.41 | 189 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/nqnuPhkcjch) |
| payments-stripe-v251-uswest | full | 1 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 27 | 0.55 | 172 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/ttcbX8EPZ59) |
| payments-stripe-v251-uswest | full | 2 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 21 | 0.40 | 159 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/c6QMYuUzavQ) |
| payments-stripe-v251-uswest | full | 3 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 25 | 0.49 | 179 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/DkN4spg58oE) |
| trigger-checkout-latency | full | 1 | run-54142a7c5b58 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 23 | 0.48 | 172 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/sybgB744tFo) |
| trigger-checkout-latency | full | 2 | run-6bc8932435e1 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 26 | 0.58 | 173 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/ou1Zz7uFyrL) |
| trigger-checkout-latency | full | 3 | run-bc1cca680f7e | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 16 | 0.41 | 144 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/ah6Duy9a9b) |
