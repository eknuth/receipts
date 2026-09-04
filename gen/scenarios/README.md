# The scenarios

Ten scripted incidents, each one a YAML file in this directory plus the ground truth the eval
harness grades against. `scenario.id` is not on the wire: the values read as answers, so an agent
that broke down on that column would be handed the root cause and whether there is an incident at
all. Field-by-field documentation of the YAML shape is in `gen/README.md`.

| id | class | true cause | herring | expected agent answer |
|---|---|---|---|---|
| `payments-stripe-v251-uswest` | latency spike | `payments.charge` +800ms on v2.5.1 / us-west-2 / stripe, from minute 10 | eu-west-1 `db.query` +150ms, whole window | v2.5.1 / us-west-2 / stripe is the incident; eu-west-1's database latency is steady and pre-existing |
| `checkout-error-surge-adyen` | error surge | `payments.charge` fails 25% of the time on adyen, from minute 10 | us-east-1 `payments.charge` fails 3%, whole window | adyen is the incident; us-east-1's failure rate is steady and pre-existing |
| `deploy-regression-v260` | deployment regression | `checkout.process` +400ms on v2.6.0 in every region, from minute 10 | paypal `payments.charge` +120ms, whole window | v2.6.0 is the incident; paypal has always been slower, on a different span |
| `control-quiet` | control | none | eu-west-1 `db.query` +150ms, steady | no incident |
| `dependency-inventory-db-timeouts` | dependency failure | `db.query` under inventory-db times out (5000ms exactly) for 40% of calls, every request, from minute 10 | eu-west-1 `checkout.process` +150ms, whole window | inventory-db is the incident, affecting the whole population; eu-west-1's checkout latency is steady and unrelated |
| `trigger-checkout-latency` | trigger fired | `checkout.process` +1400ms for `cart.size >= 8`, from minute 10; a Honeycomb trigger on root P99 > 1500ms fires after onset | paypal `payments.charge` +100ms, whole window | large carts are the incident; paypal has always been a touch slower, on a different span |
| `control-noisy` | control | none; every span's spread is doubled | 30 second burst of paypal `payments.charge` errors at minute 4 that resolves on its own | no incident; the noise and the burst are both explained, neither is ongoing |
| `herring-region-vs-version` | red herring stronger by count | `checkout.process` +600ms on v2.6.1, 8% of traffic, from minute 10 | us-east-1 `checkout.process` +120ms, 45% of traffic, whole window | v2.6.1 is the incident even though us-east-1 is larger by count; only the version stepped at onset |
| `herring-customer-whale` | red herring stronger by count | `payments.charge` fails 20% on adyen, from minute 10 | cust-00007 is 15% of traffic and about 30% of all errors, whole window | adyen is the incident; the whale's error share is steady across onset, adyen's is not |
| `error-surge-exceptions` | error surge, with exceptions | `payments.charge` fails 25% on adyen with a `ProviderDeclined` exception event, from minute 10 | us-east-1 `payments.charge` fails 3%, no exception, whole window | adyen is the incident; us-east-1's failures carry no exception and are steady |

A test (`tests/test_gen_scenario.py`) asserts this table lists exactly the ids in
`gen/scenarios/*.yml`, no more and no fewer.
