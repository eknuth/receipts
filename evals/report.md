# Eval report

Every number here is read from a `grade.json` under `evals/results/`, written by `evals/run.py` and graded by `evals/grader.py` (weights and penalties are explained in `evals/grader.md`). Nothing is typed by hand. A run that crashed, or that the loop ended with an error, is a row with a total of 0 and the error text; it was not put through the grader.

`total` is the grader's full score, penalties included, between -1 and 1. `outcome` is the weighted dims, span, incident, and onset components alone, at most 0.75, and sits next to `total` because an ablation that removes a rule loses that rule's weight by construction; whether it changed the answer is a question about `outcome`. `top right` counts runs whose top hypothesis scored at least 0.5 on the grader's dims component, which is the grader's own line for a wrong hypothesis. On a control a report with no hypothesis, or only low ones, scores 1 there and counts; on an incident scenario a report with no hypothesis scores 0 and does not; a crash never does. Ranges are the lowest and highest single run.

## Scores by scenario

| scenario | full total | full outcome | full top right |
| --- | --- | --- | --- |
| checkout-error-surge-adyen | 0.88 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| control-noisy | 0.88 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| control-quiet | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| dependency-inventory-db-timeouts | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| deploy-regression-v260 | 0.77 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| error-surge-exceptions | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| herring-customer-whale | 0.77 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| herring-region-vs-version | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| payments-stripe-v251-uswest | 0.97 (0.90 to 1.00) | 0.72 (0.65 to 0.75) | 3 of 3 |
| trigger-checkout-latency | 0.94 (0.82 to 1.00) | 0.69 (0.57 to 0.75) | 3 of 3 |
| all scenarios | 0.92 (0.65 to 1.00) | 0.74 (0.57 to 0.75) | 30 of 30 |

## Process by config

Means over every run in the config, crashes included. `passes theirs, fails ours` counts runs that pass Honeycomb's process evaluator (a reimplementation of `tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, pass at 0.6) and score a `total` under 0.50 on ours. A crash has no process score and is not counted as passing theirs. The line is on `total`, so under an ablation config the removed rule's weight (0.15 for the negation, which the grader requires whatever the config) counts against the run here; read `outcome` in the scenario table for whether the answer changed. `tokens in` is uncached input, as the grade records it; the prompt cache reads that make up most of what the model read are in each `report.json` and are already priced into the cost. `coerced` counts two kinds of fix, across every attempt and every tool call in the config: submit_report fields decoded from a JSON-encoded string or unwrapped from a stray wrapper key around the whole report, and BubbleUp group values the MCP client retyped from the column schema rather than sending on as the model wrote them; blank when none were.

| config | runs | crashed | mean calls | mean tokens in | mean tokens out | mean cost USD | mean wall s | coerced | passes theirs, fails ours (total < 0.50) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full | 30 | 0 | 25.6 | 541 | 9,515 | 0.47 | 181 | 4 | 0 of 30 |

## Runs

One row per investigation. `query` links to the first evidence query of the top hypothesis, or to the first baseline query when the report names no hypothesis. `theirs` is Honeycomb's process score with its pass mark applied.

| scenario | config | n | run id | model | total | outcome | receipts | top confidence | stopped by | calls | cost USD | wall s | theirs | query |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| checkout-error-surge-adyen | full | 1 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 26 | 0.48 | 173 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/o99d2aq78aX) |
| checkout-error-surge-adyen | full | 2 | run-974f4e6bd0ed | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 27 | 0.49 | 200 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/7D5RQDo7411) |
| checkout-error-surge-adyen | full | 3 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 24 | 0.40 | 164 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/pQzjWLxhfFh) |
| control-noisy | full | 1 | run-ae8e20c5f076 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 22 | 0.47 | 159 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/327KqkfJRjD) |
| control-noisy | full | 2 | run-ae8e20c5f076 | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 |  | report (validation failed) | 25 | 0.43 | 182 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/y6djjR5Bnhz) |
| control-noisy | full | 3 | run-ae8e20c5f076 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 22 | 0.40 | 162 | 0.90 pass |  |
| control-quiet | full | 1 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 25 | 0.44 | 148 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/zpxt67JrR1i) |
| control-quiet | full | 2 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 27 | 0.44 | 170 | 0.70 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/qeYZwXdkXEX) |
| control-quiet | full | 3 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 28 | 0.51 | 194 | 0.90 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/jq7gJ5iGkq3) |
| dependency-inventory-db-timeouts | full | 1 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 32 | 0.69 | 229 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/ABM3rfCwpoW) |
| dependency-inventory-db-timeouts | full | 2 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 27 | 0.56 | 224 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/7aJjVxq94K4) |
| dependency-inventory-db-timeouts | full | 3 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 22 | 0.36 | 159 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/uWVQAtjfYRk) |
| deploy-regression-v260 | full | 1 | run-2ef344b96d57 | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 21 | 0.48 | 173 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/g5bF48dD5XP) |
| deploy-regression-v260 | full | 2 | run-2ef344b96d57 | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 26 | 0.51 | 196 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/j9x5SHTouyv) |
| deploy-regression-v260 | full | 3 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 20 | 0.37 | 152 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/o2EDhTGAQJH) |
| error-surge-exceptions | full | 1 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 23 | 0.46 | 165 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/pRVh8bJNekt) |
| error-surge-exceptions | full | 2 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 33 | 0.57 | 227 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/bRcyrtF4EQN) |
| error-surge-exceptions | full | 3 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 31 | 0.54 | 218 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/eof3CzbqQEo) |
| herring-customer-whale | full | 1 | run-29580981edf1 | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 27 | 0.55 | 198 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/EYttABpwioe) |
| herring-customer-whale | full | 2 | run-29580981edf1 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 31 | 0.46 | 211 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/kmBXAw6bXQz) |
| herring-customer-whale | full | 3 | run-29580981edf1 | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 33 | 0.59 | 244 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/k9KsEp49Hqa) |
| herring-region-vs-version | full | 1 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 23 | 0.48 | 166 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/7QbpafpReKB) |
| herring-region-vs-version | full | 2 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 22 | 0.35 | 149 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/3GKP3mFCDhZ) |
| herring-region-vs-version | full | 3 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 21 | 0.34 | 146 | 0.88 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/ANJQL2ntQtB) |
| payments-stripe-v251-uswest | full | 1 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 26 | 0.52 | 197 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/g8TaydFE7YD) |
| payments-stripe-v251-uswest | full | 2 | run-f0d47eec0615 | claude-sonnet-4-5 | 0.90 | 0.65 | 0.25 | high | report | 24 | 0.35 | 148 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/s3gn1YKqzoz) |
| payments-stripe-v251-uswest | full | 3 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 25 | 0.40 | 157 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/fdzeqmkPgra) |
| trigger-checkout-latency | full | 1 | run-8a017b7bda00 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 28 | 0.59 | 187 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/34fpq9RizEV) |
| trigger-checkout-latency | full | 2 | run-1aa267ab88db | claude-sonnet-4-5 | 0.82 | 0.57 | 0.25 | high | report | 20 | 0.44 | 168 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/bU3BvzBu6vX) |
| trigger-checkout-latency | full | 3 | run-cf8bc7d0b2b8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 26 | 0.53 | 172 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/9NdRM5ZRfLn) |
