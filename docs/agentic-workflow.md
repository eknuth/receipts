# How Receipts is built with Claude Code

Receipts has been built with Claude Code since 2026-09-02. Fable orchestrates. The model an
issue is labeled for implements in a subagent. A Fable subagent reviews the result for
correctness and for whether it helps the Honeycomb case. The orchestrator does the browser
work. This note reports what the transcripts show the work asking for over and over, and what
`.claude/` now carries so those steps stop being re-derived by hand.

Every number here is printed by `tools/usage_stats.py` from the transcripts under
`~/.claude/projects/-Users-eknuth-proj-receipts/`. The tables at the end are the verbatim
output of `uv run python tools/usage_stats.py --top 32 --before 2026-09-06T22:34:36Z`, and a
test regenerates them with that command and compares byte for byte. The script prints counts,
tool names, normalized command shapes, and the label an `Agent` call was given; no conversation
text and no memory file name, and a test pins that its output has no line over 120 characters
and no key-shaped substring.

## Method

One JSONL line per event; a tool call is a `tool_use` block in an assistant message.
Orchestrator sessions are the top-level files, subagents have their own files under
`<session>/subagents/`, and `--before` drops every entry stamped after the cutoff. A shell
command is tokenized with `shlex`, split at `&&`, `||`, `;`, `|`, and newlines, cut at a
heredoc or an inline program, and each piece reduced to a shape: the first token, plus the
subcommand for `git`, `gh`, `make`, and `uv run`, and the module for `python -m`; paths become
`<path>`. Families match a regex against the whole command. User turns are Ed's text turns,
with injected notifications excluded and interrupts counted apart. Denials are the
`toolDenialKind` field plus tool results that begin "Permission for this action was denied";
the nine of each are the same nine events. Approved prompts leave no record.

Caveats. Claude Code compacts long sessions, so a session whose early turns were summarized
away undercounts. The `description` column is the label written at spawn time, which is what
the subagent was asked in one line.

## What the transcripts show

Sessions and turns. Ed's turns are short: 132 of 146 under 200 characters. The long ones are
the handoff prompts a prior session wrote for him. Thirteen ask for that prompt or to clear
context, thirty-two say merge, thirteen ask for the review, six correct the voice (twice on
2026-09-02 about litotes and irony, after which the rule went to memory and stayed), ten ask
about credits.

Tools. Bash is 3,254 of 5,107 tool calls. The orchestrator edits one file in five days;
subagents make 681 edits. The Linear writes (67 `save_issue`, 47 `save_comment`) are issues
moving to In Progress and Done with results comments, after Ed says merge.

Shell shapes and families. Reading files through the shell leads, and 707 inline Python
programs (`python3 -`, `python3 -c`, `uv run python -`, `uv run python -c`) are throwaway
analysis: 314 commands read a `grade.json` or `report.json` and 125 read a tool log, the
compare and cell scripts retyped in each session's scratchpad. Thirty-three commands check for
a running process. The 121 commands touching `.env` are reads and greps for a name. 103
commits; the check for keys was by eye. The 20 emits and 14 column moves are the pass shape.

Spawns. Eighteen sonnet implementers, six opus, fourteen fable reviewers, fourteen inheriting
the orchestrator's model (how reviews and closeouts were spawned from 2026-09-05). Twenty-four
of the 52 are a review by their label. Prompts run from 1,930 to 17,823 characters, each
written fresh; the `review` and `issue-start` skills are those briefs written once.

Denials. Nine blocks by the auto mode classifier, one rejection by Ed, four interrupts. The
allowlist in `.claude/settings.json` is chosen from the shapes that only read.

Memory. Nineteen files, 7,526 words at the time of the run. A new session reads the handoff
and the process file first. The two `feedback-` files are Ed's corrections that stuck: the
process, and the voice.

## What `.claude/` now carries

Skills, each with a paragraph on what it replaces in its own `SKILL.md`: `issue-start` (the
opening every session re-derived), `pass-run` (the six scratchpad pass scripts, with the
branch, ingest, and running-emit checks in front and the four-level park), `compare` and
`column-compare` (`evals/compare.py`, the paired-cell comparison and how to read it), `cell`
and `cell-read` (`evals/cell.py`, one cell's grade, validation, hypotheses, and tool log),
`review` (the Fable adversarial brief with its two lenses).

Hooks under `.claude/hooks/`, each tested in `tests/test_hooks.py` with stdin JSON:

- `block_secrets.py` (PreToolUse): a commit whose diff adds a key-shaped line, or a `git add`
  of `.env` or `.claude.json`, is refused with the file and pattern named; it follows `cd`
  and `git -C` to read the diff where the commit runs.
- `block_double_emit.py` (PreToolUse): an emit, or a `*pass*.sh` launch, while `evals.run` or
  `gen.emit` is running is refused, naming the 4,000 events per second cap.
- `block_parked_column.py` (PreToolUse): `mkdir`, `mv`, and `cp` are simulated in order and
  an end state with cells at three levels under any name other than the ones `evals/run.py`
  writes is refused, since the report merges such cells into the live column whose `config`
  they carry.
- `lint_after_commit.py` (PostToolUse): `make lint` runs in the cwd's repository after a
  commit and a red result comes back as a warning.

Permissions: the shapes that only read (`git status`, `git log`, `git diff`, `git branch
--show-current`, `ls`, pytest, `ruff check`, `ruff format --check`, `make test`, `make lint`,
`gh pr view`, `gh pr checks`), the compare and cell modules, `evals.report` (which writes the
generated `evals/report.md`), and the Linear read tools. Nothing that writes to GitHub,
Linear, or Honeycomb.

## What the hooks would have caught

Two incidents are on record. EDW-1368's Context lists a parked column at three levels counted
as live, and the 2026-09-06 handoff carries the rule as a gotcha: four levels. The mechanism
is in `evals/report.py`, which globs `*/*/*/grade.json` and keys cells by the `config` and
`provider` fields inside each `grade.json`, whatever the directory is called, so a three-level
column under any name adds its cells to the live column whose `config` they carry and n
doubles. `block_parked_column.py` refuses that `mv` and prints the four-level shape. The same
Context lists two emits run at once, and the handoff memory carries the rule never to run
overlapping emits. `block_double_emit.py` refuses an emit while another pass's `evals.run` is
alive. A false block costs one retry in the shape the message asks for; each hook exits 0 on
anything it cannot judge, so the failure mode is a missed block, never a stuck session.

## Left out

A test run before every commit: 228 pytest runs show the tests are run, and making 103
commits each wait on the suite is the wrong trade, so lint warns after instead. A Linear
closeout skill: the 114 Linear writes follow Ed saying merge, and that order belongs to a
person. A browser skill: `browser-harness` exists and was called 83 times.

## Tables

<!-- usage_stats: begin -->
## Sessions

16 orchestrator sessions and 54 subagent sessions, 2026-09-02 to 2026-09-06. Bad JSON lines skipped: 0.

| session | date | assistant | user | user<200 | interrupts | tools | bash | spawns |
|---|---|---|---|---|---|---|---|---|
| 04222f63 | 2026-09-03 | 83 | 11 | 9 | 3 | 40 | 24 | 0 |
| 05cc3625 | 2026-09-02 | 172 | 20 | 19 | 0 | 100 | 50 | 4 |
| 0ab91e3f | 2026-09-03 | 159 | 2 | 1 | 0 | 78 | 53 | 2 |
| 3086421e | 2026-09-03 | 3 | 1 | 1 | 0 | 1 | 1 | 0 |
| 3c8f8367 | 2026-09-03 | 179 | 7 | 4 | 0 | 87 | 65 | 2 |
| 4337e238 | 2026-09-06 | 260 | 6 | 6 | 0 | 109 | 83 | 5 |
| 4870d227 | 2026-09-04 | 18 | 2 | 2 | 0 | 10 | 4 | 0 |
| 554cce3b | 2026-09-04 | 210 | 9 | 8 | 0 | 116 | 84 | 2 |
| 6063c8a3 | 2026-09-05 | 310 | 10 | 9 | 0 | 100 | 71 | 2 |
| 686ba5bf | 2026-09-03 | 91 | 5 | 4 | 1 | 54 | 26 | 0 |
| 6d0b3034 | 2026-09-06 | 178 | 11 | 11 | 0 | 71 | 59 | 3 |
| 8d172514 | 2026-09-05 | 244 | 8 | 7 | 0 | 105 | 70 | 5 |
| 93b4b4e7 | 2026-09-03 | 333 | 11 | 10 | 0 | 199 | 133 | 6 |
| e00f6837 | 2026-09-05 | 341 | 7 | 6 | 0 | 118 | 95 | 5 |
| f2a8157d | 2026-09-06 | 204 | 10 | 10 | 0 | 87 | 55 | 5 |
| fd670db6 | 2026-09-04 | 768 | 26 | 25 | 0 | 331 | 229 | 11 |

Orchestrator user turns: 146, of which 132 under 200 characters; 4 interrupts.

## User turns by family

| family (orchestrator turns matching) | count |
|---|---|
| asks for a handoff prompt or to clear context | 13 |
| asks what is next | 5 |
| says merge | 32 |
| asks for an adversarial review | 13 |
| corrects the voice | 6 |
| asks about credits or budget | 10 |
| names the browser harness | 9 |

## Tool calls by tool name

| tool | orchestrator | subagent | all |
|---|---|---|---|
| Bash | 1102 | 2152 | 3254 |
| Edit | 1 | 680 | 681 |
| Read | 52 | 516 | 568 |
| Write | 27 | 82 | 109 |
| ToolSearch | 57 | 16 | 73 |
| mcp__linear__save_issue | 66 | 1 | 67 |
| WebFetch | 28 | 36 | 64 |
| Agent | 52 | 2 | 54 |
| mcp__linear__save_comment | 46 | 1 | 47 |
| mcp__linear__get_issue | 33 | 10 | 43 |
| Monitor | 28 | 0 | 28 |
| SendMessage | 26 | 0 | 26 |
| TaskStop | 13 | 1 | 14 |
| mcp__linear__list_comments | 10 | 1 | 11 |
| mcp__linear__list_issues | 11 | 0 | 11 |
| WebSearch | 8 | 0 | 8 |
| Skill | 5 | 2 | 7 |
| mcp__claude-in-chrome__computer | 6 | 0 | 6 |
| PushNotification | 4 | 0 | 4 |
| mcp__claude-in-chrome__navigate | 4 | 0 | 4 |
| mcp__claude_ai_Gmail__search_threads | 4 | 0 | 4 |
| mcp__claude-in-chrome__get_page_text | 3 | 0 | 3 |
| mcp__claude_ai_Gmail__get_thread | 3 | 0 | 3 |
| AskUserQuestion | 2 | 0 | 2 |
| mcp__claude_ai_Google_Calendar__search_events | 2 | 0 | 2 |
| mcp__linear__create_attachment_from_upload | 2 | 0 | 2 |
| mcp__linear__list_issue_labels | 2 | 0 | 2 |
| mcp__linear__list_issue_statuses | 2 | 0 | 2 |
| mcp__linear__prepare_attachment_upload | 2 | 0 | 2 |
| mcp__claude-in-chrome__find | 1 | 0 | 1 |
| mcp__claude-in-chrome__tabs_close_mcp | 1 | 0 | 1 |
| mcp__claude-in-chrome__tabs_context_mcp | 1 | 0 | 1 |
| total | 1606 | 3501 | 5107 |

## Shell commands by shape

| shape | orchestrator | subagent | all |
|---|---|---|---|
| grep | 621 | 1014 | 1635 |
| echo | 437 | 895 | 1332 |
| head | 487 | 546 | 1033 |
| tail | 243 | 554 | 797 |
| cd | 397 | 352 | 749 |
| sed | 279 | 374 | 653 |
| cat | 197 | 400 | 597 |
| ls | 149 | 181 | 330 |
| python3 - | 182 | 113 | 295 |
| git diff | 77 | 172 | 249 |
| uv run pytest | 35 | 193 | 228 |
| git status | 86 | 130 | 216 |
| git log | 114 | 85 | 199 |
| python3 -c | 81 | 105 | 186 |
| uv run python - | 93 | 77 | 170 |
| make lint | 42 | 112 | 154 |
| for | 43 | 72 | 115 |
| make test | 25 | 80 | 105 |
| wc | 51 | 54 | 105 |
| cut | 74 | 16 | 90 |
| uv run ruff format | 9 | 80 | 89 |
| git show | 14 | 71 | 85 |
| browser-harness | 83 | 0 | 83 |
| uv run ruff check | 13 | 70 | 83 |
| git branch | 39 | 31 | 70 |
| git add | 34 | 35 | 69 |
| git checkout | 52 | 14 | 66 |
| sleep | 61 | 5 | 66 |
| gh pr view | 41 | 20 | 61 |
| uv run python -c | 6 | 50 | 56 |
| git commit | 36 | 16 | 52 |
| find | 13 | 38 | 51 |
| all segments | 4751 | 6502 | 11253 |

## Shell activity by family

| family (commands matching) | orchestrator | subagent | all |
|---|---|---|---|
| eval pass with --emit | 16 | 4 | 20 |
| eval pass without --emit | 48 | 20 | 68 |
| render the report | 39 | 20 | 59 |
| park or move a results column | 11 | 3 | 14 |
| read grade.json or report.json by hand | 184 | 130 | 314 |
| read a tool_log by hand | 69 | 56 | 125 |
| check for a running process | 30 | 3 | 33 |
| read the gen/runs manifests | 41 | 41 | 82 |
| branch for an issue | 8 | 9 | 17 |
| commit | 61 | 42 | 103 |
| run tests | 137 | 302 | 439 |
| run lint | 123 | 220 | 343 |
| touch .env | 58 | 63 | 121 |
| gh pr | 137 | 21 | 158 |

## Subagent spawns

52 Agent calls. By model: sonnet 18, fable 14, inherit 14, opus 6. By type: general-purpose 43, default 9.

| session | description | model | type | prompt chars |
|---|---|---|---|---|
| 05cc3625 | Implement R2 (EDW-1324) | sonnet | general-purpose | 1930 |
| 05cc3625 | Adversarial review of PR #1 | fable | general-purpose | 2413 |
| 05cc3625 | Implement R3 (EDW-1325) MCP client | sonnet | general-purpose | 4381 |
| 05cc3625 | Adversarial review of PR #2 | fable | general-purpose | 3568 |
| 0ab91e3f | Implement R9 telemetry (sonnet) | sonnet | general-purpose | 10637 |
| 0ab91e3f | Adversarial review of R9 (fable) | fable | general-purpose | 4934 |
| 3c8f8367 | Adversarial review of R8 | fable | general-purpose | 4427 |
| 3c8f8367 | Rename joymath to edwin-knuth | sonnet | general-purpose | 2321 |
| 4337e238 | Implement NVIDIA NIM provider | sonnet | default | 7075 |
| 4337e238 | Adversarial review of NVIDIA provider | inherit | general-purpose | 4153 |
| 4337e238 | Review partially_checked prose vs log | inherit | general-purpose | 2524 |
| 4337e238 | Adversarial code review of PRs 22 and 23 | inherit | general-purpose | 3825 |
| 4337e238 | Fix review findings on r18 | sonnet | default | 6789 |
| 554cce3b | Implement EDW-1361 and EDW-1362 | sonnet | general-purpose | 17823 |
| 554cce3b | Adversarial review of both fixes | fable | general-purpose | 8073 |
| 6063c8a3 | Implement EDW-1363 series parsing | sonnet | default | 7025 |
| 6063c8a3 | Adversarial review of EDW-1363 | fable | default | 3849 |
| 6d0b3034 | Close out EDW-1337 after merge | inherit | general-purpose | 3406 |
| 6d0b3034 | Combined two-provider eval report | inherit | general-purpose | 3943 |
| 6d0b3034 | README results section from report | inherit | general-purpose | 8046 |
| 8d172514 | Implement EDW-1365 partially_checked | sonnet | default | 7926 |
| 8d172514 | Adversarial review of EDW-1365 after-pass logs | inherit | default | 5620 |
| 8d172514 | Implement EDW-1367 checkable readings | sonnet | default | 8761 |
| 8d172514 | Implement EDW-1337 Ollama provider | sonnet | default | 6099 |
| 8d172514 | Report columns keyed by provider | sonnet | default | 3620 |
| 93b4b4e7 | Build R4 generator on r4-generator | opus | general-purpose | 9122 |
| 93b4b4e7 | Adversarial review of PR #3 | fable | general-purpose | 4389 |
| 93b4b4e7 | Assess Receipts story and video prep | fable | general-purpose | 4438 |
| 93b4b4e7 | Build R6 investigator on r6-investigator | opus | general-purpose | 11197 |
| 93b4b4e7 | Adversarial review of PR #4 | fable | general-purpose | 6206 |
| 93b4b4e7 | Adversarial review of PR #4 | opus | general-purpose | 6518 |
| e00f6837 | Implement EDW-1364 prompt step | sonnet | general-purpose | 7305 |
| e00f6837 | Adversarial review EDW-1364 prompt | inherit | general-purpose | 3606 |
| e00f6837 | Implement EDW-1332 resume, delta, spend | sonnet | general-purpose | 5439 |
| e00f6837 | Adversarial review PR #19 code | inherit | general-purpose | 3349 |
| e00f6837 | Adversarial review PR #20 final | inherit | general-purpose | 3965 |
| f2a8157d | Implement EDW-1366 noise floor step | sonnet | general-purpose | 8285 |
| f2a8157d | Adversarial review of EDW-1366 diff | inherit | general-purpose | 7019 |
| f2a8157d | Fable adversarial review of PR #28 | inherit | general-purpose | 5663 |
| f2a8157d | EDW-1368 Claude Code tooling for Receipts | inherit | general-purpose | 10181 |
| f2a8157d | Fable adversarial review of EDW-1368 | inherit | general-purpose | 8828 |
| fd670db6 | R5 implementer: six scenarios | sonnet | general-purpose | 12499 |
| fd670db6 | R5 adversarial review (Fable) | fable | general-purpose | 4785 |
| fd670db6 | Three pre-R10 fixes implementer | sonnet | general-purpose | 7432 |
| fd670db6 | Adversarial review: three pre-R10 fixes | fable | general-purpose | 2752 |
| fd670db6 | Fix run_bubbleup dataset guard | sonnet | general-purpose | 4040 |
| fd670db6 | Adversarial review: BubbleUp guard fix | fable | general-purpose | 3183 |
| fd670db6 | EDW-1359 implementer (Opus) | opus | general-purpose | 5849 |
| fd670db6 | Adversarial review: EDW-1359 grader | fable | general-purpose | 2615 |
| fd670db6 | EDW-1360 implementer (Opus) | opus | general-purpose | 4652 |
| fd670db6 | Adversarial review: EDW-1360 prompt | fable | general-purpose | 3378 |
| fd670db6 | Onset bracket wording (worktree) | opus | general-purpose | 2814 |

## Permission denials

| kind | count |
|---|---|
| automode-blocked | 9 |
| denial text in a tool result | 9 |
| user-rejected | 1 |

Approved permission prompts leave no record in the transcript and are not counted.

## Memory files

19 files, 7526 words. Read at run time, not scoped by --before.
<!-- usage_stats: end -->
