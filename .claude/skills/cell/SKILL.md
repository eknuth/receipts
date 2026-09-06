---
name: cell
description: Read one result cell (grade, validation, hypotheses, evidence, tool log) with evals/cell.py. Usage /cell <column> <scenario> <n>.
allowed-tools: Bash(uv run python -m evals.cell:*)
---

Run `uv run python -m evals.cell $ARGUMENTS` and read the output in the order the
`cell-read` skill gives: the component under 1.0, the validation messages, the top
hypothesis against `gen/scenarios/<scenario>.yml`, then the query in the tool log that
would have settled it. Reply with the loss named as a method gap in general form, with the
query id or the missing query, in under ten lines.
