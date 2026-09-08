# Voice-over, technical in plain words

Same segments and start times as before. The technical names stay; each one gets a
short plain clause the first time it appears.

**0:00 title, 5 s (13 words).** An agent can explain why production broke. This one
has to prove it.

**0:05 architecture, 10 s (26 words).** Four parts. A generator that injects faults
into a fake shop. Honeycomb, over its hosted MCP. The agent. And a grader that holds
the answer key.

**0:15 emit, 45 s (87 words).** The shop is synthetic, so the root cause is written
in a file before anything runs. This scenario: version 2.5.1 rolls out to us-west-2,
and Stripe charges gain about 800 milliseconds from minute ten. There is a decoy too.
eu-west-1 has been slow on its database since minute zero, and that is old news, not
the incident. The emitter backdates twenty minutes of traffic, ninety thousand spans,
into Honeycomb in under a minute and prints a run id. That run id is all the agent is
told.

**1:00 heatmap, 12 s (23 words).** In Honeycomb, a heatmap of request duration for that
run id. The step at minute ten is what the agent has to find.

**1:12 investigation, about 58 s (117 words).** The agent runs Honeycomb's own
playbook. Orient: learn the columns. Characterize: find where the latency steps.
BubbleUp: ask which attributes the slow requests share. Then a trace, to see which span
carries the time. Every call is a read over the hosted MCP, paced under the rate limit.

> Stage direction, not read: the loop's quiet two minutes play at seven times speed for
> about twenty seconds here. Keep talking through it if the lines above run long; pick
> up the next line when the report scrolls in.

Then the report. The top hypothesis names the version, the region, and the payment
provider, plus the slow span, at high confidence. Under it, each claim cites the query
that ran, with a link. Then the negation query: everything outside that population
stayed fast. Then the not-checked list, the attributes it never queried. A claim with
no query behind it, or no negation, never makes it into the report.

**2:10 Agent Timeline, 8 s (20 words).** The agent traces its own loop, so Honeycomb
shows it as a timeline. Here it hands its report to Canvas.

**2:18 a graded run, 8 s (21 words).** Under the eval harness the grade is written onto
the run's root span. The run and its score are one record.

**2:26 tables, 16 s (40 words).** Ten scripted incidents, three repeats, two models.
Sonnet named the right cause thirty times out of thirty. The second model passed
Honeycomb's process evaluator on every run and scored under 0.5 here on twenty-five.
Grade the answer, not the steps.

**2:42 end card, 6 s (12 words).** Every hypothesis carries its receipts. A confident
wrong answer costs the most.
