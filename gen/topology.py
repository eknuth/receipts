"""The synthetic shop: service topology, span tree, attributes, and fault injection.

This module is the "production system" the agent investigates. It has no
network and no clock. It turns a scenario plus a seed into a deterministic
list of requests, each one a tree of spans with durations and attributes.
`gen/emit.py` gives those spans real timestamps and ships them to Honeycomb.

Topology, four services:

    gateway   HTTP POST /checkout          root
      checkout   checkout.process
        payments      payments.charge
        inventory-db  inventory.reserve
          inventory-db  db.query

Children run one after another inside their parent, so a parent's duration is
its own work plus the sum of its children. A fault that adds time to
`payments.charge` therefore shows up on the root span too, which is what makes
a heatmap of root duration the right first query.

Every span carries `service.component` and the run id. The three dimensions a
fault can select on (`deployment.version`, `cloud.region`, `payment.provider`)
are on every span as well, because in a real system they come from the resource
and the request context rather than from one span. The rest (`customer.id`,
`cart.size`, `http.route`, `http.status_code`, `db.statement.hash`) sit where
they belong, mostly on the root.

The scenario id is not on the wire. Its values read as answers, such as
`payments-stripe-v251-uswest` and `control-quiet`, so an agent that broke down
on that column would be handed the root cause and whether there is an incident
at all. The run manifest in `gen/runs/` maps a run id back to its scenario, and
that is where the grader reads it.

Latencies are log-normal around a median. Base error rate is 0.5%, spread
across the four services. A fault adds latency to one named span, or makes it
fail, for the requests that match its `where` clause after its onset minute.
The added latency is jittered by about 12% so the slow requests spread across
a band of the heatmap instead of stacking on one value.
"""

from __future__ import annotations

import bisect
import math
import random
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for type checkers
    from gen.scenario import Effect, Scenario

GATEWAY = "gateway"
CHECKOUT = "checkout"
PAYMENTS = "payments"
INVENTORY_DB = "inventory-db"
SERVICES: tuple[str, ...] = (GATEWAY, CHECKOUT, PAYMENTS, INVENTORY_DB)

ROOT_SPAN = "HTTP POST /checkout"

# Share of requests that fail for ordinary reasons, with no fault injected.
BASELINE_ERROR_RATE = 0.005

# Where an ordinary failure originates. Weighted so most failures come from
# the payment provider, which is also where two of the scenarios put a fault:
# a control whose failures come from elsewhere would give the answer away.
BASELINE_ERROR_SOURCE: dict[str, float] = {
    "payments.charge": 0.50,
    "db.query": 0.20,
    "inventory.reserve": 0.20,
    "checkout.process": 0.10,
}

# Spread of the multiplier applied to an injected latency add, as a fraction.
FAULT_JITTER_SIGMA = 0.12
FAULT_JITTER_FLOOR = 0.3


@dataclass(frozen=True)
class SpanNode:
    """One node of the span tree, with the median of its own work in ms.

    `self_ms_median` and `self_ms_sigma` describe the log-normal distribution
    of the time this span spends outside its children. `dims` names the
    request dimensions copied onto this span beyond the three that go
    everywhere.
    """

    name: str
    service: str
    self_ms_median: float
    self_ms_sigma: float
    dims: tuple[str, ...] = ()
    children: tuple[SpanNode, ...] = ()


TREE = SpanNode(
    name=ROOT_SPAN,
    service=GATEWAY,
    self_ms_median=4.0,
    self_ms_sigma=0.45,
    dims=("customer.id", "cart.size", "http.route", "db.statement.hash"),
    children=(
        SpanNode(
            name="checkout.process",
            service=CHECKOUT,
            self_ms_median=6.0,
            self_ms_sigma=0.45,
            dims=("customer.id", "cart.size", "http.route"),
            children=(
                SpanNode(
                    name="payments.charge",
                    service=PAYMENTS,
                    self_ms_median=55.0,
                    self_ms_sigma=0.55,
                    dims=("customer.id",),
                ),
                SpanNode(
                    name="inventory.reserve",
                    service=INVENTORY_DB,
                    self_ms_median=5.0,
                    self_ms_sigma=0.45,
                    dims=("cart.size",),
                    children=(
                        SpanNode(
                            name="db.query",
                            service=INVENTORY_DB,
                            self_ms_median=9.0,
                            self_ms_sigma=0.60,
                            dims=("db.statement.hash",),
                        ),
                    ),
                ),
            ),
        ),
    ),
)


def _walk(node: SpanNode) -> Iterator[SpanNode]:
    yield node
    for child in node.children:
        yield from _walk(child)


SPAN_NAMES: tuple[str, ...] = tuple(node.name for node in _walk(TREE))
SPAN_SERVICE: dict[str, str] = {node.name: node.service for node in _walk(TREE)}

# Dimensions a fault or a red herring may select on. Weights are the default
# mix; a scenario can override any of them in its `dimensions:` block.
# The default mix puts 0.40 * 0.50 * 0.60 = 0.12 of traffic in the
# v2.5.1 / us-west-2 / stripe corner, which is the first scenario's fault
# population and its ground-truth affected share.
DIMENSION_WEIGHTS: dict[str, dict[str, float]] = {
    "deployment.version": {"2.4.0": 0.30, "2.5.0": 0.30, "2.5.1": 0.40},
    "cloud.region": {"us-west-2": 0.50, "us-east-1": 0.30, "eu-west-1": 0.20},
    "payment.provider": {"stripe": 0.60, "adyen": 0.25, "paypal": 0.15},
}

# Dimensions that ride on every span, not just the root. In a real system these
# come from the resource and the request context, so a query for one child span
# can still filter on them, which is what the verifier and the agent need.
PROPAGATED_DIMS: tuple[str, ...] = tuple(DIMENSION_WEIGHTS)

# Everything copied onto every span. `scenario.run_id` is added by gen/emit.py,
# which is where a run id exists. The scenario id is deliberately absent; see
# the module docstring.
SPAN_COMMON_ATTRIBUTES: tuple[str, ...] = PROPAGATED_DIMS

CUSTOMER_COUNT = 2000
CUSTOMER_ZIPF_EXPONENT = 0.9

HTTP_ROUTES: dict[str, float] = {
    "/checkout": 0.80,
    "/checkout/express": 0.15,
    "/checkout/gift": 0.05,
}

DB_STATEMENT_HASHES: dict[str, float] = {
    "a1f3c2": 0.34,
    "b7e401": 0.22,
    "c09d55": 0.17,
    "d4128a": 0.12,
    "e5b6f0": 0.07,
    "f81c93": 0.05,
    "0a2e77": 0.02,
    "19bd4c": 0.01,
}


class WeightedChoice:
    """Draws a value from a fixed weighted set, in O(log n) per draw.

    Cumulative weights plus a bisect, so drawing 18,000 customer ids out of
    2,000 candidates costs the same as drawing three regions.
    """

    def __init__(self, weights: Mapping[str, float]) -> None:
        if not weights:
            raise ValueError("a weighted choice needs at least one value")
        if any(w <= 0 for w in weights.values()):
            raise ValueError("weights must be positive")
        self._values: list[str] = list(weights)
        total = float(sum(weights.values()))
        self._shares = {value: weights[value] / total for value in self._values}
        acc = 0.0
        cumulative: list[float] = []
        for value in self._values:
            acc += self._shares[value]
            cumulative.append(acc)
        cumulative[-1] = 1.0
        self._cumulative = cumulative

    def pick(self, rng: random.Random) -> str:
        return self._values[bisect.bisect_right(self._cumulative, rng.random())]

    def share(self, value: str) -> float:
        """The fraction of draws expected to come back as `value`."""
        return self._shares.get(value, 0.0)

    @property
    def values(self) -> list[str]:
        return list(self._values)


def customer_weights(count: int = CUSTOMER_COUNT) -> dict[str, float]:
    """Zipf-ish customer ids: the busiest customer is roughly 2,000x the quietest."""
    return {f"cust-{i:05d}": 1.0 / (i + 1) ** CUSTOMER_ZIPF_EXPONENT for i in range(count)}


def cart_size_weights() -> dict[str, float]:
    """Cart sizes 1 to 12, decaying, so small carts dominate."""
    return {str(size): 0.75 ** (size - 1) for size in range(1, 13)}


@dataclass
class SpanRecord:
    """One generated span: name, service, position inside the trace, attributes."""

    name: str
    service: str
    start_offset_ms: float
    duration_ms: float
    attributes: dict[str, object] = field(default_factory=dict)
    error: bool = False
    children: list[SpanRecord] = field(default_factory=list)

    def walk(self) -> Iterator[SpanRecord]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True)
class Request:
    """One generated request: when it happened, what it looked like, its spans."""

    index: int
    offset_s: float
    dims: dict[str, str]
    root: SpanRecord
    in_fault_population: bool
    faulted: bool

    @property
    def duration_ms(self) -> float:
        return self.root.duration_ms


@dataclass(frozen=True)
class _ActiveEffect:
    """An effect that applies to the request being built."""

    span: str
    latency_add_ms: float
    error_rate: float


def dimension_weights_for(scenario: Scenario) -> dict[str, dict[str, float]]:
    """The dimension mix this scenario uses: the defaults with its overrides applied."""
    weights = {name: dict(values) for name, values in DIMENSION_WEIGHTS.items()}
    for name, override in scenario.dimensions.items():
        weights[name] = dict(override)
    return weights


class Sampler:
    """The weighted draws one scenario needs, built once and reused per request."""

    def __init__(self, scenario: Scenario) -> None:
        self.dimensions = {
            name: WeightedChoice(values) for name, values in dimension_weights_for(scenario).items()
        }
        self.customer = WeightedChoice(customer_weights())
        self.cart_size = WeightedChoice(cart_size_weights())
        self.route = WeightedChoice(HTTP_ROUTES)
        self.db_statement = WeightedChoice(DB_STATEMENT_HASHES)
        self.error_source = WeightedChoice(BASELINE_ERROR_SOURCE)

    def population_share(self, where: Mapping[str, str]) -> float:
        """The expected fraction of requests matching every clause in `where`.

        The dimensions are drawn independently, so the share is the product of
        the per-dimension shares. A clause naming a value the scenario never
        emits gives zero, which is how a mistyped scenario file is caught.
        """
        share = 1.0
        for name, value in where.items():
            chooser = self.dimensions.get(name)
            if chooser is None:
                return 0.0
            share *= chooser.share(str(value))
        return share


def matches(dims: Mapping[str, str], where: Mapping[str, str]) -> bool:
    """True when every clause in `where` holds for these request dimensions."""
    return all(str(dims.get(name)) == str(value) for name, value in where.items())


def _effects_for(
    scenario: Scenario, dims: Mapping[str, str], offset_s: float
) -> tuple[list[_ActiveEffect], bool, bool]:
    """The effects in force for one request, plus fault population and fault flags."""
    active: list[_ActiveEffect] = []
    in_population = False
    faulted = False

    if scenario.fault is not None:
        in_population = matches(dims, scenario.fault.where)
        if in_population and offset_s >= scenario.fault.onset_min * 60.0:
            faulted = True
            active.append(_as_active(scenario.fault.effect))

    for herring in scenario.red_herrings:
        if matches(dims, herring.where) and offset_s >= herring.onset_min * 60.0:
            active.append(_as_active(herring.effect))

    return active, in_population, faulted


def _as_active(effect: Effect) -> _ActiveEffect:
    return _ActiveEffect(
        span=effect.span,
        latency_add_ms=effect.latency_add_ms,
        error_rate=effect.error_rate,
    )


def _build_span(
    node: SpanNode,
    rng: random.Random,
    effects: Sequence[_ActiveEffect],
    attributes: Mapping[str, object],
    failing: set[str],
) -> SpanRecord:
    """Build one span and its subtree, applying any effect aimed at this span."""
    self_ms = rng.lognormvariate(math.log(node.self_ms_median), node.self_ms_sigma)

    for effect in effects:
        if effect.span != node.name:
            continue
        if effect.latency_add_ms:
            jitter = max(FAULT_JITTER_FLOOR, rng.gauss(1.0, FAULT_JITTER_SIGMA))
            self_ms += effect.latency_add_ms * jitter
        if effect.error_rate and rng.random() < effect.error_rate:
            failing.add(node.name)

    span_attributes: dict[str, object] = {name: attributes[name] for name in SPAN_COMMON_ATTRIBUTES}
    for name in node.dims:
        if name in attributes:
            span_attributes[name] = attributes[name]
    span_attributes["service.component"] = node.service

    children: list[SpanRecord] = []
    cursor = self_ms / 2.0
    for child_node in node.children:
        child = _build_span(child_node, rng, effects, attributes, failing)
        child.start_offset_ms = cursor
        cursor += child.duration_ms
        children.append(child)

    duration = self_ms + sum(child.duration_ms for child in children)
    return SpanRecord(
        name=node.name,
        service=node.service,
        start_offset_ms=0.0,
        duration_ms=duration,
        attributes=span_attributes,
        children=children,
    )


def _mark_errors(span: SpanRecord, failing: set[str]) -> bool:
    """Set `error` on failing spans and every ancestor above them."""
    errored = span.name in failing
    for child in span.children:
        if _mark_errors(child, failing):
            errored = True
    span.error = errored
    if errored:
        span.attributes["error"] = True
    return errored


def generate_requests(scenario: Scenario, seed: int = 0) -> list[Request]:
    """Every request in one run of `scenario`, deterministic for a given seed.

    The stream is a function of the scenario id and the seed alone. Trace and
    span ids are not decided here; `gen/emit.py` gets those from the
    OpenTelemetry SDK so two runs of the same seed do not collide in Honeycomb.
    """
    rng = random.Random(f"{scenario.id}:{seed}")
    sampler = Sampler(scenario)

    total_seconds = scenario.baseline.minutes * 60.0
    count = int(round(scenario.baseline.rps * total_seconds))
    spacing = total_seconds / count if count else 0.0

    requests: list[Request] = []
    for index in range(count):
        jitter = rng.uniform(-0.5, 0.5) * spacing
        offset_s = min(max(index * spacing + jitter, 0.0), total_seconds - 1e-6)

        dims = {name: chooser.pick(rng) for name, chooser in sampler.dimensions.items()}
        attributes: dict[str, object] = dict(dims)
        attributes["customer.id"] = sampler.customer.pick(rng)
        attributes["cart.size"] = int(sampler.cart_size.pick(rng))
        attributes["http.route"] = sampler.route.pick(rng)
        attributes["db.statement.hash"] = sampler.db_statement.pick(rng)

        effects, in_population, faulted = _effects_for(scenario, dims, offset_s)

        failing: set[str] = set()
        root = _build_span(TREE, rng, effects, attributes, failing)
        if rng.random() < BASELINE_ERROR_RATE:
            failing.add(sampler.error_source.pick(rng))
        _mark_errors(root, failing)

        root.attributes["http.status_code"] = 500 if root.error else 200
        if not root.error:
            root.attributes["error"] = False

        requests.append(
            Request(
                index=index,
                offset_s=offset_s,
                dims=dims,
                root=root,
                in_fault_population=in_population,
                faulted=faulted,
            )
        )
    return requests


def span_count(requests: Sequence[Request]) -> int:
    """Total spans across every request, which is what Honeycomb bills as events."""
    return sum(1 for request in requests for _ in request.root.walk())
