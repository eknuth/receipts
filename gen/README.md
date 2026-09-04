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
| `scenario.run_id` | the run id, fresh per run. Every query scopes to it. An `exception` span event carries its own copy too: Honeycomb gives an event row only its own attributes, separate from the span it hangs off. |
| `error` | true on a failing span and on every ancestor above it |

On the root span, plus the child spans where they belong:

| Attribute | Values |
|---|---|
| `customer.id` | 2,000 ids, Zipf weighted, so BubbleUp has a high-cardinality column to chew on |
| `cart.size` | 1 to 12, decaying. Also on `inventory.reserve`. |
| `http.route` | `/checkout` 80%, `/checkout/express` 15%, `/checkout/gift` 5% |
| `http.status_code` | 200, or 500 when the request failed |
| `db.statement.hash` | eight hashes, weighted. Also on `db.query`. |

`scenario.id` is not on the wire. Its values read as answers, such as
`payments-stripe-v251-uswest` and `control-quiet`, so an agent that broke down on that column
would be handed the root cause and whether there is an incident at all. `scenario.run_id` is the
only run identifier the data carries, and the manifest in `gen/runs/` maps a run id back to its
scenario for the grader.

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
| `baseline.sigma_scale` | no, defaults to 1.0 | Multiplies every span's log-normal sigma (its spread). A control can use this to look noisier than usual while each span's own median holds, a different thing from a fault stepping a median at onset. The root span's median is the exception: its duration sums its own time and every descendant's, and a sum of wider log-normals has a heavier right tail, so `control-noisy`'s root median at `sigma_scale: 2.0` runs about 1.19x `sigma_scale: 1.0`'s even though every individual span's own median (gateway, checkout.process, payments.charge, inventory.reserve, db.query) stays within a few percent. |
| `dimensions` | no | Overrides the weights for one or more of `deployment.version`, `cloud.region`, `payment.provider`, or pins one or more `customer.id` values to a fixed share (see below). A value that appears nowhere else, such as `2.6.0`, exists only because a scenario declares it here. Weights are normalised, so they need not sum to one. |
| `dimensions.customer.id` | no | `{"cust-00007": 0.15}`-style: pins that customer to exactly 15% of traffic. The remaining share is spread over the rest of the 2,000 Zipf ids in their normal proportions. A `where` clause may only name a customer id that is pinned here. |
| `fault` | only when there is an incident | The incident. |
| `fault.onset_min` | yes | Minutes into the window at which the fault starts. Before this, the run is baseline. |
| `fault.where` | yes | Which requests the fault hits, as an AND over dimension values. Every name must be a dimension the generator emits and every value must be one the scenario emits, with two exceptions below (`service.component` and a numeric range). |
| `fault.where` on `service.component` | no | Names the service that runs `fault.effect.span` (checked against the topology at load time). Every request touches every service, so this clause selects the whole run: population share 1.0, and there is no "outside" for the verifier to compare against (see "What verification checks"). |
| `fault.where` on a numeric range | no | A value like `>=8`, `<5`, matching `^(>=|<=|>|<)\s*-?\d+(\.\d+)?$`. Only a numeric dimension accepts one; today that is `cart.size` only. The dimension must be one the effect's span actually carries. |
| `fault.effect.span` | yes | Which span the fault acts on. Must be one of the five in the topology. |
| `fault.effect.latency_add_ms` | one of latency/error | Milliseconds added to that span's own work, jittered by about 12% so the slow requests spread across a band of the heatmap instead of stacking on one value. Exclusive with `timeout_ms`. |
| `fault.effect.error_rate` | one of latency/error | Probability that span fails, per matching request, after onset. |
| `fault.effect.timeout_ms` | no, requires `error_rate > 0` | On a span that failed because of this effect, its own time is replaced outright by this value, exactly, no jitter: a deadline does not vary. Exclusive with `latency_add_ms`. |
| `fault.effect.error_type` | no, requires `error_rate > 0` | Written as the `error.type` attribute on a span that failed because of this effect, and only that span, not its ancestors. |
| `fault.effect.exception` | no, requires `error_rate > 0` | `{type, message}`. A span that failed because of this effect gets an OTel span event named `exception` at its end time, with `exception.type`, `exception.message`, `exception.escaped=true`, and `scenario.run_id`. It lands in Honeycomb as its own row (`name = exception`, `meta.annotation_type = span_event`, `trace.parent_id` the failing span's id, carrying only its own attributes); a query filtered to the fault's span name still counts the span rows correctly, the event is a separate row alongside it. Honeycomb also copies `exception.type` and `exception.message` onto the parent span's own row (`error-surge-exceptions`: a run-scoped breakdown on `exception.type` over `name = payments.charge` and a count of `name = exception` rows both came back 570, matching the manifest's `span_events`), which is what makes filtering on `exception.type` a real query rather than a guess. Baseline failures and a red herring's failures never get one, only the fault's own. |
| `ground_truth.incident_present` | yes | What the agent has to get right. False means a control, and a control may not carry a fault. |
| `ground_truth.root_cause_dims` | when there is an incident | Must repeat `fault.where` exactly. |
| `ground_truth.slow_or_failing_span` | when there is an incident | Must name `fault.effect.span`. |
| `ground_truth.affected_share` | when there is an incident | The share of all requests in the run that match `fault.where`. This is the population share over the whole window, not the share that were actually slowed: onset splits the window in time, it does not change who is in the population. |
| `ground_truth.equivalent_dims` | no | Alternative selectors, declared deliberately per scenario, not inferred, and checked against the topology at load time: every key of `fault.where` other than `service.component` has to appear with the same value (every request runs every span, so only those dimensions narrow the population), and on top of that an alternative may add or swap in a `name` clause equal to `fault.effect.span` or a `service.component` clause equal to the service that runs it; any other key is rejected, and so is an alternative identical to `root_cause_dims` or to an earlier entry. The grader scores the top hypothesis's dims against `root_cause_dims` and against every entry here and keeps the best. |
| `red_herrings` | no | Other effects that are not the incident. Same shape as a fault, plus a `note`. |
| `red_herrings[].onset_min` | no, defaults to 0 | A red herring is normally present for the whole window, so a before-and-after comparison separates it from the fault. |
| `red_herrings[].duration_min` | no, defaults to unset | Unset means "the rest of the window" (the original behaviour). Set it to make the herring a burst: active from `onset_min` for `duration_min`, then off, on its own, with no further intervention. `onset_min + duration_min` must stay inside the window. Keep it wide enough to clear the verifier's own row-count floor (100 rows, `MIN_ROWS` in `gen/verify.py`): 30 seconds at 15rps on a 15% population holds about 64 rows, too thin to judge, which is why `control-noisy` uses 1.0 minute (about 135 rows) instead. |
| `trigger` | no | A Honeycomb trigger this scenario expects to fire, created by hand in the UI. `{id, name, threshold_ms, window_min, frequency_s, note}`. The UI allows a duration of at most 4x the frequency, in whole minutes, so a 5 minute window runs every 2 minutes at the fastest (120s in `trigger-checkout-latency`; the issue asked for 60s). `gen/verify.py` calls `get_triggers` and checks the row for `id` reports `triggered: true`. |

The models reject a file that does not hold together: an incident with no fault, `root_cause_dims`
that disagree with `fault.where`, a span the topology does not have, a dimension value the scenario
never emits, an effect that neither adds latency nor raises the error rate, a `timeout_ms` without
a positive `error_rate` or alongside `latency_add_ms`, a `where` clause naming a dimension its span
does not carry, a customer id in `where` that was not pinned in `dimensions.customer.id`, a red
herring burst that runs past the end of the window, an unknown key. A typo fails at load rather
than producing a run that quietly verifies nothing.

## The scenarios

| id | class | fault | population | red herring |
|---|---|---|---|---|
| `payments-stripe-v251-uswest` | latency spike | `payments.charge` +800ms on v2.5.1 / us-west-2 / stripe, from minute 10 | 12% | eu-west-1 `db.query` +150ms, whole window |
| `checkout-error-surge-adyen` | error surge | `payments.charge` fails 25% of the time on adyen, from minute 10 | 25% | us-east-1 `payments.charge` fails 3%, whole window |
| `deploy-regression-v260` | deployment regression | `checkout.process` +400ms on v2.6.0 in every region, from minute 10 | 35% | paypal `payments.charge` +120ms, whole window |
| `control-quiet` | control | none | n/a | the same eu-west-1 `db.query` +150ms |
| `dependency-inventory-db-timeouts` | dependency failure | `db.query` under inventory-db times out (5000ms exactly) for 40% of calls, every request, from minute 10 | 100% | eu-west-1 `checkout.process` +150ms, whole window |
| `trigger-checkout-latency` | trigger fired | `checkout.process` +1400ms for `cart.size >= 8`, from minute 10; a trigger on root P99 > 1500ms should fire after onset | 10.5% | paypal `payments.charge` +100ms, whole window |
| `control-noisy` | control | none; every span's spread doubled | n/a | one minute burst of paypal `payments.charge` errors at minute 4, resolves on its own |
| `herring-region-vs-version` | red herring stronger by count | `checkout.process` +600ms on v2.6.1, from minute 10 | 8% | us-east-1 `checkout.process` +120ms, 45% of traffic, whole window |
| `herring-customer-whale` | red herring stronger by count | `payments.charge` fails 20% on adyen, from minute 10 | 25% | cust-00007, 15% of traffic and about 30% of all errors, whole window |
| `error-surge-exceptions` | error surge, with exceptions | `payments.charge` fails 25% on adyen with a `ProviderDeclined` exception event, from minute 10 | 25% | us-east-1 `payments.charge` fails 3%, no exception, whole window |

A table with a "true cause / herring / expected agent answer" column per scenario lives in
`gen/scenarios/README.md`.

`deploy-regression-v260` is already the awkward one. Selecting the slow band on a root-span heatmap
and running BubbleUp puts `deployment.version 2.6.0` first at 36.7% to 100%, and `payment.provider
paypal` second at 15.4% to 67.8%. Both are real. Only one of them stepped at onset, and separating
them means looking at the time dimension rather than at the ranking.

The two herring scenarios were built on the premise that a BubbleUp over the whole window would
rank the herring first because it is the larger population, and that a BubbleUp restricted to the
post-onset window would rank the true cause first. Live runs on 2026-09-04 showed something else.
BubbleUp ranks columns by how far the selection's distribution sits from the baseline's, and the
selection band decides that, not the time window. On `herring-region-vs-version`, a root
`duration_ms` band from 200 ms up ranks `cloud.region` first in both windows (whole window
25% to 89%, post-onset 37% to 84%), and a band from 300 ms up ranks `deployment.version`
first in both (whole window 4% to 72%, post-onset 4.5% to 85%); at 250 ms the two are within
half a point of each other. On `herring-customer-whale`, a selection of `error = true` ranks
`payment.provider` first and `customer.id` second in both windows (adyen 23% to 78% against
the whale's 16% to 30% over the whole window). Restricting the window cannot demote a herring
that is steady across it, because the herring's share of the band is the same on both sides of
onset. What separates them in Honeycomb's method is the step in time: a heatmap shows the
version's band appear at onset while the region's band is flat, and the negation query
(`WHERE deployment.version != 2.6.1`) shows P99 flat across onset. The screenshots are in
`docs/r5-herring-*.png`.

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

Rate and connection count changed together, so the experiment on its own shows that four posters at
6,800 spans per second lose data and one poster at 3,300 does not. The number came an hour later by
email. Honeycomb support sent a "Rate limit exceeded" notice for the team: the limit is 4,000 events
per second per ingest type, and 32,500 events in `receipts-shop` were dropped in the past hour. That
is exactly 90,000 minus 57,500. The email is sent at most once per 24 hours, and there is no
in-band signal, so an emitter that runs faster than that finds out by mail the next day, if at all.

Two things follow. The emitter defaults to one poster and a cap of 2,500 spans per second. And
`gen/verify.py` counts the root spans that arrived against the manifest before it believes any
other number, and the count has to be exact. A partial run fails verification instead of quietly
halving the population and moving the percentiles.

The count can be exact because the window edges sit on whole seconds. The hosted MCP truncates
the query's `from` and `to` to whole seconds, so a window that ended at 01:46:33.845 and was
queried to 01:46:33 came back a dozen requests short on every run until the emitter started
flooring the edges. Those bounds were called `start_time` and `end_time` in `query_spec` until
2026-09-04, when a live check found the hosted MCP rejecting the old names with an error that
says they were renamed. Every query here now sends `from` and `to`.

## What verification checks

Every query is scoped to `scenario.run_id`, so runs of the same scenario never contaminate each
other, and every query goes through the hosted Honeycomb MCP client in `agent/mcp_client.py`.

For every scenario:

- **ingest.** At least 98% of the manifest's root spans are queryable in the window.
- **row counts.** At least 100 rows in each half of the window, inside and outside the population,
  and in every red herring burst window (see "control" below).
- **affected share.** The measured population share is within 0.02 of `ground_truth.affected_share`.

For a latency fault:

- P99 of the faulted span inside `where` is at least 2x higher after onset than before.
- P99 outside `where` moves by less than 1.5x. "Outside" is the exact complement, built by OR-ing a
  `!=` for each clause in `where`. That is the verify-by-negation query.

For an error fault:

- The error rate inside `where` after onset reaches at least half the injected rate, and at least
  5x its own before-onset rate.
- The error rate outside `where` moves by less than 3x.

For a dependency fault (`where` names only `service.component`, so the population is the whole
run and there is no "outside" for the checks above to compare against):

- The inside latency and error checks above still run.
- The population query itself is different: scoped only to the run id, it counts root spans for
  the total and the fault's own span, filtered to the service it runs on, for "inside". That is a
  real measurement, so a run that dropped the fault's own span fails the affected-share check
  instead of passing 1.000 against 1.000 by construction.
- In place of the outside checks, one negation stands in for both: P99 of a different, unrelated
  span (`payments.charge`) moves by less than 1.5x across the same onset.

For every fault with a red herring, one check per herring: **the fault's step survives excluding
the herring.** Filtering the fault's own span to the herring's `where`, before and after onset,
and reading the OUTSIDE numbers (the complement of the herring's population) proves the incident
holds with the herring's population excluded, and is not an artifact of it. A latency fault needs
the outside P99 to step by 2x; an error fault needs the outside error rate to step by 5x, which
needs its own outside-errors query per window, the same way the main measurement does. Two
`run_query` calls per herring for a latency fault, four for an error fault. This is what makes
`herring-customer-whale` honest: the whale holds about 30% of all errors over the window, but
excluding the whale, adyen's error rate still steps about 13x at onset, which is the check that
proves the incident is adyen and not the whale.

For a control:

- No latency step and no error step on the root span between the first half of the window and the
  second.
- For every red herring with a `duration_min` (a burst): its own rate inside the burst window
  clears half its injected rate and steps at least 5x above its rate in the rest of the window
  (one check), and its rate from the end of the burst to the end of the window falls back under
  3x its rate before the burst (a second check). Together these two checks prove the burst
  happened and then stopped on its own, distinct from a mean that just runs a little higher across
  the whole window. `control-noisy`'s whole verification stays at or under seven `run_query` calls.

For a scenario with a `trigger` block: one `get_triggers` call (list mode, `environment_slug`
only, it takes no `dataset_slug`, unlike `run_query`) after everything else, checking that the
row for the trigger's `id` reports `triggered: true`. This reads the trigger's current state, not
a history: a run passes if any run fired it, this one included, and fails if verify runs after
the triggered window has passed. A run where it never fired is NOT VERIFIED.

For a fault whose effect carries an `exception`, two checks. First, one `run_query` breaking down
on `trace.trace_id` (filtered to the fault's population and span, `error = true`, after onset,
`limit: 1`), then one `get_trace` on that id with `show_events: true`. A live check found that
Honeycomb renders a span event as its own row with a `name`, an `annotation_type` of
`span_event`, and a `parent_id` pointing at the span it hangs off, and renders none of the
event's attributes, so this check confirms that a `span_event` row named `exception` hangs off a
failed span named `fault.effect.span` and stops there. Second, the type: one more `run_query`,
scoped to the run and filtered to `name = exception` and `exception.type = <the configured type>`,
which reads the event rows themselves (each carries its own attributes, and the emitter puts the
run id on it), and whose `COUNT` must equal `manifest.span_events` exactly, the same exactness the
ingest check uses. Honeycomb also copies `exception.type` and `exception.message` onto the parent
span's row, so a run-scoped breakdown on `exception.type` over `name = payments.charge` gives the
same number. That query extends `to` ten seconds past the window's own end: an event sits at its
span's end time, and a span starting just before the window closes still runs its full duration,
so a failure in the last fraction of a second could otherwise land past `to` and be missed.

Five `run_query` calls at most for a plain fault or control, all inside the MCP client's pacing.
