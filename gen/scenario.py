"""Scenario files: the schema, the loader, and the checks that reject a file that disagrees
with itself.

A scenario is the ground truth for one incident. It says how much baseline
traffic to emit, which requests get the fault and when, what the fault does,
and what the right answer is. The grader reads `ground_truth` and nothing
else, so the file is the contract between the generator and the eval harness.

The models validate more than shape. A scenario that claims an incident must
carry a fault, and its `ground_truth` must repeat that fault's `where` clause
and its target span. A scenario that claims no incident must carry no fault.
Span names must exist in the topology and dimension values must be values the
scenario actually emits, so a typo fails at load instead of producing a run
that quietly verifies nothing.

Field-by-field documentation lives in `gen/README.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gen import topology

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

# How far ground_truth.affected_share may sit from the share the weights give.
# Files write three decimals, so 0.0005 is one rounding step.
AFFECTED_SHARE_TOLERANCE = 0.0005


class Baseline(BaseModel):
    """How much ordinary traffic the run emits, before any fault."""

    model_config = ConfigDict(extra="forbid")

    rps: float = Field(gt=0)
    minutes: float = Field(gt=0)
    # Multiplies every span's log-normal sigma. 1.0 (the default) is the
    # ordinary spread; a control can widen it to look noisier without moving
    # any span's own median, which is a different thing from a fault stepping
    # a median at onset. The root span's median still shifts, because its
    # duration sums its own time and every descendant's, and a sum of wider
    # log-normals has a heavier right tail; see gen/README.md.
    sigma_scale: float = Field(default=1.0, gt=0)

    @property
    def request_count(self) -> int:
        return int(round(self.rps * self.minutes * 60.0))

    @model_validator(mode="after")
    def _check(self) -> Baseline:
        if self.request_count < 1:
            raise ValueError(f"rps {self.rps} over {self.minutes} minutes rounds to zero requests")
        return self


class ExceptionEffect(BaseModel):
    """A span event an effect attaches to the spans it fails.

    Named `ExceptionEffect` rather than `Exception` so it does not shadow the
    builtin. Only valid on an effect that also raises the error rate: an
    exception event without a failure to explain would not mean anything.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    message: str


class Effect(BaseModel):
    """What a fault does to one span: adds latency, makes it fail, or both.

    `timeout_ms` and `latency_add_ms` are exclusive: a timeout replaces the
    span's own time outright, exactly, because a deadline does not jitter,
    where `latency_add_ms` adds a jittered amount on top of the ordinary
    draw. Both `timeout_ms` and `error_type` and `exception` only mean
    anything on a span that actually failed, so each requires `error_rate`
    above zero.
    """

    model_config = ConfigDict(extra="forbid")

    span: str
    latency_add_ms: float = Field(default=0.0, ge=0)
    error_rate: float = Field(default=0.0, ge=0, le=1)
    timeout_ms: float | None = Field(default=None, gt=0)
    error_type: str | None = None
    exception: ExceptionEffect | None = None

    @model_validator(mode="after")
    def _check(self) -> Effect:
        if self.span not in topology.SPAN_NAMES:
            raise ValueError(
                f"unknown span {self.span!r}; the topology has {list(topology.SPAN_NAMES)}"
            )
        if not self.latency_add_ms and not self.error_rate:
            raise ValueError("an effect must add latency, raise the error rate, or both")
        if self.timeout_ms is not None:
            if self.error_rate <= 0:
                raise ValueError(
                    "effect.timeout_ms requires error_rate > 0: only a failing span times out"
                )
            if self.latency_add_ms:
                raise ValueError(
                    "effect.timeout_ms and effect.latency_add_ms are exclusive: a timeout "
                    "replaces the span's own time, it does not add to it"
                )
        if self.error_type is not None and self.error_rate <= 0:
            raise ValueError("effect.error_type requires error_rate > 0")
        if self.exception is not None and self.error_rate <= 0:
            raise ValueError("effect.exception requires error_rate > 0")
        return self


class Fault(BaseModel):
    """The incident: which requests it hits, from which minute, and what it does."""

    model_config = ConfigDict(extra="forbid")

    onset_min: float = Field(ge=0)
    where: dict[str, str] = Field(min_length=1)
    effect: Effect


class RedHerring(BaseModel):
    """A second, weaker effect that is not the incident.

    Defaults to `onset_min: 0`, so it is present for the whole window and a
    before-and-after comparison separates it from the fault. `duration_min`
    is None by default, meaning "the rest of the window"; set it to make the
    herring a burst that starts at `onset_min` and stops on its own.
    """

    model_config = ConfigDict(extra="forbid")

    where: dict[str, str] = Field(min_length=1)
    effect: Effect
    onset_min: float = Field(default=0.0, ge=0)
    duration_min: float | None = Field(default=None, gt=0)
    note: str = ""


class GroundTruth(BaseModel):
    """The answer the grader scores against."""

    model_config = ConfigDict(extra="forbid")

    incident_present: bool
    root_cause_dims: dict[str, str] = Field(default_factory=dict)
    slow_or_failing_span: str | None = None
    affected_share: float | None = Field(default=None, ge=0, le=1)


class Trigger(BaseModel):
    """A Honeycomb trigger the scenario expects to fire.

    Created by hand in the Honeycomb UI (browser work is the orchestrating
    session's job, not this loader's), and named here so `gen/verify.py` can
    confirm it actually fired. The UI enforces a rule on top of these fields:
    a trigger's duration may be at most 4x its frequency, and the frequency is
    whole minutes, so a 5 minute window runs every 2 minutes at the fastest.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    threshold_ms: float = Field(gt=0)
    window_min: float = Field(gt=0)
    frequency_s: float = Field(gt=0)
    note: str = ""


class Scenario(BaseModel):
    """One scenario file."""

    model_config = ConfigDict(extra="forbid")

    id: str
    narrative: str
    baseline: Baseline
    ground_truth: GroundTruth
    fault: Fault | None = None
    red_herrings: list[RedHerring] = Field(default_factory=list)
    dimensions: dict[str, dict[str, float]] = Field(default_factory=dict)
    trigger: Trigger | None = None

    @model_validator(mode="after")
    def _check(self) -> Scenario:
        self._check_dimension_overrides()
        self._check_where_clauses()
        self._check_timing()
        self._check_red_herrings()
        self._check_ground_truth()
        return self

    def _check_timing(self) -> None:
        if self.fault is None:
            return
        onset = self.fault.onset_min
        if not 0 < onset < self.baseline.minutes:
            raise ValueError(
                f"fault.onset_min must fall strictly inside the window, got {onset} "
                f"in a {self.baseline.minutes} minute run; the verifier needs traffic "
                "on both sides of onset"
            )

    def _check_red_herrings(self) -> None:
        for i, herring in enumerate(self.red_herrings):
            label = f"red_herrings[{i}]"
            if herring.onset_min >= self.baseline.minutes:
                raise ValueError(f"{label}.onset_min is past the end of the window")
            if herring.duration_min is not None:
                herring_end = herring.onset_min + herring.duration_min
                if herring_end > self.baseline.minutes:
                    raise ValueError(
                        f"{label}.onset_min + duration_min is {herring_end}, past the end of "
                        f"the {self.baseline.minutes} minute window"
                    )
            if self.fault is None:
                continue
            if herring.onset_min >= self.fault.onset_min:
                raise ValueError(
                    f"{label} starts at minute {herring.onset_min}, at or after the fault's "
                    f"{self.fault.onset_min}; a red herring has to be older than the incident"
                )
            if herring.where == self.fault.where and herring.effect.span == self.fault.effect.span:
                raise ValueError(f"{label} hits the same population and span as the fault")
            if (
                self.fault.effect.timeout_ms is not None
                and herring.effect.span == self.fault.effect.span
                and herring.effect.latency_add_ms
            ):
                raise ValueError(
                    f"{label} adds latency to {herring.effect.span!r}, the same span "
                    "fault.effect.timeout_ms replaces outright; for any request the herring "
                    "also matches, its jitter would land on top of what is supposed to be an "
                    "exact deadline"
                )

    def _check_dimension_overrides(self) -> None:
        for name, weights in self.dimensions.items():
            if name == "customer.id":
                if not weights:
                    raise ValueError("dimension 'customer.id' needs at least one pinned value")
                if any(weight <= 0 for weight in weights.values()):
                    raise ValueError("dimension 'customer.id' has a non-positive weight")
                pinned_total = sum(weights.values())
                if pinned_total >= 1.0:
                    raise ValueError(
                        f"dimension 'customer.id' pins {pinned_total:.3f} of traffic, which "
                        "leaves nothing for the rest of the customers; pins must sum to less "
                        "than 1.0"
                    )
                continue
            if name not in topology.DIMENSION_WEIGHTS:
                known = [*topology.DIMENSION_WEIGHTS, "customer.id"]
                raise ValueError(f"unknown dimension {name!r}; the generator emits {known}")
            if not weights:
                raise ValueError(f"dimension {name!r} needs at least one value")
            if any(weight <= 0 for weight in weights.values()):
                raise ValueError(f"dimension {name!r} has a non-positive weight")

    def _check_where_clauses(self) -> None:
        weights = topology.dimension_weights_for(self)
        clauses = [("fault", self.fault.where, self.fault.effect.span)] if self.fault else []
        clauses += [
            (f"red_herrings[{i}]", h.where, h.effect.span) for i, h in enumerate(self.red_herrings)
        ]
        for label, where, span in clauses:
            for name, value in where.items():
                self._check_where_clause(label, name, str(value), span, weights)

    def _check_where_clause(
        self, label: str, name: str, value: str, span: str, weights: dict[str, dict[str, float]]
    ) -> None:
        if name == "service.component":
            span_service = topology.SPAN_SERVICE.get(span)
            if value != span_service:
                raise ValueError(
                    f"{label}.where names service.component={value!r}, which is not the "
                    f"service that runs {span!r} ({span_service!r})"
                )
            return
        if name == "customer.id":
            pinned = self.dimensions.get("customer.id", {})
            if value not in pinned:
                raise ValueError(
                    f"{label}.where wants customer.id={value!r}, which is not pinned in "
                    "dimensions.customer.id; pin it there first"
                )
            if not topology.span_carries(span, name):
                raise ValueError(f"{label}.where names customer.id but {span!r} does not carry it")
            return
        if name in topology.NUMERIC_RANGE_DIMS:
            if not topology.span_carries(span, name):
                raise ValueError(f"{label}.where names {name!r} but {span!r} does not carry it")
            if topology.RANGE_RE.match(value) is None and value not in topology.cart_size_weights():
                raise ValueError(
                    f"{label}.where gives {name} the value {value!r}, which is neither a "
                    "range like '>=8' nor a value the generator emits"
                )
            return
        if name not in weights:
            known = [*weights, *topology.NUMERIC_RANGE_DIMS, "customer.id", "service.component"]
            raise ValueError(
                f"{label}.where names {name!r}, which the generator does not emit; it emits {known}"
            )
        if value not in weights[name]:
            raise ValueError(
                f"{label}.where wants {name}={value!r}, which this scenario never "
                f"emits; its values are {sorted(weights[name])}"
            )

    def _check_ground_truth(self) -> None:
        truth = self.ground_truth
        if truth.incident_present:
            if self.fault is None:
                raise ValueError("ground_truth.incident_present is true but there is no fault")
            if truth.root_cause_dims != self.fault.where:
                raise ValueError(
                    "ground_truth.root_cause_dims must repeat fault.where exactly; "
                    f"got {truth.root_cause_dims} against {self.fault.where}"
                )
            if truth.slow_or_failing_span != self.fault.effect.span:
                raise ValueError(
                    "ground_truth.slow_or_failing_span must name fault.effect.span; "
                    f"got {truth.slow_or_failing_span!r} against {self.fault.effect.span!r}"
                )
            if truth.affected_share is None:
                raise ValueError("a scenario with an incident needs ground_truth.affected_share")
            expected = self.expected_affected_share()
            if abs(truth.affected_share - expected) > AFFECTED_SHARE_TOLERANCE:
                raise ValueError(
                    f"ground_truth.affected_share is {truth.affected_share} but the dimension "
                    f"weights give {expected:.4f} for {self.fault.where}"
                )
        else:
            if self.fault is not None:
                raise ValueError("ground_truth.incident_present is false but a fault is defined")
            if truth.root_cause_dims or truth.slow_or_failing_span:
                raise ValueError("a control scenario must not name a root cause or a span")
            if truth.affected_share is not None:
                raise ValueError("a control scenario has no affected_share")

    @property
    def onset_min(self) -> float | None:
        return self.fault.onset_min if self.fault else None

    def expected_affected_share(self) -> float:
        """The share of requests the fault's `where` clause selects, from the weights.

        This is the population share over the whole window, not the share of
        requests that were actually slowed. Onset splits the window; the
        population does not change across it.
        """
        if self.fault is None:
            return 0.0
        return topology.Sampler(self).population_share(self.fault.where)


def scenario_path(scenario_id: str) -> Path:
    return SCENARIO_DIR / f"{scenario_id}.yml"


def load_scenario(scenario_id: str) -> Scenario:
    """Load one scenario by id. The id must match the file name."""
    path = scenario_path(scenario_id)
    if not path.exists():
        known = ", ".join(available_scenarios()) or "(none)"
        raise FileNotFoundError(f"no scenario {scenario_id!r} in {SCENARIO_DIR}; have: {known}")
    return load_scenario_file(path)


def load_scenario_file(path: Path) -> Scenario:
    """Load and validate one scenario file."""
    data: Any = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a YAML mapping")
    scenario = Scenario.model_validate(data)
    if scenario.id != path.stem:
        raise ValueError(f"{path.name} declares id {scenario.id!r}; it must match the file name")
    return scenario


def available_scenarios() -> list[str]:
    """Ids of every scenario file on disk, sorted."""
    return sorted(path.stem for path in SCENARIO_DIR.glob("*.yml"))


def load_all() -> list[Scenario]:
    """Every scenario on disk, sorted by id."""
    return [load_scenario(scenario_id) for scenario_id in available_scenarios()]
