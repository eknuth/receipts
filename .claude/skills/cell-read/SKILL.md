---
name: cell-read
description: Read one result cell with evals/cell.py to find why it lost points, before any method change is proposed. Use when a cell in the report sits under 1.0, when Ed asks "why did it miss", or when a review needs the tool log behind a claim.
---

# Read a cell

What this replaces. A `dump.py` was rewritten in four session scratchpads to print the same
things: the grade components, the validation messages, the hypotheses with their evidence,
the baseline queries, the rejected candidates, and the `run_query` calls with their
breakdowns, filters, granularity, and window. The transcripts show over a hundred commands
reading a `tool_log` by hand and over three hundred reading a `grade.json` or
`report.json`. Every method change on
this project came from reading cells this way: the split-in-time step, the population step,
the noise floor.

Arguments: `<column> <scenario> <n>`, for example `full control-noisy 2`.

## Run

```
uv run python -m evals.cell <column> <scenario> <n>
```

## Read it in this order

1. The grade. Each component is 0 to 1 with its weight beside it; the one under 1.0 names
   the loss. The penalties line says whether calibration or a validation failure took the
   rest. `top wrong` with `high` confidence is the expensive case.
2. The validation messages. Each names a rule and a slot. A `not_checked` message means an
   entry was in the tool log; a `partially_checked` message means the log contradicts the
   reading. Both are the model writing what it did not do.
3. The hypotheses. Does the top one's `dims` match the ground truth in
   `gen/scenarios/<scenario>.yml`? Does the negation exclude the population named, or a
   symptom column? An empty `excludes` is the symptom-column-in-dims shape.
4. The tool log. Find the query that would have settled it and check whether it was run.
   The usual shapes: a breakdown on COUNT where the measurement was P99; a candidate read
   against one sparse bucket instead of the window; a negation over an earlier window; a
   `dims` read off a carried scope filter.
5. The rejected candidates and the baseline. On a control these are the report.

## Then

Name the loss as a method gap in general form, never as a scenario to tune. A gap becomes a
Linear issue with a before-and-after pass as its acceptance. If the harness made one answer
cheaper than the other, fix the harness first (`project-control-failure-punish-not-prompt.md`).
