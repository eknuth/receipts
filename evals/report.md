# Eval report

Every number here is read from a `grade.json` under `evals/results/`, written by `evals/run.py` and graded by `evals/grader.py` (weights and penalties are explained in `evals/grader.md`). Nothing is typed by hand. A run that crashed, or that the loop ended with an error, is a row with a total of 0 and the error text; it was not put through the grader.

`total` is the grader's full score, penalties included, between -1 and 1. `outcome` is the weighted dims, span, incident, and onset components alone, at most 0.75, and sits next to `total` because an ablation that removes a rule loses that rule's weight by construction; whether it changed the answer is a question about `outcome`. `top right` counts runs whose top hypothesis scored at least 0.5 on the grader's dims component, which is the grader's own line for a wrong hypothesis. On a control a report with no hypothesis, or only low ones, scores 1 there and counts; on an incident scenario a report with no hypothesis scores 0 and does not; a crash never does. Ranges are the lowest and highest single run.

## Scores by scenario

| scenario | full total | full outcome | full top right |
| --- | --- | --- | --- |
| checkout-error-surge-adyen | 0.88 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| control-noisy | 0.50 (-0.50 to 1.00) | 0.50 (0.00 to 0.75) | 2 of 3 |
| control-quiet | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| dependency-inventory-db-timeouts | 0.67 (0.00 to 1.00) | 0.50 (0.00 to 0.75) | 2 of 3 |
| deploy-regression-v260 | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| error-surge-exceptions | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| herring-customer-whale | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| herring-region-vs-version | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| payments-stripe-v251-uswest | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| trigger-checkout-latency | 0.77 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| all scenarios | 0.88 (-0.50 to 1.00) | 0.70 (0.00 to 0.75) | 28 of 30 |

## Process by config

Means over every run in the config, crashes included. `passes theirs, fails ours` counts runs that pass Honeycomb's process evaluator (a reimplementation of `tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, pass at 0.6) and score a `total` under 0.50 on ours. A crash has no process score and is not counted as passing theirs. The line is on `total`, so under an ablation config the removed rule's weight (0.15 for the negation, which the grader requires whatever the config) counts against the run here; read `outcome` in the scenario table for whether the answer changed. `tokens in` is uncached input, as the grade records it; the prompt cache reads that make up most of what the model read are in each `report.json` and are already priced into the cost. `coerced` counts two kinds of fix, across every attempt and every tool call in the config: submit_report fields decoded from a JSON-encoded string or unwrapped from a stray wrapper key around the whole report, and BubbleUp group values the MCP client retyped from the column schema rather than sending on as the model wrote them; blank when none were.

| config | runs | crashed | mean calls | mean tokens in | mean tokens out | mean cost USD | mean wall s | coerced | passes theirs, fails ours (total < 0.50) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full | 30 | 0 | 26.8 | 548 | 9,692 | 0.51 | 213 | 3 | 2 of 30 |

## Runs

One row per investigation. `query` links to the first evidence query of the top hypothesis, or to the first baseline query when the report names no hypothesis. `theirs` is Honeycomb's process score with its pass mark applied.

| scenario | config | n | run id | model | total | outcome | receipts | top confidence | stopped by | calls | cost USD | wall s | theirs | query |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| checkout-error-surge-adyen | full | 1 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 25 | 0.47 | 159 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/vUx4AepQes8) |
| checkout-error-surge-adyen | full | 2 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 30 | 0.46 | 193 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/jFMNEzX9D3E) |
| checkout-error-surge-adyen | full | 3 | run-974f4e6bd0ed | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 33 | 0.64 | 243 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/fU51kZdFQzM) |
| control-noisy | full | 1 | run-ae8e20c5f076 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 34 | 0.74 | 315 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/6QF4hTeE2oW) |
| control-noisy | full | 2 | run-ae8e20c5f076 | claude-sonnet-4-5 | -0.50 | 0.00 | 0.00 | medium | report (validation failed) | 40 | 0.66 | 299 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/JnbvZRpwYam) |
| control-noisy | full | 3 | run-ae8e20c5f076 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 26 | 0.49 | 253 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/tW9ZsC4StNW) |
| control-quiet | full | 1 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 28 | 0.55 | 171 | 0.90 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/f8YKFwja5Kf) |
| control-quiet | full | 2 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 34 | 0.52 | 190 | 0.95 pass |  |
| control-quiet | full | 3 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 29 | 0.54 | 201 | 0.95 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/sfyN68cDW6Q) |
| dependency-inventory-db-timeouts | full | 1 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 32 | 0.62 | 228 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/nJu5Nv8XftP) |
| dependency-inventory-db-timeouts | full | 2 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 0.00 | 0.00 | 0.00 |  | wall_cap | 24 | 0.48 | 480 | 1.00 pass |  |
| dependency-inventory-db-timeouts | full | 3 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 21 | 0.34 | 166 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/8V2JMmwzAqf) |
| deploy-regression-v260 | full | 1 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 22 | 0.50 | 162 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/JAooXABmTJv) |
| deploy-regression-v260 | full | 2 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 27 | 0.48 | 189 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/q2szfAM9arV) |
| deploy-regression-v260 | full | 3 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 18 | 0.33 | 136 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/qkJKLRvSGi3) |
| error-surge-exceptions | full | 1 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 31 | 0.63 | 238 | 1.00 pass |  |
| error-surge-exceptions | full | 2 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 22 | 0.37 | 173 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/pn6yJLuRxVG) |
| error-surge-exceptions | full | 3 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 28 | 0.53 | 249 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/GYDdXe2w8pH) |
| herring-customer-whale | full | 1 | run-29580981edf1 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 28 | 0.61 | 282 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/5MFYTRxVgWn) |
| herring-customer-whale | full | 2 | run-29580981edf1 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 24 | 0.47 | 208 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/cdP427wiiJf) |
| herring-customer-whale | full | 3 | run-29580981edf1 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 28 | 0.52 | 228 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/yHRrmzLw5Er) |
| herring-region-vs-version | full | 1 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 20 | 0.46 | 206 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/6cFhovCGAmw) |
| herring-region-vs-version | full | 2 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 21 | 0.36 | 164 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/gwMYqUhyo1) |
| herring-region-vs-version | full | 3 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 23 | 0.37 | 179 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/jXChQrs8dqG) |
| payments-stripe-v251-uswest | full | 1 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 30 | 0.66 | 209 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/xkdtusNt3fZ) |
| payments-stripe-v251-uswest | full | 2 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 25 | 0.50 | 190 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/pPFuhRYEH5z) |
| payments-stripe-v251-uswest | full | 3 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 20 | 0.31 | 129 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/EFMRyvi52YZ) |
| trigger-checkout-latency | full | 1 | run-a21ce3773ac0 | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 30 | 0.69 | 205 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/gh2oPtrBq7r) |
| trigger-checkout-latency | full | 2 | run-d065503386dc | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 23 | 0.47 | 162 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/k4RNqwBZHkx) |
| trigger-checkout-latency | full | 3 | run-2eee1f397cae | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 28 | 0.51 | 178 | 0.88 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/bqfSvXBhJiB) |
