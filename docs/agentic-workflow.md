# How Receipts is built with Claude Code

Receipts has been built with Claude Code since 2026-09-02. Fable orchestrates. The model an
issue is labeled for implements in a subagent. A Fable subagent reviews the result for
correctness and for whether it helps the Honeycomb case. The orchestrator does the browser
work. This note reports what the transcripts show the work asking for over and over, and what
`.claude/` now carries so those steps stop being re-derived by hand.

Every number here is printed by `tools/usage_stats.py` from the transcripts under
`~/.claude/projects/-Users-eknuth-proj-receipts/`, run on 2026-09-06. The script prints
counts, tool names, normalized command shapes, and the label an `Agent` call was given, and
no conversation text; a test pins that its output has no line over 120 characters and no
key-shaped substring.

## Method

One JSONL line per event; a tool call is a `tool_use` block in an assistant message.
Orchestrator sessions are the top-level files and each subagent has its own file under
`<session>/subagents/`. A shell command is tokenized with `shlex`, split at `&&`, `||`, `;`,
`|`, and newlines, cut at a heredoc or an inline program, and each piece reduced to a shape:
the first token, plus the subcommand for `git`, `gh`, `make`, and `uv run`, and the module
for `python -m`; paths become `<path>`. Families match a regex against the whole command.
User turns are Ed's text turns, with injected notifications excluded and interrupts counted
apart. Denials are the `toolDenialKind` field plus tool results that begin "Permission for
this action was denied"; the nine of each are the same nine events. Approved prompts leave no
record and are not counted.

Caveats. Claude Code compacts long sessions, so a session whose early turns were summarized
away undercounts. The session this ran in (`f2a8157d`) was live, so its row grows on a rerun.
The `description` column is the label written at spawn time, which is what the subagent was
asked in one line.

## What the transcripts show

## Sessions

17 orchestrator sessions and 52 subagent sessions, 2026-09-02 to 2026-09-06. Bad JSON lines skipped: 0.

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
| 9232bcc2 | 2026-09-04 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 93b4b4e7 | 2026-09-03 | 333 | 11 | 10 | 0 | 199 | 133 | 6 |
| e00f6837 | 2026-09-05 | 341 | 7 | 6 | 0 | 118 | 95 | 5 |
| f2a8157d | 2026-09-06 | 172 | 6 | 6 | 0 | 74 | 48 | 4 |
| fd670db6 | 2026-09-04 | 768 | 26 | 25 | 0 | 331 | 229 | 11 |

Orchestrator user turns: 142, of which 128 under 200 characters; 4 interrupts.

## User turns by family

| family (orchestrator turns matching) | count |
|---|---|
| asks for a handoff prompt or to clear context | 13 |
| asks what is next | 5 |
| says merge | 31 |
| asks for an adversarial review | 13 |
| corrects the voice | 6 |
| asks about credits or budget | 10 |
| names the browser harness | 9 |

Ed's turns are short: 128 of 142 under 200 characters. The long ones are the handoff prompts a
prior session wrote for him. Thirteen ask for that prompt or to clear context, thirty-one say
merge, thirteen ask for the review, six correct the voice (twice with "no litotes or irony" on
2026-09-02, after which the rule went to memory and stayed), ten ask about credits.

## Tool calls by tool name

| tool | orchestrator | subagent | all |
|---|---|---|---|
| Bash | 1095 | 2099 | 3194 |
| Edit | 1 | 651 | 652 |
| Read | 52 | 511 | 563 |
| Write | 27 | 55 | 82 |
| ToolSearch | 57 | 15 | 72 |
| mcp__linear__save_issue | 65 | 1 | 66 |
| WebFetch | 28 | 25 | 53 |
| Agent | 51 | 1 | 52 |
| mcp__linear__save_comment | 45 | 1 | 46 |
| mcp__linear__get_issue | 33 | 10 | 43 |
| Monitor | 28 | 0 | 28 |
| SendMessage | 24 | 0 | 24 |
| TaskStop | 12 | 1 | 13 |
| mcp__linear__list_comments | 10 | 1 | 11 |
| mcp__linear__list_issues | 11 | 0 | 11 |
| WebSearch | 8 | 0 | 8 |
| Skill | 5 | 2 | 7 |
| mcp__claude-in-chrome__computer | 6 | 0 | 6 |
| mcp__claude-in-chrome__navigate | 4 | 0 | 4 |
| PushNotification | 4 | 0 | 4 |
| mcp__claude_ai_Gmail__search_threads | 4 | 0 | 4 |
| mcp__claude-in-chrome__get_page_text | 3 | 0 | 3 |
| mcp__claude_ai_Gmail__get_thread | 3 | 0 | 3 |
| AskUserQuestion | 2 | 0 | 2 |
| mcp__linear__list_issue_labels | 2 | 0 | 2 |
| mcp__linear__prepare_attachment_upload | 2 | 0 | 2 |
| mcp__linear__list_issue_statuses | 2 | 0 | 2 |
| mcp__linear__create_attachment_from_upload | 2 | 0 | 2 |
| mcp__claude_ai_Google_Calendar__search_events | 2 | 0 | 2 |
| mcp__claude-in-chrome__tabs_close_mcp | 1 | 0 | 1 |
| mcp__claude-in-chrome__find | 1 | 0 | 1 |
| mcp__claude_ai_Gmail__create_draft | 1 | 0 | 1 |
| total | 1593 | 3374 | 4967 |

Bash is 3,194 of 4,967 tool calls. The orchestrator edits one file in five days; subagents
make 651 edits. The Linear writes (66 `save_issue`, 46 `save_comment`) are issues moving to
In Progress and Done with results comments, after Ed says merge.

## Shell commands by shape

| shape | orchestrator | subagent | all |
|---|---|---|---|
| grep | 618 | 977 | 1595 |
| echo | 432 | 819 | 1251 |
| head | 482 | 520 | 1002 |
| tail | 236 | 541 | 777 |
| cd | 393 | 331 | 724 |
| sed | 279 | 359 | 638 |
| cat | 196 | 376 | 572 |
| ls | 149 | 170 | 319 |
| python3 - | 180 | 113 | 293 |
| git diff | 72 | 168 | 240 |
| uv run pytest | 35 | 186 | 221 |
| git status | 83 | 126 | 209 |
| git log | 109 | 81 | 190 |
| python3 -c | 81 | 98 | 179 |
| uv run python - | 93 | 77 | 170 |
| make lint | 41 | 109 | 150 |
| for | 42 | 68 | 110 |
| make test | 24 | 77 | 101 |
| wc | 51 | 48 | 99 |
| cut | 74 | 16 | 90 |
| git show | 14 | 71 | 85 |
| uv run ruff format | 9 | 75 | 84 |
| browser-harness | 83 | 0 | 83 |
| uv run ruff check | 13 | 64 | 77 |
| git branch | 37 | 30 | 67 |
| sleep | 61 | 5 | 66 |
| git add | 34 | 31 | 65 |
| git checkout | 51 | 11 | 62 |
| gh pr view | 41 | 20 | 61 |
| uv run python -c | 6 | 50 | 56 |
| find | 13 | 38 | 51 |
| git commit | 36 | 14 | 50 |
| all segments | 4699 | 6175 | 10874 |

## Shell activity by family

| family (commands matching) | orchestrator | subagent | all |
|---|---|---|---|
| eval pass with --emit | 16 | 0 | 16 |
| eval pass without --emit | 48 | 18 | 66 |
| render the report | 39 | 20 | 59 |
| park or move a results column | 11 | 1 | 12 |
| read grade.json or report.json by hand | 184 | 130 | 314 |
| read a tool_log by hand | 69 | 56 | 125 |
| check for a running process | 30 | 0 | 30 |
| read the gen/runs manifests | 41 | 38 | 79 |
| branch for an issue | 8 | 9 | 17 |
| commit | 61 | 35 | 96 |
| run tests | 136 | 292 | 428 |
| run lint | 122 | 210 | 332 |
| touch .env | 58 | 61 | 119 |
| gh pr | 136 | 21 | 157 |

Reading files through the shell leads, and 642 inline Python programs (`python3 -`,
`python3 -c`, `uv run python -`, `uv run python -c`) are throwaway analysis: 314 commands
read a `grade.json` or `report.json` and 125 read a tool log, the compare and cell scripts
retyped in each session's scratchpad. Thirty `pgrep` checks precede an emit. The 119
commands touching `.env` are reads and greps for a name; the 96 commits carried no key,
checked by reading the diff each time. The 16 emits and 12 column moves are the pass shape.

## Subagent spawns

51 Agent calls. By model: sonnet 18, fable 14, inherit 13, opus 6. By type: general-purpose 42, default 9.

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

Eighteen sonnet implementers, six opus, fourteen fable reviewers, thirteen inheriting the
orchestrator's model (how reviews and closeouts were spawned from 2026-09-05). Twenty-three
of the 51 are a review by their label. Prompts run from 1,930 to 17,823 characters, each
written fresh; the `review` and `issue-start` skills are those briefs written once.

## Permission denials

| kind | count |
|---|---|
| automode-blocked | 9 |
| denial text in a tool result | 9 |
| user-rejected | 1 |

Approved permission prompts leave no record in the transcript and are not counted.

Nine blocks by the auto mode classifier, one rejection by Ed, four interrupts. The allowlist
in `.claude/settings.json` is chosen from the shapes above that only read.

## Memory files

| file | words |
|---|---|
| MEMORY.md | 471 |
| feedback-issue-process.md | 337 |
| feedback-no-litotes-irony.md | 170 |
| handoff-2026-09-06.md | 609 |
| project-agent-timeline-facts.md | 334 |
| project-answer-leaks-before-r6.md | 220 |
| project-bubbleup-group-selection-facts.md | 280 |
| project-control-failure-punish-not-prompt.md | 303 |
| project-first-ten-scenario-pass.md | 950 |
| project-honeycomb-findings-ticket.md | 163 |
| project-honeycomb-team-slug.md | 185 |
| project-katie-leonard-honeycomb-backchannel.md | 132 |
| project-mcp-time-series-not-seen.md | 475 |
| project-noise-floor-step.md | 526 |
| project-nvidia-nim-provider.md | 514 |
| project-payments-population-step.md | 507 |
| project-r5-live-facts.md | 387 |
| project-rule-two-partially-checked.md | 450 |
| project-trigger-facts.md | 249 |
| total | 7262 |

Nineteen files, 7,262 words. A new session reads the handoff and the process file first.
The two `feedback-` files are Ed's corrections that stuck: the process, and the voice.

## What `.claude/` now carries

Skills, each with a paragraph on what it replaces in its own `SKILL.md`:

- `issue-start`: the opening every session re-derived (CLAUDE.md, handoff, issue, R number,
  branch, implementer on the labeled model).
- `pass-run`: the six scratchpad pass scripts, with the branch, ingest, and running-emit
  checks in front and the four-level park.
- `column-compare`: `evals/compare.py`, the paired-cell comparison, and how to read it.
- `cell-read`: `evals/cell.py`, one cell's grade, validation, hypotheses, and tool log.
- `review`: the Fable adversarial review brief with its two lenses.

Commands: `/cell <col> <scenario> <n>` and `/compare <before> <after>` run the two modules.

Hooks under `.claude/hooks/`, each tested in `tests/test_hooks.py` with stdin JSON:

- `block_secrets.py` (PreToolUse, Bash): a commit whose diff adds a key-shaped line, or a
  `git add` of `.env` or `.claude.json`, is refused with the file and pattern named.
- `block_double_emit.py` (PreToolUse, Bash): an emit while `evals.run` or `gen.emit` is
  running is refused, naming the 4,000 events per second cap.
- `block_parked_column.py` (PreToolUse, Bash): an `mv` or `cp` that would leave cells at
  three levels under `evals/results/` under a non-config name is refused.
- `lint_after_commit.py` (PostToolUse, Bash): after a commit, `make lint` runs and a red
  result comes back as a warning.

Permissions: the read-only shapes above (`git status`, `git log`, `git diff`, `ls`, pytest,
ruff, `make test`, `make lint`, `gh pr view`, `gh pr checks`, the report, compare, and cell
modules) and the Linear read tools. Nothing that writes to GitHub, Linear, or Honeycomb.

Left out. A test run before every commit: 221 pytest runs show the tests are run, and making
96 commits each wait on the suite is the wrong trade, so lint warns after instead. A Linear
closeout skill: the 112 Linear writes follow Ed saying merge, and that order belongs to a
person. A browser skill: `browser-harness` exists and was called 83 times.
