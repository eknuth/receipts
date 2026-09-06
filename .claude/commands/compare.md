---
description: Compare two result columns cell by cell. Usage /compare <before> <after>, paths under evals/results/
argument-hint: <before> <after>
allowed-tools: Bash(uv run python -m evals.compare:*)
---

Run `uv run python -m evals.compare $ARGUMENTS` and read it in the order the
`column-compare` skill gives: the paired line first, then by scenario, then the moved
cells, the controls, the validation codes. Reply with the paired total and outcome with
their signs, top right of n, validation failures, calls, cost, and the moved cells one line
each. Open a moved cell with `/cell` before giving a reason for it.
