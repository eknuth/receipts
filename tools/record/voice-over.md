# Voice-over for the recording

Read against `tools/record/out/receipts-demo-2026-09.mp4`. Times are where each
segment starts in the silent cut; the exact figures print at the end of `make video`.
Record as one audio file (QuickTime, File, New Audio Recording, or Voice Memos),
then `uv run python tools/record/build.py --assemble --voice <file>`.

**0:00 title, 5 s (11 words).** Any agent can explain what went wrong. This one
proves it.

**0:05 architecture, 10 s (25 words).** Four pieces: a generator, a hosted MCP, a
model, and a grader. The agent queries only receipts-shop, never the scenario file
or its own telemetry.

**0:15 emit, 45 s (77 words).** The system under investigation is synthetic, so the
root cause is a file. This scenario adds about 800 milliseconds to Stripe charges on
version 2.5.1 in us-west-2, starting at minute ten. eu-west-1 has been slow on the
database the whole time, and that is not the incident. The emitter backdates twenty
minutes of traffic, ninety thousand spans, in under a minute, and prints the run id.
That run id is the only thing the agent is told.

**1:00 heatmap, 12 s (21 words).** In Honeycomb, scoped to the run id, the step is at
minute ten. That is what the agent has to find.

**1:12 investigation, about 58 s (121 words).** It follows Honeycomb's own playbook:
orient, characterize, BubbleUp, traces, verify by negation. Every call goes through a
client that only allows read tools and paces itself under the hosted limit. [the quiet
middle runs fast here] Then the report. The top hypothesis names the three dimensions
and the slow span, at high confidence. Under it, the evidence: each line is a query
that actually ran, with its permalink. Then the negation query, the same population
with the finding excluded. Then the list of what was in scope and never checked. A
hypothesis without a query id and a negation that ran is dropped by the validator
before it can be reported, and anything on the not-checked list that the tool log
shows was queried fails the report.

**2:10 Agent Timeline, 8 s (20 words).** The same run in Agent Timeline, and a second lane beside it: the investigator handing its report to Canvas, Honeycomb's own agent.

**2:18 a graded run, 8 s (20 words).** The harness writes the grade onto the run's own
root span, so a run and its score are one thing.

**2:26 tables, 16 s (38 words).** Ten scenarios, three repeats, two models. On
Sonnet, all thirty top hypotheses were right, or called no incident. The second model
passes Honeycomb's evaluator, scoring under 0.5 on twenty-five of thirty. That column
argues for grading the answer.

**2:42 end card, 6 s (12 words).** Every hypothesis cites its evidence. A confident
wrong answer costs the most.
