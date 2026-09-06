# Eval report

Every number here is read from a `grade.json` under `evals/results/`, written by `evals/run.py` and graded by `evals/grader.py` (weights and penalties are explained in `evals/grader.md`). Nothing is typed by hand. A run that crashed, or that the loop ended with an error, is a row with a total of 0 and the error text; it was not put through the grader.

`total` is the grader's full score, penalties included, between -1 and 1. `outcome` is the weighted dims, span, incident, and onset components alone, at most 0.75, and sits next to `total` because an ablation that removes a rule can lose that rule's weight without changing an answer; whether it changed the answer is a question about `outcome`. `top right` counts runs whose top hypothesis scored at least 0.5 on the grader's dims component, which is the grader's own line for a wrong hypothesis. On a control a report with no hypothesis, or only low ones, scores 1 there and counts; on an incident scenario a report with no hypothesis scores 0 and does not; a crash never does. Ranges are the lowest and highest single run.

## Scores by scenario

| scenario | full total | full outcome | full top right |
| --- | --- | --- | --- |
| checkout-error-surge-adyen | 0.80 (0.75 to 0.90) | 0.75 (0.75 to 0.75) | 3 of 3 |
| control-noisy | 0.30 (-0.50 to 0.75) | 0.50 (0.00 to 0.75) | 2 of 3 |
| control-quiet | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| dependency-inventory-db-timeouts | 0.75 (0.50 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| deploy-regression-v260 | 0.97 (0.90 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| error-surge-exceptions | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| herring-customer-whale | 0.80 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| herring-region-vs-version | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| payments-stripe-v251-uswest | 0.92 (0.75 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| trigger-checkout-latency | 0.88 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| all scenarios | 0.82 (-0.50 to 1.00) | 0.72 (0.00 to 0.75) | 29 of 30 |

## Process by config

Means over every run in the config, crashes included. `passes theirs, fails ours` counts runs that pass Honeycomb's process evaluator (a reimplementation of `tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, pass at 0.6) and score a `total` under 0.50 on ours. A crash has no process score and is not counted as passing theirs. The line is on `total`, so under an ablation config the removed rule's weight (0.15 for the negation, 0.10 for the not-checked list, both scored whatever the config) can count against the run here; read `outcome` in the scenario table for whether the answer changed. `tokens in` is uncached input, as the grade records it; the prompt cache reads that make up most of what the model read are in each `report.json` and are already priced into the cost. `coerced` counts two kinds of fix, across every attempt and every tool call in the config: submit_report fields decoded from a JSON-encoded string or unwrapped from a stray wrapper key around the whole report, and BubbleUp group values the MCP client retyped from the column schema rather than sending on as the model wrote them; blank when none were. `total cost USD` sums the same cost column instead of averaging it, and the line under the table sums that column again across every config.

| config | runs | crashed | mean calls | mean tokens in | mean tokens out | mean cost USD | total cost USD | mean wall s | coerced | passes theirs, fails ours (total < 0.50) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full | 30 | 0 | 25.3 | 543 | 10,308 | 0.54 | 16.19 | 187 | 13 | 1 of 30 |

Total Anthropic spend across every run in this results directory: $16.19.

## Runs

One row per investigation. `query` links to the first evidence query of the top hypothesis, or to the first baseline query when the report names no hypothesis. `theirs` is Honeycomb's process score with its pass mark applied.

| scenario | config | n | run id | model | total | outcome | receipts | top confidence | stopped by | calls | cost USD | wall s | theirs | query |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| checkout-error-surge-adyen | full | 1 | run-974f4e6bd0ed | claude-sonnet-4-5 | 0.90 | 0.75 | 0.15 | high | report | 24 | 0.56 | 173 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/J8cHjvvzPW) |
| checkout-error-surge-adyen | full | 2 | run-974f4e6bd0ed | claude-sonnet-4-5 | 0.75 | 0.75 | 0.25 | high | report (validation failed) | 23 | 0.46 | 172 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/sKDnHYbnWia) |
| checkout-error-surge-adyen | full | 3 | run-974f4e6bd0ed | claude-sonnet-4-5 | 0.75 | 0.75 | 0.25 | high | report (validation failed) | 29 | 0.50 | 210 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/fqW91S9qZYa) |
| control-noisy | full | 1 | run-ae8e20c5f076 | claude-sonnet-4-5 | 0.75 | 0.75 | 0.25 |  | report (validation failed) | 26 | 0.56 | 179 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/EDoqvjjmYHD) |
| control-noisy | full | 2 | run-ae8e20c5f076 | claude-sonnet-4-5 | -0.50 | 0.00 | 0.25 | high | report (validation failed) | 34 | 0.63 | 269 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/8ct1S382bZV) |
| control-noisy | full | 3 | run-ae8e20c5f076 | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 |  | report (validation failed) | 24 | 0.47 | 200 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/3BqacPg1L9H) |
| control-quiet | full | 1 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 27 | 0.61 | 171 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/jCao9PBRayC) |
| control-quiet | full | 2 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 24 | 0.46 | 158 | 0.90 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/jSYxzHKmVYc) |
| control-quiet | full | 3 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 27 | 0.40 | 154 | 0.75 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/wsHVDSFrBj8) |
| dependency-inventory-db-timeouts | full | 1 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 0.50 | 0.75 | 0.00 | high | report (validation failed) | 27 | 0.66 | 225 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/CxdmB1gHoAc) |
| dependency-inventory-db-timeouts | full | 2 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 31 | 0.68 | 230 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/2ia8LaqMFm7) |
| dependency-inventory-db-timeouts | full | 3 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 0.75 | 0.75 | 0.25 | high | report (validation failed) | 29 | 0.58 | 200 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/7bed46ehmec) |
| deploy-regression-v260 | full | 1 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 26 | 0.66 | 202 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/zCbmv167acM) |
| deploy-regression-v260 | full | 2 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 19 | 0.41 | 152 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/a5ttdtzfBZH) |
| deploy-regression-v260 | full | 3 | run-2ef344b96d57 | claude-sonnet-4-5 | 0.90 | 0.75 | 0.15 | high | report | 21 | 0.42 | 138 | 0.88 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/bnx9b844D58) |
| error-surge-exceptions | full | 1 | run-a6353e4f46f8 | claude-sonnet-4-5 | 0.75 | 0.75 | 0.25 | high | report (validation failed) | 32 | 0.78 | 263 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/8ENXeYam1jZ) |
| error-surge-exceptions | full | 2 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 24 | 0.48 | 219 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/k4DDhWBQwpz) |
| error-surge-exceptions | full | 3 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 22 | 0.48 | 246 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/zsqhFARiQcH) |
| herring-customer-whale | full | 1 | run-29580981edf1 | claude-sonnet-4-5 | 0.75 | 0.75 | 0.25 | high | report (validation failed) | 28 | 0.63 | 185 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/kpctYpGRrYB) |
| herring-customer-whale | full | 2 | run-29580981edf1 | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 33 | 0.67 | 225 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/cjKUhtUHpjp) |
| herring-customer-whale | full | 3 | run-29580981edf1 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 28 | 0.56 | 179 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/y9KSuqUnmB) |
| herring-region-vs-version | full | 1 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 23 | 0.52 | 170 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/FaRnz7s6jBz) |
| herring-region-vs-version | full | 2 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 21 | 0.46 | 160 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/e7LFmm9HEB2) |
| herring-region-vs-version | full | 3 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 0.75 | 0.75 | 0.25 | high | report (validation failed) | 22 | 0.45 | 160 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/mPuucQn1p9r) |
| payments-stripe-v251-uswest | full | 1 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 24 | 0.52 | 161 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/bSdmDasU3Hv) |
| payments-stripe-v251-uswest | full | 2 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 20 | 0.37 | 138 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/nXYQV4ppESt) |
| payments-stripe-v251-uswest | full | 3 | run-f0d47eec0615 | claude-sonnet-4-5 | 0.75 | 0.75 | 0.25 | high | report (validation failed) | 28 | 0.60 | 210 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/G4cTrCnUwM5) |
| trigger-checkout-latency | full | 1 | run-f89265a20861 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 22 | 0.55 | 163 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/qNgvapCBj1z) |
| trigger-checkout-latency | full | 2 | run-288accc0a996 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 21 | 0.53 | 150 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/G93ebCChs3N) |
| trigger-checkout-latency | full | 3 | run-f407d9bb3908 | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 21 | 0.50 | 148 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/HexEaCzPzWp) |
