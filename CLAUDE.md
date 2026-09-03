# Receipts

Portfolio demo for the Honeycomb "Agentic Intelligence" role. An investigation agent that works
against the hosted Honeycomb MCP and must show its evidence, plus an eval harness with ground
truth that grades outcomes, not process. Target: a working vertical slice by 2026-09-08, full
build by 2026-09-09.

Work is tracked in Linear, team EDW, project "Receipts", issues EDW-1323 through EDW-1336
(R1 to R14) plus R15 (Ollama provider). Each issue carries Context, Spec, Acceptance criteria, and Out of scope. Read the
issue before starting it. Use the Linear MCP; the `linear` CLI is the fallback.

Vault note: `~/proj/edwin-knuth/Projects/Honeycomb Demo - Receipts.md`. Full plan:
`~/.claude/plans/i-need-toake-a-enumerated-scone.md`.

## Rules

- Secrets come only from `.env` (gitignored). Never write a key into code, a commit, a fixture,
  `~/.claude.json`, or a Linear comment. `.env.example` lists the names.
- Do not tune the agent to the scenarios. If the agent fails a scenario honestly, fix the method,
  not the prompt for that case. A failure is a finding for the README.
- Read tools only against the Honeycomb MCP until R12 (Canvas and boards). The allowlist lives in
  `agent/mcp_client.py`.
- Pacing: at most 40 MCP calls per minute with 1.5 s spacing. The hosted limit is 50/min. Calls
  over the cap sleep, they do not fail.
- Prose (README, report, docstrings that read as prose) is in Ed's voice: plain sentences, no
  em dashes at all, no rule-of-three flourishes, no "delve", no hedging boilerplate.
- One issue per branch, branch named `r<N>-<slug>`. Commit as Edwin Knuth <eknuth@gmail.com>.
- Python 3.12, `uv`. Tests with pytest. Lint with ruff. `make test` and `make lint` must pass
  before an issue is called done.
- Anything that needs a browser (Honeycomb UI, trigger creation, Canvas) is Ed's, not a
  subagent's. Stop and say what is needed.
- Honeycomb bugs and docs gaps go to Linear issue EDW-1338 as a row (date, area, finding,
  verified how, action). Report them to Ed; do not edit Linear yourself.

## Layout

```
receipts/
  gen/            fault-injectable OTLP trace generator (the "production" system)
    scenarios/*.yml   scripted incidents with ground truth
    topology.py       4 services: gateway -> checkout -> payments -> inventory-db
    emit.py           baseline + fault traffic to Honeycomb via OTLP/HTTP
    verify.py         asserts the injected fault is visible via a Honeycomb query
  agent/          the investigator
    providers/    anthropic.py, bedrock.py, ollama.py (same interface: messages + tool use)
    mcp_client.py hosted Honeycomb MCP over streamable HTTP, Bearer key, read tools only
    loop.py       orient -> characterize -> bubbleup -> trace -> verify-by-negation -> report
    report.py     pydantic Report schema
    telemetry.py  gen_ai.* spans for the agent's own loop -> same Honeycomb env
  evals/
    grader.py     outcome scoring against scenario ground truth
    run.py        N scenarios x M configs x K repeats, JSON + markdown report
    report.md     generated
```

Packages: `anthropic`, `boto3` (R11 only), `mcp` (streamable HTTP client), `opentelemetry-sdk`,
`opentelemetry-exporter-otlp-proto-http`, `pydantic`, `pyyaml`, `rich`, `python-dotenv`.

## Honeycomb facts

- Environment `receipts-demo`, dataset `receipts-shop`. OTLP/HTTP to `https://api.honeycomb.io`
  with header `x-honeycomb-team: <ingest key>`.
- Hosted MCP: `https://mcp.honeycomb.io/mcp`, streamable HTTP, `Authorization: Bearer
  <key_id:secret>` using the management v2 key. Read tools used: `get_workspace_context`,
  `get_dataset_columns`, `find_columns`, `find_queries`, `run_query`, `get_query_results`,
  `run_bubbleup`, `get_trace`, `get_slos`, `get_triggers`. Write tools (R12 only):
  `create_board`, `canvas_agent_invoke`, `canvas_agent_poll_response`.
- `mcp` is pinned to 2.x. The 2.x client entrypoint is
  `mcp.client.streamable_http.streamable_http_client` (1.x called it `streamablehttp_client`);
  Honeycomb's published snippets use the 1.x name.
- Send `traceparent` in MCP `params._meta` on every call. Honeycomb does not document this
  field; R9 (self-telemetry, EDW-1331) checks whether MCP-side spans link to ours in the Agent
  Timeline. Until then it is an assumption, and the wire format is proven by test.
- Agent Timeline groups spans by `gen_ai.conversation.id`. Free tier: 20M events/month.
- Method to follow, from `honeycombio/agent-skill` (`honeycomb-investigator` agent and
  `production-investigation` skill): Orient, Characterize, BubbleUp, Traces, Verify by negation
  (`WHERE NOT <finding>`), Record. Discover columns before assuming names. `find_queries` first.
  Combine calculations in one query (`COUNT, P99(duration_ms), HEATMAP(duration_ms)`).

## Generator (`gen/`)

Synthetic, deterministic, ground truth is a file. Root spans carry high-cardinality attributes so
BubbleUp has something to find: `customer.id` (a few thousand values), `deployment.version`,
`cloud.region`, `payment.provider`, `cart.size`, `http.route`, `db.statement` hash, `error`,
`duration_ms`. Child spans per service so `get_trace` shows where time goes. Every span carries
`scenario.run_id`; the agent is told the run id and scopes every query to it. `scenario.id` is
not on the wire: its values read as answers, so an agent that broke down on that column would be
handed the root cause and whether there is an incident at all. The run manifest maps run id to
scenario id, which is where the grader reads it.

Timestamps: try backdated (Honeycomb accepts the recent past) so a 30-minute scenario emits in
seconds; fall back to real time if rejected. Volume: 15 rps x 30 min x ~5 spans is about 135k
spans per scenario, tiny against the free tier.

Scenario YAML:

```yaml
id: payments-stripe-v251-uswest
narrative: "v2.5.1 rolled to us-west-2; stripe calls in payments gained ~800ms"
baseline: {rps: 15, minutes: 20}
fault:
  onset_min: 10
  where: {deployment.version: "2.5.1", cloud.region: "us-west-2", payment.provider: "stripe"}
  effect: {span: "payments.charge", latency_add_ms: 800, error_rate: 0.0}
ground_truth:
  incident_present: true
  root_cause_dims: {deployment.version: "2.5.1", cloud.region: "us-west-2", payment.provider: "stripe"}
  slow_or_failing_span: payments.charge
  affected_share: 0.12
red_herrings:
  - where: {cloud.region: "eu-west-1"}
    effect: {span: "db.query", latency_add_ms: 150}
    onset_min: 0
    note: "pre-existing, not the incident"
```

Field-by-field documentation is in `gen/README.md`. One dataset (`receipts-shop`), so the resource
carries `service.name = receipts-shop` and each span names its real service in `service.component`.
Backdating works. The team's ingest limit is 4,000 events per second. Going over it returns HTTP
200 with an empty `partial_success` and the spans are dropped; the only notice is an email at most
once per 24 hours. So `gen/emit.py` posts from one connection at a cap of 2,500 spans per second,
never run two emits at once, and `gen/verify.py` counts what arrived before it believes any other
number. The hosted MCP truncates query time bounds to whole seconds, so window edges sit on whole
seconds.

Scenario classes: latency spike, error surge, deployment regression, dependency failure, trigger
fired, two controls (no incident, the agent should say so), two where a red herring is stronger
in count than the true cause.

## Agent (`agent/`)

Two rules that are the point of the project:

1. **Receipts rule.** A hypothesis may only be reported if it carries at least one `query_id`
   from `run_query` with the rows that support it, plus a negation query (`WHERE NOT finding`)
   that was actually run. Enforced in the prompt and validated in code against the tool log.
2. **Not-checked list.** The report enumerates dimensions and services that were in scope but
   not queried. Every entry must be absent from the tool log.

Report schema (`agent/report.py`):

```python
class Evidence(BaseModel):
    query_id: str
    summary: str
    permalink: str | None

class Hypothesis(BaseModel):
    claim: str
    dims: dict[str, str]
    slow_or_failing_span: str | None
    confidence: Literal["high", "medium", "low"]
    evidence: list[Evidence]
    negation: Evidence | None

class Report(BaseModel):
    incident_present: bool
    hypotheses: list[Hypothesis]   # ranked
    affected_population: str | None
    onset_estimate: str | None
    not_checked: list[str]
    tool_calls: int
    tokens_in: int
    tokens_out: int
    wall_s: float
```

Providers share one interface (messages in, tool use out). `anthropic.py` first, default model
from `ANTHROPIC_MODEL` (`claude-sonnet-4-5` in the plan; `claude-sonnet-5` also verified working and
cheaper). The Anthropic key is identity-linked, so every request must carry the header
`anthropic-workspace-id: $ANTHROPIC_WORKSPACE_ID` (pass `default_headers` to the SDK client). `bedrock.py` uses `boto3` `converse` with tool config (R11). Budget per run:
under 40 MCP calls and 8 minutes.

Self-telemetry (`agent/telemetry.py`, R9), OTel GenAI semconv, exported to the same environment:
one `invoke_agent receipts-investigator` root span per run with `gen_ai.conversation.id = run_id`
and `gen_ai.agent.name = "receipts-investigator"`; `chat {model}` spans with `gen_ai.usage.*` and
`gen_ai.request.model`; `execute_tool {tool}` spans with `gen_ai.tool.name` and
`gen_ai.tool.call.arguments`; `gen_ai.evaluation.result` on the root span after grading.

## Grader (`evals/`)

| Component | Score |
|---|---|
| Top hypothesis dims match ground truth (Jaccard over dims) | 0 to 1, weight 0.35 |
| Correct slow or failing span named | 0/1, weight 0.15 |
| `incident_present` correct (controls) | 0/1, weight 0.15 |
| Onset within 3 minutes | 0/1, weight 0.10 |
| Every reported hypothesis carries evidence and negation | 0 to 1, weight 0.15 |
| Not-checked list non-empty and truthful against the tool log | 0/1, weight 0.10 |

Calibration penalty on the top hypothesis: high-confidence wrong -0.5, medium wrong -0.25, low
wrong -0.1, and any hypothesis with zero evidence -0.25 regardless of correctness. The required
ordering, proven by unit tests: confident-wrong < hedged-wrong < hedged-right < confident-right.
Record process metrics next to the outcome score: tool calls, tokens, cost, wall time.

Runner: `uv run evals/run.py --scenarios all --configs full,no-negation,no-notchecked --repeats 3`.
A failed run is recorded as 0 with the error, never skipped. Output `evals/report.md` with a
per-scenario table, mean and spread per config, and Honeycomb permalinks per run.

## Reuse

- `~/proj/charles/api/src/services/fact-checker.ts`: grounded/partial/unsupported vocabulary and
  the "hold until a person clears flagged claims" pattern. Port the vocabulary into confidence and
  the receipts rule.
- `~/proj/hevy-mcp/src/tools.ts`: compact-text tool result convention.
- `~/proj/charles/tools/slackbot/telemetry.py`: OTel Python setup with header auth and graceful
  no-op. Adapt to `x-honeycomb-team`.

## Build order

Linear is authoritative for the numbering: R6 is the agent (EDW-1328), R7 the grader (EDW-1329),
R8 the eval runner (EDW-1330), R9 self-telemetry (EDW-1331). Vertical slice first, then expand:
R1, R2, R3, R4 (two scenarios: one latency spike, one control), R6, R7, R8 (2 x 1 x 3), R9.
Then R5, R10, R13, R14. R15 (Ollama, `qwen3.8:27b` local, free dev runs
and a third report column), then R11 (Bedrock) and R12 (Canvas) last and droppable. If the recruiter screen lands before the grader exists, the honest line is
"generator and MCP loop work, grader is this week." Never present partial numbers.
