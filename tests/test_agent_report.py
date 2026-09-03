"""Tests for agent/report.py: the schema the model fills in and the file it writes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.report import (
    Evidence,
    Hypothesis,
    Report,
    ReportDraft,
    ToolCall,
    load_report,
    submit_report_schema,
)


def draft(**overrides: object) -> ReportDraft:
    base: dict[str, object] = {
        "incident_present": True,
        "hypotheses": [
            Hypothesis(
                claim="the checkout path got slow",
                dims={"a.b": "1"},
                slow_or_failing_span="some.span",
                confidence="high",
                evidence=[Evidence(query_id="Q1", summary="P99 went from 180ms to 1100ms")],
                negation=Evidence(query_id="Q2", summary="P99 flat outside the population"),
            )
        ],
        "affected_population": "12% of requests",
        "onset_estimate": datetime(2026, 9, 3, 2, 47, 20, tzinfo=UTC),
        "not_checked": ["cart.size"],
    }
    base.update(overrides)
    return ReportDraft.model_validate(base)


def test_the_draft_carries_no_process_fields() -> None:
    """A model that could write tool_calls could write any number it liked."""
    fields = set(ReportDraft.model_fields)
    assert fields.isdisjoint({"tool_calls", "tokens_in", "tokens_out", "wall_s", "cost_usd"})


def test_an_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ReportDraft.model_validate({"incident_present": True, "root_cause": "guessing"})


def test_confidence_is_one_of_three_levels() -> None:
    with pytest.raises(ValidationError):
        Hypothesis.model_validate(
            {"claim": "x", "confidence": "certain", "evidence": [], "dims": {}}
        )


def test_the_submit_report_schema_matches_the_draft() -> None:
    schema = submit_report_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(ReportDraft.model_fields)
    assert schema["required"] == ["incident_present"]
    # Every field the model has to reason about carries prose describing it.
    assert all("description" in prop for prop in schema["properties"].values())


def test_the_schema_is_json_serialisable() -> None:
    """It goes on the wire as a tool definition, so it has to survive json.dumps."""
    assert json.loads(json.dumps(submit_report_schema()))


def test_from_draft_merges_findings_and_process_fields() -> None:
    report = Report.from_draft(
        draft(),
        run_id="run-1",
        scenario_id="s",
        provider="fake",
        model="fake-model",
        tool_calls=7,
        tokens_in=100,
        tokens_out=20,
        wall_s=3.5,
        cost_usd=0.001,
        tool_log=[ToolCall(name="run_query", query_id="Q1", t=1.0)],
    )
    assert report.incident_present is True
    assert report.hypotheses[0].evidence[0].query_id == "Q1"
    assert report.tool_calls == 7
    assert report.tool_log[0].name == "run_query"


def test_write_lands_at_run_id_report_json_and_reads_back(tmp_path: Path) -> None:
    report = Report.from_draft(
        draft(), run_id="run-abc", scenario_id="s", provider="fake", model="fake-model"
    )
    path = report.write(tmp_path)

    assert path == tmp_path / "run-abc" / "report.json"
    reloaded = load_report(path)
    assert reloaded.hypotheses[0].claim == "the checkout path got slow"
    assert reloaded.onset_estimate == datetime(2026, 9, 3, 2, 47, 20, tzinfo=UTC)


def test_onset_estimate_accepts_the_iso_string_a_model_would_send() -> None:
    parsed = draft(onset_estimate="2026-09-03T02:47:20Z")
    assert parsed.onset_estimate == datetime(2026, 9, 3, 2, 47, 20, tzinfo=UTC)


def test_a_second_report_for_the_same_run_does_not_overwrite_the_first(tmp_path: Path) -> None:
    """Repeats of one scenario share a run id. Overwriting them loses the
    spread that repeats exist to measure."""
    first = Report(run_id="run-abc123", scenario_id="s", provider="fake", model="m")
    second = Report(run_id="run-abc123", scenario_id="s", provider="fake", model="m", tool_calls=7)

    first_path = first.write(tmp_path)
    second_path = second.write(tmp_path)

    assert first_path.name == "report.json"
    assert second_path.name == "report-2.json"
    assert json.loads(first_path.read_text())["tool_calls"] == 0
    assert json.loads(second_path.read_text())["tool_calls"] == 7


def test_overwrite_keeps_the_plain_name(tmp_path: Path) -> None:
    report = Report(run_id="run-abc123", scenario_id="s", provider="fake", model="m")
    assert report.write(tmp_path).name == "report.json"
    assert report.write(tmp_path, overwrite=True).name == "report.json"
    assert not (tmp_path / "run-abc123" / "report-2.json").exists()
