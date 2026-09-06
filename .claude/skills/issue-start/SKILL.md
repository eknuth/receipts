---
name: issue-start
description: Start a Receipts issue the way every issue since R9 has been started. Reads CLAUDE.md, the newest handoff memory, and the Linear issue; marks it In Progress; branches r<N>-<slug> off main with the next free R number; hands the spec to an implementer subagent on the issue's labeled model. Use at the top of a fresh session when Ed says "start on EDW-<n>", "read handoff and start", or pastes a prompt that begins with the issue number.
---

# Start an issue

What this replaces. Thirteen orchestrator turns in the transcripts ask for a handoff prompt
or to clear context, and each new session then re-derives the same opening by hand: read
CLAUDE.md, read the handoff, read the issue (over forty `get_issue` calls), find the next R
number, branch (over a dozen branch commands), spawn the implementer. Two sessions got the
R number wrong
because it was read off branch names alone. The steps below are the process in
`feedback-issue-process.md`, in order, with the R number taken from both sources.

Argument: the issue id, for example `EDW-1368`.

## Steps

1. Read, in this order: `CLAUDE.md`; the newest `handoff-*.md` under
   `~/.claude/projects/-Users-eknuth-proj-receipts/memory/` (its Gotchas section first);
   the issue with `mcp__linear__get_issue` and its comments with `mcp__linear__list_comments`.
   The issue carries Context, Spec, Acceptance criteria, and Out of scope. Quote nothing
   from it back to Ed; he wrote it.
2. Find the next R number. Linear is authoritative for the numbering. Take the highest
   `R<N>` from the issue titles in the project and from `git log --merges --oneline main`
   and `git branch -a`, then use N+1 unless the issue title already names its R. State the
   mapping in the first reply ("R20 is EDW-1368").
3. Branch off main and nothing else: `git checkout main && git pull --ff-only && git checkout
   -b r<N>-<slug>`. One issue per branch. If a branch for the issue exists, check it out
   instead and say so.
4. Mark the issue In Progress with `mcp__linear__save_issue`. This is the one Linear write
   the opening makes; comments come later, with results.
5. Spawn one implementer subagent with the `Agent` tool on the model the issue is labeled
   for (`model:sonnet` means `model: "sonnet"`). The prompt carries the whole spec, the
   things to get right, the tests to write, the paid-run budget, the branch name, and these
   lines: do not push, do not open a PR, do not post to Linear, do not touch the browser, do
   not write a secret anywhere. The orchestrator does not implement.
6. When the implementer reports, verify: `make test`, `make lint`, `git diff main...HEAD`
   read in full, the live check the issue names. Browser work is the orchestrator's, with
   `browser-harness` against Ed's signed-in Chrome; stop at a login wall, MFA, a payment
   step, or anything outside `receipts-demo`.
7. Then `/review`. Fix in-scope findings, record the rest on the issue, push, open the PR,
   tell Ed. Ed says merge.

## Reply shape

Branch name, the R mapping, the implementer's model, the paid-run budget, and what Ed will
be asked for (a browser step, a merge). Nothing else until the implementer reports.
