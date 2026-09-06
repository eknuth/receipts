---
name: column-compare
description: Compare two result columns cell by cell with evals/compare.py and read the result the same way each time. Use after a pass, when Ed asks "did it move", "is this better", or "compare the before and after", or when a PR needs its before-and-after table.
---

# Compare two columns

What this replaces. A `compare.py`, `analyze.py`, or `recompute.py` was written from scratch
in four session scratchpads, and over three hundred commands in the transcripts read a
`grade.json` or `report.json` by hand. The one lesson those scripts converged on is in the memory files:
pair the cells and report the paired mean, because a headline mean over unequal sets moved
once for a wall cap and once for a noise-floor miss that had nothing to do with the change.

Arguments: `<before> <after>`, both paths under `evals/results/`. A live column is one
level (`full`); a parked one is two (`r18-pass/full`).

## Run

```
uv run python -m evals.compare <before> <after>
```

## Read it in this order

1. The `paired n=` line. That is the before-and-after. When the paired n is under thirty,
   say which cells are missing and why (a crash, a cap, a cell not yet run).
2. The by-scenario table. A delta that lives in one scenario is a scenario finding; a delta
   spread across most of them is a method finding.
3. The moved cells. Open each with `/cell` before saying why it moved. The grade components
   name the lost weight; the tool log names the query.
4. The controls. `incident_present` and the hypothesis count are the whole test there. A
   control that filed a hypothesis at any confidence is a false incident, and the fix is in
   the method or the validator, never in wording about controls (see
   `project-control-failure-punish-not-prompt.md`).
5. The validation codes. A code that grew is a rule the model is now tripping; read one cell
   to see whether the rule or the report is wrong.

## Report

Paired total and outcome with the sign, top right of n, validation failures, mean calls, and
the cost, then the moved cells one line each with the reason from `/cell`. The README and
the PR get the same numbers; nothing is rounded differently in two places.
