"""Nothing the model reads may carry a scenario's answer.

The scenario id was already kept off the wire and out of the prompt. This
file widens that line to every ground-truth value and every fault column the
scenario files name, plus a hand-typed list of words the method must not
mention, and checks every surface the model reads: the rendered system prompt,
the `submit_report` tool schema and its description, and every string literal
in `agent/validate.py`, which is where the rejection messages the model gets
back are written. Pydantic copies class docstrings and `Field(description=...)`
text into the schema, so an example in a docstring is an example the model
sees. This is the one place that defines what the model may not be told.
"""

from __future__ import annotations

import ast
import inspect
import json
import re

import agent.validate
from agent.loop import SUBMIT_REPORT_DESCRIPTION, AgentConfig, ScenarioRun, render_prompt
from agent.report import submit_report_schema
from agent.validate import _COMPLEMENT_OP
from gen.scenario import load_all
from gen.topology import RANGE_RE

RUN = ScenarioRun(
    run_id="run-leak-check",
    scenario_id="control-quiet",
    dataset="receipts-shop",
    environment="receipts-demo",
    window_start="2026-09-03T02:37:20Z",
    window_end="2026-09-03T02:57:20Z",
)

# Words the method must not use, whatever the scenario files say. Service
# names, provider names, region prefixes, and version strings: naming one in
# the prompt would be tuning to the data. Folded in from the prompt test that
# used to carry this list on its own.
BANNED_WORDS: tuple[str, ...] = (
    "payments",
    "stripe",
    "adyen",
    "paypal",
    "us-west",
    "eu-west",
    "2.5.1",
    "2.6.0",
    "checkout",
    "inventory",
    "scenario.id",
)

# Fault columns that are generic enough to name. `name` is the span name
# column every trace dataset has; `equivalent_dims` uses it to say db.query
# selects the same population as its service, and the prompt has to be able
# to say "the span name" without that being a hint.
GENERIC_COLUMNS: frozenset[str] = frozenset({"name"})


def _ground_truth_values() -> set[str]:
    values: set[str] = set()
    for scenario in load_all():
        values.update(scenario.ground_truth.root_cause_dims.values())
        for dims in scenario.ground_truth.equivalent_dims:
            values.update(dims.values())
        if scenario.ground_truth.slow_or_failing_span:
            values.add(scenario.ground_truth.slow_or_failing_span)
        if scenario.fault is not None:
            values.update(scenario.fault.where.values())
        for herring in scenario.red_herrings:
            values.update(herring.where.values())
    return {str(v).lower() for v in values if not isinstance(v, bool)}


def _fault_columns() -> set[str]:
    """Every column a scenario selects on: root cause, equivalent, or red herring."""
    keys: set[str] = set()
    for scenario in load_all():
        keys.update(scenario.ground_truth.root_cause_dims)
        for dims in scenario.ground_truth.equivalent_dims:
            keys.update(dims)
        if scenario.fault is not None:
            keys.update(scenario.fault.where)
        for herring in scenario.red_herrings:
            keys.update(herring.where)
    return {k.lower() for k in keys} - GENERIC_COLUMNS


def _bound_phrases(value: str) -> list[str]:
    """The ways a range bound like `>=8` can be written into prose.

    The complement at the same bound counts too: `< 8` gives away `>= 8`
    just as well, and the validator's own messages talk in complements.
    """
    match = RANGE_RE.match(value)
    if match is None:
        return []
    op, bound = match.group(1), match.group(2)
    complement = _COMPLEMENT_OP[op]
    phrases = [
        f"{op}{bound}",
        f"{op} {bound}",
        f"{complement}{bound}",
        f"{complement} {bound}",
        f"below {bound}",
        f"under {bound}",
        f"at least {bound}",
        f"{bound} and up",
        f"{bound} or more",
        f"{bound}+",
    ]
    if bound.isdigit():
        phrases.append(f"{int(bound) - 1} and below")
    return phrases


def _phrase_in(phrase: str, text: str) -> bool:
    """`phrase` in `text`, with the number not part of a longer number."""
    return re.search(rf"(?<![\d.]){re.escape(phrase)}(?![\d.])", text) is not None


def _validate_literals() -> str:
    """Every string literal in agent/validate.py, docstrings and messages both.

    Comments never reach the model and are not in the AST, so this reads the
    rejection message templates without dragging comments in.
    """
    tree = ast.parse(inspect.getsource(agent.validate))
    return "\n".join(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _surfaces() -> dict[str, str]:
    # The default config renders every optional block; `_OPTIONAL_BLOCK` only
    # ever removes text, so the default prompt is a superset of all four.
    return {
        "system prompt": render_prompt(RUN, AgentConfig()).lower(),
        "submit_report schema": json.dumps(submit_report_schema()).lower(),
        "submit_report description": SUBMIT_REPORT_DESCRIPTION.lower(),
        "agent/validate.py literals": _validate_literals().lower(),
    }


def test_the_scenarios_carry_values_to_check() -> None:
    values = _ground_truth_values()
    assert len(values) >= 10, values
    assert any(RANGE_RE.match(v) for v in values), "expected at least one range bound"
    assert _fault_columns() >= {"cloud.region", "payment.provider", "customer.id"}
    assert "name" not in _fault_columns()


def test_the_bound_phrases_cover_the_complement() -> None:
    phrases = _bound_phrases(">=8")
    assert {"<8", "< 8", ">=8", ">= 8", "at least 8", "8+", "7 and below"} <= set(phrases)
    assert _phrase_in("below 8", "did not look below 8 in that column")
    assert not _phrase_in("below 8", "did not look below 80 in that column")


def test_no_ground_truth_value_reaches_the_model() -> None:
    values = _ground_truth_values()
    leaks: list[str] = []
    for surface, text in _surfaces().items():
        for value in sorted(values):
            if value in text:
                leaks.append(f"{surface}: {value!r}")
            for phrase in _bound_phrases(value):
                if _phrase_in(phrase, text):
                    leaks.append(f"{surface}: {phrase!r} (bound of {value!r})")
    assert leaks == [], "\n".join(leaks)


def test_no_fault_column_reaches_the_model() -> None:
    keys = _fault_columns()
    leaks: list[str] = []
    for surface, text in _surfaces().items():
        for key in sorted(keys):
            if key in text:
                leaks.append(f"{surface}: {key!r}")
    assert leaks == [], "\n".join(leaks)


def test_no_banned_word_reaches_the_model() -> None:
    """The method is general. Naming a service or a provider would be tuning to the data."""
    leaks: list[str] = []
    for surface, text in _surfaces().items():
        for word in BANNED_WORDS:
            if word in text:
                leaks.append(f"{surface}: {word!r}")
    assert leaks == [], "\n".join(leaks)
