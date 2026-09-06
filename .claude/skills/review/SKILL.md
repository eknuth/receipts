---
name: review
description: Spawn the Fable adversarial review that every Receipts issue gets before its PR is called ready, with the two lenses Ed set on 2026-09-03, implementation correctness with reproduced findings, and whether the work advances getting hired for Honeycomb's Agentic Intelligence role. Use when an implementer has reported, when Ed says "spawn the review" or "adversarial review", or before marking a PR ready.
---

# Adversarial review

What this replaces. Thirteen orchestrator turns ask for an adversarial review and 23 of the
51 subagent spawns are one by their label; the brief was retyped each time, and the R8 review that set the
process found fifteen real things a single-session build had missed (a paid-for report
discarded on session close, a top-right column that counted an empty report as right). The
brief below is the one from `feedback-issue-process.md`.

Argument: what to review, a PR number, a branch, or a path range. Default is the current
branch against `main`.

## Spawn

Use the `Agent` tool with `model: "fable"` and `subagent_type: "general-purpose"`. The
prompt is this file's brief plus the issue id, the branch, the diff range, where the pass
results are (`evals/results/<park>/full` and `evals/results/full`), and the lines: read
only, do not push, do not post to Linear, do not touch the browser, do not modify anything
under `~/.claude/`.

## The brief

Two lenses, reported separately.

Lens one, correctness. Read the whole diff, then the tests, then run `make test` and `make
lint`. For each claim in the PR description or the issue comment, find the code or the
result cell that supports it and say whether it does. Reproduce every finding: a number
from a `grade.json`, a query from a tool log (`uv run python -m evals.cell`), a failing
input to a function. A finding without a reproduction is a question, listed separately.
Check the things this project gets wrong: an asymmetry that makes one answer cheaper than
the other; a rule that bites in one direction; a fixture or constant carrying an answer; a
scenario id, run id, or key on the wire or in a file; prose in the wrong voice (em dashes,
litotes, irony, rule-of-three, hedging); a number in the README that the report does not
produce; a stray issue number in a docstring or test header.

Lens two, the hiring goal. The reader is a Honeycomb engineer deciding whether this
person should build agentic features on their product. Does the change show its grounding
(receipts, negation, not-checked) in the tool log? Does it fix the method and leave the
scenarios alone? Would the README sentence this change earns survive that reader running the
code? Say what the change adds to that case and what it costs.

## Output

Numbered findings, each with severity (blocks merge, fix before ready, nit), the file and
line or the cell, the reproduction, and the fix in one sentence. Then the lens-two verdict
in one paragraph. Then the questions. The orchestrator fixes in-scope findings, records the
rest on the issue, and reruns the review only when a fix changed the method or the numbers.
