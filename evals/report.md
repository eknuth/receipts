# Eval report

Every number here is read from a `grade.json` under `evals/results/`, written by `evals/run.py` and graded by `evals/grader.py` (weights and penalties are explained in `evals/grader.md`). Nothing is typed by hand. A run that crashed, or that the loop ended with an error, is a row with a total of 0 and the error text; it was not put through the grader.

`total` is the grader's full score, penalties included, between -1 and 1. `outcome` is the weighted dims, span, incident, and onset components alone, at most 0.75, and sits next to `total` because an ablation that removes a rule can lose that rule's weight without changing an answer; whether it changed the answer is a question about `outcome`. `top right` counts runs whose top hypothesis scored at least 0.5 on the grader's dims component, which is the grader's own line for a wrong hypothesis. On a control a filed report with no hypothesis, or only low ones, scores 1 there and counts; on an incident scenario a report with no hypothesis scores 0 and does not; a run that filed nothing (stopped by a cap, or by a submit_report that never matched the schema) scores 0 on both, and a crash never does. Ranges are the lowest and highest single run.

## Scores by scenario

| scenario | full total | full outcome | full top right |
| --- | --- | --- | --- |
| checkout-error-surge-adyen | 0.45 (-0.10 to 0.85) | 0.45 (0.15 to 0.60) | 2 of 3 |
| control-noisy | -0.25 (-0.25 to -0.25) | 0.00 (0.00 to 0.00) | 0 of 3 |
| control-quiet | -0.25 (-0.25 to -0.25) | 0.00 (0.00 to 0.00) | 0 of 3 |
| dependency-inventory-db-timeouts | -0.25 (-0.50 to 0.00) | 0.08 (0.00 to 0.25) | 0 of 3 |
| deploy-regression-v260 | 0.35 (-0.25 to 0.85) | 0.40 (0.00 to 0.60) | 2 of 3 |
| error-surge-exceptions | -0.22 (-0.40 to 0.00) | 0.08 (0.00 to 0.25) | 0 of 3 |
| herring-customer-whale | 0.20 (-0.25 to 0.85) | 0.28 (0.00 to 0.60) | 1 of 3 |
| herring-region-vs-version | 0.20 (0.00 to 0.60) | 0.20 (0.00 to 0.60) | 1 of 3 |
| payments-stripe-v251-uswest | 0.00 (0.00 to 0.00) | 0.00 (0.00 to 0.00) | 0 of 3 |
| trigger-checkout-latency | -0.30 (-0.40 to -0.25) | 0.08 (0.00 to 0.25) | 0 of 3 |
| all scenarios | -0.01 (-0.50 to 0.85) | 0.16 (0.00 to 0.60) | 6 of 30 |

## Process by config

Means over every run in the config, crashes included. `passes theirs, fails ours` counts runs that pass Honeycomb's process evaluator (a reimplementation of `tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, pass at 0.6) and score a `total` under 0.50 on ours. A crash has no process score and is not counted as passing theirs. The line is on `total`, so under an ablation config the removed rule's weight (0.15 for the negation, 0.10 for the not-checked list, both scored whatever the config) can count against the run here; read `outcome` in the scenario table for whether the answer changed. `tokens in` is uncached input, as the grade records it; the prompt cache reads that make up most of what the model read are in each `report.json` and are already priced into the cost. `mean cost USD` reads $0.00 for `qwen3.8:27b`, which runs on local hardware, and for the NVIDIA rows on the unmetered developer tier: `nvidia/nemotron-3-super-120b-a12b`, the `NVIDIA_MODEL` default, and `moonshotai/kimi-k3`, tried first and kept as a row after it turned out throttled on this key; `evals/pricing.yml` prices all three at zero. `coerced` counts two kinds of fix, across every attempt and every tool call in the config: submit_report fields decoded from a JSON-encoded string or unwrapped from a stray wrapper key around the whole report, and BubbleUp group values the MCP client retyped from the column schema rather than sending on as the model wrote them; blank when none were. `total cost USD` sums the same cost column instead of averaging it, and the line under the table sums that column again across every config. `wall cap s` is the mean of `Report.max_wall_s`, the wall-clock budget each run was given, over the runs in the row that recorded one; blank when none did, which is every run from before this column existed. `malformed calls` sums `Report.malformed_calls`, tool calls a provider handed back with arguments the client could not parse into an object at all, across the row; blank when none.

| config | runs | crashed | mean calls | mean tokens in | mean tokens out | mean cost USD | total cost USD | mean wall s | coerced | passes theirs, fails ours (total < 0.50) | wall cap s | malformed calls |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full | 30 | 0 | 33.9 | 1,501,110 | 26,815 | 0.00 | 0.00 | 352 |  | 25 of 30 | 480 |  |

Total Anthropic spend across every run in this results directory: $0.00.

## Runs

One row per investigation. `query` links to the first evidence query of the top hypothesis, or to the first baseline query when the report names no hypothesis. `theirs` is Honeycomb's process score with its pass mark applied.

| scenario | config | n | run id | model | total | outcome | receipts | top confidence | stopped by | calls | cost USD | wall s | theirs | query |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| checkout-error-surge-adyen | full | 1 | run-974f4e6bd0ed | nvidia/nemotron-3-super-120b-a12b | -0.10 | 0.15 | 0.10 | low | report (validation failed) | 40 | 0.00 | 458 | 1.00 pass |  |
| checkout-error-surge-adyen | full | 2 | run-974f4e6bd0ed | nvidia/nemotron-3-super-120b-a12b | 0.60 | 0.60 | 0.25 | high | report (validation failed) | 28 | 0.00 | 273 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/qhQieLM6z79) |
| checkout-error-surge-adyen | full | 3 | run-974f4e6bd0ed | nvidia/nemotron-3-super-120b-a12b | 0.85 | 0.60 | 0.25 | high | report | 40 | 0.00 | 225 | 0.70 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/j5FzgQjpXHy) |
| control-noisy | full | 1 | run-ae8e20c5f076 | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.00 | 0.00 |  | schema (validation failed) | 40 | 0.00 | 234 | 0.95 pass |  |
| control-noisy | full | 2 | run-ae8e20c5f076 | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.00 | 0.00 |  | schema (validation failed) | 35 | 0.00 | 363 | 0.95 pass |  |
| control-noisy | full | 3 | run-ae8e20c5f076 | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.00 | 0.25 | high | report | 32 | 0.00 | 309 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/iFbE9jazNxj) |
| control-quiet | full | 1 | run-a265adf6e322 | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.00 | 0.00 |  | schema (validation failed) | 38 | 0.00 | 308 | 0.90 pass |  |
| control-quiet | full | 2 | run-a265adf6e322 | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.00 | 0.00 |  | schema (validation failed) | 33 | 0.00 | 179 | 0.90 pass |  |
| control-quiet | full | 3 | run-a265adf6e322 | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.00 | 0.00 |  | schema (validation failed) | 32 | 0.00 | 308 | 0.95 pass |  |
| dependency-inventory-db-timeouts | full | 1 | run-e10b6c1bf2dc | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.00 | 0.00 |  | schema (validation failed) | 40 | 0.00 | 450 | 0.75 pass |  |
| dependency-inventory-db-timeouts | full | 2 | run-e10b6c1bf2dc | nvidia/nemotron-3-super-120b-a12b | -0.50 | 0.25 | 0.00 | high | report (validation failed) | 35 | 0.00 | 282 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/2CAf9Gf5NXS) |
| dependency-inventory-db-timeouts | full | 3 | run-e10b6c1bf2dc | nvidia/nemotron-3-super-120b-a12b | 0.00 | 0.00 | 0.00 |  | call_cap | 40 | 0.00 | 432 | 0.95 pass |  |
| deploy-regression-v260 | full | 1 | run-2ef344b96d57 | nvidia/nemotron-3-super-120b-a12b | 0.85 | 0.60 | 0.25 | high | report | 24 | 0.00 | 328 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/DenbKDnStXf?tab=bubbleup&bubbleup_result=FFWFB1d3KdQ) |
| deploy-regression-v260 | full | 2 | run-2ef344b96d57 | nvidia/nemotron-3-super-120b-a12b | 0.45 | 0.60 | 0.10 | high | report (validation failed) | 27 | 0.00 | 332 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/bbuj725fotB) |
| deploy-regression-v260 | full | 3 | run-2ef344b96d57 | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.00 | 0.00 |  | schema (validation failed) | 34 | 0.00 | 458 | 0.82 pass |  |
| error-surge-exceptions | full | 1 | run-a6353e4f46f8 | nvidia/nemotron-3-super-120b-a12b | 0.00 | 0.00 | 0.00 |  | wall_cap | 40 | 0.00 | 480 | 0.95 pass |  |
| error-surge-exceptions | full | 2 | run-a6353e4f46f8 | nvidia/nemotron-3-super-120b-a12b | -0.40 | 0.25 | 0.10 | high | report (validation failed) | 40 | 0.00 | 308 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/fmhm4HCTeb1) |
| error-surge-exceptions | full | 3 | run-a6353e4f46f8 | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.00 | 0.00 |  | schema (validation failed) | 40 | 0.00 | 393 | 0.90 pass |  |
| herring-customer-whale | full | 1 | run-29580981edf1 | nvidia/nemotron-3-super-120b-a12b | 0.85 | 0.60 | 0.25 | high | report | 12 | 0.00 | 125 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/hc4TZScZRK7) |
| herring-customer-whale | full | 2 | run-29580981edf1 | nvidia/nemotron-3-super-120b-a12b | 0.00 | 0.00 | 0.00 |  | call_cap | 40 | 0.00 | 374 | 0.90 pass |  |
| herring-customer-whale | full | 3 | run-29580981edf1 | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.25 | 0.00 | medium | report (validation failed) | 40 | 0.00 | 300 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/4gbDS5Zx9Wp) |
| herring-region-vs-version | full | 1 | run-cd1ec0dcfc51 | nvidia/nemotron-3-super-120b-a12b | 0.60 | 0.60 | 0.25 | high | report (validation failed) | 35 | 0.00 | 312 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/yA5yV1gB2e) |
| herring-region-vs-version | full | 2 | run-cd1ec0dcfc51 | nvidia/nemotron-3-super-120b-a12b | 0.00 | 0.00 | 0.00 |  | call_cap | 40 | 0.00 | 468 | 0.95 pass |  |
| herring-region-vs-version | full | 3 | run-cd1ec0dcfc51 | nvidia/nemotron-3-super-120b-a12b | 0.00 | 0.00 | 0.00 |  | wall_cap | 33 | 0.00 | 480 | 0.95 pass |  |
| payments-stripe-v251-uswest | full | 1 | run-f0d47eec0615 | nvidia/nemotron-3-super-120b-a12b | 0.00 | 0.00 | 0.00 |  | wall_cap | 30 | 0.00 | 480 | 0.95 pass |  |
| payments-stripe-v251-uswest | full | 2 | run-f0d47eec0615 | nvidia/nemotron-3-super-120b-a12b | 0.00 | 0.00 | 0.00 |  | wall_cap | 22 | 0.00 | 480 | 0.95 pass |  |
| payments-stripe-v251-uswest | full | 3 | run-f0d47eec0615 | nvidia/nemotron-3-super-120b-a12b | 0.00 | 0.00 | 0.00 |  | wall_cap | 32 | 0.00 | 480 | 0.95 pass |  |
| trigger-checkout-latency | full | 1 | run-8aa9cbafb7ef | nvidia/nemotron-3-super-120b-a12b | -0.40 | 0.25 | 0.10 | high | report (validation failed) | 29 | 0.00 | 239 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/tDtyQRfZxvb) |
| trigger-checkout-latency | full | 2 | run-fd1ab9cda8b6 | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.00 | 0.00 |  | schema (validation failed) | 40 | 0.00 | 434 | 0.95 pass |  |
| trigger-checkout-latency | full | 3 | run-5de95a5f0234 | nvidia/nemotron-3-super-120b-a12b | -0.25 | 0.00 | 0.00 |  | schema (validation failed) | 25 | 0.00 | 281 | 0.95 pass |  |
