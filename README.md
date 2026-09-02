# Receipts

An investigation agent for Honeycomb that has to show its work, plus an eval harness that grades
it on outcomes.

The agent follows Honeycomb's own investigation playbook (Orient, Characterize, BubbleUp, Traces,
Verify by negation, Record) against the hosted Honeycomb MCP. Every hypothesis it reports must
cite the query and the rows that support it, must carry a negation query that was actually run,
and the report must list what was in scope but not checked.

Alongside it, a fault-injectable telemetry generator emits scripted incidents into a real Honeycomb
environment, so the grader knows the true root cause, the affected population, and the onset. The
score rewards a correct, cited conclusion and punishes a confident wrong one harder than a hedged
wrong one. The agent's own loop is traced with the OpenTelemetry GenAI semantic conventions, so
each investigation renders in Honeycomb's Agent Timeline.

Status: day 0. Write-up, numbers, and how-to-run land as the build progresses.
