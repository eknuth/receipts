# gen: the synthetic shop

The "production system" the investigation agent works against. It emits OpenTelemetry traces for a
small four service shop into the Honeycomb environment `receipts-demo`, with scripted incidents
whose root cause is written down in a file. The eval harness grades against that file, so the
generator is the ground truth and everything here exists to keep it trustworthy.

```
gen/
  topology.py     services, span tree, attributes, fault injection. No network, no clock.
  scenario.py     the scenario schema, the loader, and the checks that reject a file that disagrees with itself.
  scenarios/*.yml one file per scripted incident.
  emit.py         gives spans real timestamps and ships them over OTLP/HTTP.
  verify.py       asserts the fault is visible in Honeycomb before a run is used.
  runs/           one manifest per run, written by emit.py, read by verify.py. Gitignored.
```

## Running it

```bash
uv run python -m gen.emit --scenario payments-stripe-v251-uswest
uv run python -m gen.verify --scenario payments-stripe-v251-uswest --run-id run-665e4c1b3bae
```

`emit` prints a fresh `run_id`, the dataset, and the time window, and writes the same to
`gen/runs/<run_id>.json`. `verify` reads that manifest, so it does not need to be told a window.
It exits non-zero when a check fails.

Useful flags on `emit`:

| Flag | Default | What it does |
|---|---|---|
| `--seed` | `0` | Seeds the request stream. Same seed, same run. |
| `--backdate` | on | Timestamps the window so it ends just before now. |
| `--realtime` | off | Plays the window out at wall speed instead. |
| `--lag-seconds` | `60` | How far before now a backdated window ends. |
| `--max-spans-per-second` | `2500` | Ingest pacing. See "Ingest" below. Zero turns it off. |
| `--concurrency` | `1` | Parallel OTLP posters. More than one has cost spans. |
| `--dry-run` | off | Builds every span, prints the counts, sends nothing. |

## The topology

Four services, one trace per request, five spans per trace:

```
gateway       HTTP POST /checkout          root, SERVER
  checkout      checkout.process           INTERNAL
    payments      payments.charge          CLIENT
    inventory-db  inventory.reserve        INTERNAL
      inventory-db  db.query               CLIENT
```

Children run one after another inside their parent, so a parent's duration is its own work plus
the sum of its children. Latency added to `payments.charge` therefore shows on the root span, which
is what makes a heatmap of root `duration_ms` the right first query.

Baseline self-times are log-normal: 4ms at the gateway, 6ms in checkout, 55ms in payments, 5ms in
inventory, 9ms in the database. That puts the root span at roughly 80ms median and 230ms at P99
with no fault. The baseline error rate is 0.5%, spread across the four services and weighted toward
payments so a control does not look obviously different from an incident.

### One dataset, not four

Everything lands in `receipts-shop`. In a Honeycomb environment that is not classic the dataset is
taken from the resource's `service.name`, so the resource carries `service.name = receipts-shop`
and each span names its real service in `service.component`.

Two consequences:

- One schema and one BubbleUp across all four services, and one `run_query` can compare them. That
  is what this project needs: the agent's job is to find which service and which dimensions moved,
  and splitting the data into four datasets would make that four queries instead of one.
- Honeycomb's per-service features do not work on this data. The service map, per-service SLOs, and
  anything else keyed on `service.name` see one service called `receipts-shop`. Slice by
  `service.component` instead.

### Attributes

On every span, because in a real system they come from the resource or the request context and a
query for one child span has to be able to filter on them:

| Attribute | Values |
|---|---|
| `deployment.version` | `2.4.0` 30%, `2.5.0` 30%, `2.5.1` 40%. A scenario may override this. |
| `cloud.region` | `us-west-2` 50%, `us-east-1` 30%, `eu-west-1` 20% |
| `payment.provider` | `stripe` 60%, `adyen` 25%, `paypal` 15% |
| `service.component` | `gateway`, `checkout`, `payments`, `inventory-db` |
| `scenario.id` | the scenario's id |
| `scenario.run_id` | the run id, fresh per run. Every query scopes to it. |
| `error` | true on a failing span and on every ancestor above it |

On the root span, plus the child spans where they belong:

| Attribute | Values |
|---|---|
| `customer.id` | 2,000 ids, Zipf weighted, so BubbleUp has a high-cardinality column to chew on |
| `cart.size` | 1 to 12, decaying. Also on `inventory.reserve`. |
| `http.route` | `/checkout` 80%, `/checkout/express` 15%, `/checkout/gift` 5% |
| `http.status_code` | 200, or 500 when the request failed |
| `db.statement.hash` | eight hashes, weighted. Also on `db.query`. |

`duration_ms` is not set here. Honeycomb derives it from the OTLP span, and writing our own would
collide with it.

The root span is named `HTTP POST /checkout` for every request. `http.route` is the sub-path inside
the checkout API, so the span name stays the low-cardinality operation and the route stays a
dimension worth breaking down on.

## The scenario file, field by field

```yaml
id: payments-stripe-v251-uswest
narrative: >-
  v2.5.1 rolled to us-west-2; stripe calls in payments gained about 800ms at the
  ten minute mark.
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
    note: pre-existing cross-region database latency, not the incident
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | The scenario's name. Must match the file name without `.yml`. |
| `narrative` | yes | What happened, in one or two sentences. For the README and the report, never shown to the agent. |
| `baseline.rps` | yes | Requests per second across the whole window. |
| `baseline.minutes` | yes | Length of the window. `rps * minutes * 60` requests, five spans each. |
| `dimensions` | no | Overrides the weights for one or more of `deployment.version`, `cloud.region`, `payment.provider`. A value that appears nowhere else, such as `2.6.0`, exists only because a scenario declares it here. Weights are normalised, so they need not sum to one. |
| `fault` | only when there is an incident | The incident. |
| `fault.onset_min` | yes | Minutes into the window at which the fault starts. Before this, the run is baseline. |
| `fault.where` | yes | Which requests the fault hits, as an AND over dimension values. Every name must be a dimension the generator emits and every value must be one the scenario emits. |
| `fault.effect.span` | yes | Which span the fault acts on. Must be one of the five in the topology. |
| `fault.effect.latency_add_ms` | one of the two | Milliseconds added to that span's own work, jittered by about 12% so the slow requests spread across a band of the heatmap instead of stacking on one value. |
| `fault.effect.error_rate` | one of the two | Probability that span fails, per matching request, after onset. |
| `ground_truth.incident_present` | yes | What the agent has to get right. False means a control, and a control may not carry a fault. |
| `ground_truth.root_cause_dims` | when there is an incident | Must repeat `fault.where` exactly. |
| `ground_truth.slow_or_failing_span` | when there is an incident | Must name `fault.effect.span`. |
| `ground_truth.affected_share` | when there is an incident | The share of all requests in the run that match `fault.where`. This is the population share over the whole window, not the share that were actually slowed: onset splits the window in time, it does not change who is in the population. |
| `red_herrings` | no | Other effects that are not the incident. Same shape as a fault, plus a `note`. |
| `red_herrings[].onset_min` | no, defaults to 0 | A red herring is normally present for the whole window, so a before-and-after comparison separates it from the fault. |

The models reject a file that does not hold together: an incident with no fault, `root_cause_dims`
that disagree with `fault.where`, a span the topology does not have, a dimension value the scenario
never emits, an effect that neither adds latency nor raises the error rate, an unknown key. A typo
fails at load rather than producing a run that quietly verifies nothing.

## The scenarios

| id | class | fault | population | red herring |
|---|---|---|---|---|
| `payments-stripe-v251-uswest` | latency spike | `payments.charge` +800ms on v2.5.1 / us-west-2 / stripe, from minute 10 | 12% | eu-west-1 `db.query` +150ms, whole window |
| `checkout-error-surge-adyen` | error surge | `payments.charge` fails 25% of the time on adyen, from minute 10 | 25% | us-east-1 `payments.charge` fails 3%, whole window |
| `deploy-regression-v260` | deployment regression | `checkout.process` +400ms on v2.6.0 in every region, from minute 10 | 35% | paypal `payments.charge` +120ms, whole window |
| `control-quiet` | control | none | n/a | the same eu-west-1 `db.query` +150ms |

The remaining scenarios in the plan (dependency failure, trigger fired, a second control, and two
where a red herring is stronger in count than the true cause) are R5.

`deploy-regression-v260` is already the awkward one. Selecting the slow band on a root-span heatmap
and running BubbleUp puts `deployment.version 2.6.0` first at 36.7% to 100%, and `payment.provider
paypal` second at 15.4% to 67.8%. Both are real. Only one of them stepped at onset, and separating
them means looking at the time dimension rather than at the ranking.

## Backdating

Backdating works. Honeycomb accepts span timestamps in the recent past, and a run whose window ends
sixty seconds before now lands in that window and can be queried there straight away. That is the
default, and it means a twenty minute scenario emits in about forty seconds instead of twenty
minutes.

Real-time emission also works and is kept as the fallback, and as the mode to use against a live
trigger in R5, which fires on wall-clock time. It sleeps between requests so the window plays out at wall speed.
Both modes are recorded in the manifest as `mode`.

## Ingest

One experiment, two conditions. The same deterministic 90,000 span run was emitted twice:

| Posters | Rate | Spans that arrived |
|---|---|---|
| 4 | about 6,800/s | 57,500 of 90,000 |
| 1 | about 3,300/s | 90,000 of 90,000 |

Spans were counted with a `COUNT` per span name over the run id through the MCP. In the partial run
every span name was short by the same amount, so whole batches were lost rather than single spans,
and a per-minute count showed the first two minutes complete with acceptance falling after that.

Every request in both runs came back HTTP 200 with an empty body. Decoding the body as
`ExportTraceServiceResponse` gives an empty `partial_success`. The Python OTLP exporter returns
success on any 2xx without reading that field, so it would report nothing even if it were set.

Rate and connection count changed together, so the experiment shows that four posters at 6,800
spans per second lose data and one poster at 3,300 does not. It does not say where between those the
limit sits, or whether it is a rate limit or a concurrency limit.

Two things follow. The emitter defaults to one poster and a cap of 2,500 spans per second. And
`gen/verify.py` counts the root spans that arrived against the manifest before it believes any
other number, and the count has to be exact. A partial run fails verification instead of quietly
halving the population and moving the percentiles.

The count can be exact because the window edges sit on whole seconds. The hosted MCP truncates
`start_time` and `end_time` to whole seconds, so a window that ended at 01:46:33.845 and was
queried to 01:46:33 came back a dozen requests short on every run until the emitter started
flooring the edges.

## What verification checks

Every query is scoped to `scenario.run_id`, so runs of the same scenario never contaminate each
other, and every query goes through the hosted Honeycomb MCP client in `agent/mcp_client.py`.

For every scenario:

- **ingest.** At least 98% of the manifest's root spans are queryable in the window.
- **row counts.** At least 100 rows in each half of the window, inside and outside the population.
- **affected share.** The measured population share is within 0.02 of `ground_truth.affected_share`.

For a latency fault:

- P99 of the faulted span inside `where` is at least 2x higher after onset than before.
- P99 outside `where` moves by less than 1.5x. "Outside" is the exact complement, built by OR-ing a
  `!=` for each clause in `where`. That is the verify-by-negation query.

For an error fault:

- The error rate inside `where` after onset reaches at least half the injected rate, and at least
  5x its own before-onset rate.
- The error rate outside `where` moves by less than 3x.

For a control:

- No latency step and no error step on the root span between the first half of the window and the
  second.

Five `run_query` calls at most, all inside the MCP client's pacing.
