# Eval report

Every number here is read from a `grade.json` under `evals/results/`, written by `evals/run.py` and graded by `evals/grader.py` (weights and penalties are explained in `evals/grader.md`). Nothing is typed by hand. A run that crashed, or that the loop ended with an error, is a row with a total of 0 and the error text; it was not put through the grader.

`total` is the grader's full score, penalties included, between -1 and 1. `outcome` is the weighted dims, span, incident, and onset components alone, at most 0.75, and sits next to `total` because an ablation that removes a rule loses that rule's weight by construction; whether it changed the answer is a question about `outcome`. `top right` counts runs whose top hypothesis scored at least 0.5 on the grader's dims component, which is the grader's own line for a wrong hypothesis. On a control a report with no hypothesis, or only low ones, scores 1 there and counts; on an incident scenario a report with no hypothesis scores 0 and does not; a crash never does. Ranges are the lowest and highest single run.

## Scores by scenario

| scenario | full total | full outcome | full top right |
| --- | --- | --- | --- |
| checkout-error-surge-adyen | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| control-noisy | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| control-quiet | 0.88 (0.65 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| dependency-inventory-db-timeouts | 0.75 (0.60 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| deploy-regression-v260 | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| error-surge-exceptions | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| herring-customer-whale | 0.87 (0.60 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| herring-region-vs-version | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| payments-stripe-v251-uswest | 0.64 (-0.08 to 1.00) | 0.67 (0.52 to 0.75) | 2 of 3 |
| trigger-checkout-latency | 1.00 (1.00 to 1.00) | 0.75 (0.75 to 0.75) | 3 of 3 |
| all scenarios | 0.91 (-0.08 to 1.00) | 0.74 (0.52 to 0.75) | 29 of 30 |

## Process by config

Means over every run in the config, crashes included. `passes theirs, fails ours` counts runs that pass Honeycomb's process evaluator (a reimplementation of `tests/scenarios/evaluator.py` in `honeycombio/agent-skill`, pass at 0.6) and score a `total` under 0.50 on ours. A crash has no process score and is not counted as passing theirs. The line is on `total`, so under an ablation config the removed rule's weight (0.15 for the negation, which the grader requires whatever the config) counts against the run here; read `outcome` in the scenario table for whether the answer changed. `tokens in` is uncached input, as the grade records it; the prompt cache reads that make up most of what the model read are in each `report.json` and are already priced into the cost. `coerced` counts two kinds of fix, across every attempt and every tool call in the config: submit_report fields decoded from a JSON-encoded string or unwrapped from a stray wrapper key around the whole report, and BubbleUp group values the MCP client retyped from the column schema rather than sending on as the model wrote them; blank when none were.

| config | runs | crashed | mean calls | mean tokens in | mean tokens out | mean cost USD | mean wall s | coerced | passes theirs, fails ours (total < 0.50) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full | 30 | 0 | 27.0 | 542 | 9,596 | 0.47 | 177 | 11 | 1 of 30 |

## Runs

One row per investigation. `query` links to the first evidence query of the top hypothesis, or to the first baseline query when the report names no hypothesis. `theirs` is Honeycomb's process score with its pass mark applied.

| scenario | config | n | run id | model | total | outcome | receipts | top confidence | stopped by | calls | cost USD | wall s | theirs | query |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| checkout-error-surge-adyen | full | 1 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 28 | 0.46 | 171 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/2Tpa5HhgeHU) |
| checkout-error-surge-adyen | full | 2 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 25 | 0.45 | 184 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/wwNY97kTFDU) |
| checkout-error-surge-adyen | full | 3 | run-974f4e6bd0ed | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 28 | 0.40 | 204 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/DUpWnhUW2id) |
| control-noisy | full | 1 | run-ae8e20c5f076 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 30 | 0.57 | 183 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/wP3rSjvDqWX) |
| control-noisy | full | 2 | run-ae8e20c5f076 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 30 | 0.48 | 173 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/gx9rbT2NzZq) |
| control-noisy | full | 3 | run-ae8e20c5f076 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 30 | 0.44 | 180 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/uUMYWQ3LHme) |
| control-quiet | full | 1 | run-a265adf6e322 | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 |  | report (validation failed) | 22 | 0.44 | 148 | 0.90 pass |  |
| control-quiet | full | 2 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 25 | 0.35 | 145 | 0.90 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/cwtK4iazydJ) |
| control-quiet | full | 3 | run-a265adf6e322 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 |  | report | 24 | 0.44 | 161 | 0.90 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/Xbxw7aDReQ) |
| dependency-inventory-db-timeouts | full | 1 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 0.65 | 0.75 | 0.15 | high | report (validation failed) | 35 | 0.73 | 238 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/p3h54LL8hoX) |
| dependency-inventory-db-timeouts | full | 2 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 32 | 0.58 | 216 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/B3UbiLr1sD2) |
| dependency-inventory-db-timeouts | full | 3 | run-e10b6c1bf2dc | claude-sonnet-4-5 | 0.60 | 0.75 | 0.10 | high | report (validation failed) | 26 | 0.50 | 196 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/oguTfibLx26) |
| deploy-regression-v260 | full | 1 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 23 | 0.46 | 132 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/4HXpb12o9Kd) |
| deploy-regression-v260 | full | 2 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 23 | 0.35 | 151 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/9UoHjp6eFvC) |
| deploy-regression-v260 | full | 3 | run-2ef344b96d57 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 24 | 0.47 | 173 | 0.68 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/H3sGe4J25k5) |
| error-surge-exceptions | full | 1 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 26 | 0.45 | 168 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/JfPDVvrfMKe) |
| error-surge-exceptions | full | 2 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 29 | 0.43 | 198 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/9eQunNnfjrm) |
| error-surge-exceptions | full | 3 | run-a6353e4f46f8 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 30 | 0.50 | 191 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/DqJFdvVSvxi) |
| herring-customer-whale | full | 1 | run-29580981edf1 | claude-sonnet-4-5 | 0.60 | 0.75 | 0.10 | high | report (validation failed) | 31 | 0.60 | 207 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/EoQThdX7f4D) |
| herring-customer-whale | full | 2 | run-29580981edf1 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 23 | 0.33 | 144 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/J58XEoCd86) |
| herring-customer-whale | full | 3 | run-29580981edf1 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 31 | 0.49 | 193 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/m9nG3yE9aEf) |
| herring-region-vs-version | full | 1 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 24 | 0.48 | 145 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/DNBda6iD6Bx) |
| herring-region-vs-version | full | 2 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 26 | 0.38 | 150 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/E8shbo3NRzM) |
| herring-region-vs-version | full | 3 | run-cd1ec0dcfc51 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 21 | 0.35 | 159 | 0.80 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/qYEErEomFPp) |
| payments-stripe-v251-uswest | full | 1 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 37 | 0.77 | 247 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/sHUM7CdweoD) |
| payments-stripe-v251-uswest | full | 2 | run-f0d47eec0615 | claude-sonnet-4-5 | -0.08 | 0.52 | 0.15 | high | report (validation failed) | 24 | 0.40 | 170 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/o96Fh29Dw4m) |
| payments-stripe-v251-uswest | full | 3 | run-f0d47eec0615 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 34 | 0.58 | 226 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/2KcQi8z7Q9s) |
| trigger-checkout-latency | full | 1 | run-160da8a5c092 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 26 | 0.45 | 165 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/m3Cy5ZSdyYJ) |
| trigger-checkout-latency | full | 2 | run-697e3ee9e27d | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 22 | 0.45 | 150 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/nznMQwAcac5) |
| trigger-checkout-latency | full | 3 | run-403f1573b841 | claude-sonnet-4-5 | 1.00 | 0.75 | 0.25 | high | report | 20 | 0.43 | 149 | 1.00 pass | [query](https://ui.honeycomb.io/joymath/environments/receipts-demo/datasets/receipts-shop/result/hcGZw4Eat2p) |
