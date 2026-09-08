"""Tests for agent/report.py: the schema the model fills in and the file it writes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.report import (
    SCHEMA_REJECTION,
    Evidence,
    Hypothesis,
    PartialCheck,
    RejectedCandidate,
    Report,
    ReportDraft,
    ToolCall,
    current_schema_hash,
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
    assert "partially_checked" in schema["properties"]
    assert schema["required"] == ["incident_present"]
    # Every field the model has to reason about carries prose describing it.
    assert all("description" in prop for prop in schema["properties"].values())


def test_the_submit_report_schema_carries_reading_and_measurement() -> None:
    schema = submit_report_schema()
    defs = schema.get("$defs", {})
    partial_check = defs.get("PartialCheck")
    assert partial_check is not None
    assert "reading" in partial_check["properties"]
    assert "measurement" in partial_check["properties"]
    assert partial_check["required"] == ["subject", "queried_as", "reading", "not_run"]


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


def test_the_schema_hash_is_twelve_hex_characters_and_stable() -> None:
    assert re.fullmatch(r"[0-9a-f]{12}", current_schema_hash())
    assert current_schema_hash() == current_schema_hash()


def test_a_report_written_before_the_schema_hash_loads_as_none() -> None:
    report = Report.model_validate(
        {"run_id": "run-old", "scenario_id": "s", "provider": "fake", "model": "fake-model"}
    )
    assert report.schema_hash is None


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


# --------------------------------------------------------------------------
# A JSON-encoded list or object in place of the list or dict submit_report wants
# --------------------------------------------------------------------------


def test_hypotheses_as_a_json_string_validates_to_the_same_report_as_the_list_form() -> None:
    hypotheses = [
        {
            "claim": "the checkout path got slow",
            "dims": {"a.b": "1"},
            "slow_or_failing_span": "some.span",
            "confidence": "high",
            "evidence": [{"query_id": "Q1", "summary": "P99 went from 180ms to 1100ms"}],
            "negation": {"query_id": "Q2", "summary": "P99 flat outside the population"},
        }
    ]
    as_list = draft(hypotheses=hypotheses)
    as_json_string = draft(hypotheses=json.dumps(hypotheses))
    assert as_list == as_json_string


def test_a_non_json_string_for_hypotheses_still_fails_with_the_original_error() -> None:
    with pytest.raises(ValidationError, match="hypotheses"):
        draft(hypotheses="not json at all")


def test_a_json_string_of_the_wrong_container_type_still_fails() -> None:
    """A JSON object where a list is wanted is not coerced; it falls through
    to pydantic's normal type error."""
    with pytest.raises(ValidationError, match="hypotheses"):
        draft(hypotheses=json.dumps({"claim": "not a list"}))


def test_dims_and_evidence_and_negation_as_json_strings_coerce() -> None:
    hypothesis = Hypothesis.model_validate(
        {
            "claim": "x",
            "dims": json.dumps({"a.b": "1"}),
            "confidence": "low",
            "evidence": json.dumps([{"query_id": "Q1", "summary": "rows"}]),
            "negation": json.dumps({"query_id": "Q2", "summary": "flat outside"}),
        }
    )
    assert hypothesis.dims == {"a.b": "1"}
    assert hypothesis.evidence == [Evidence(query_id="Q1", summary="rows")]
    assert hypothesis.negation == Evidence(query_id="Q2", summary="flat outside")


def test_a_json_string_evidence_of_the_wrong_container_type_still_fails() -> None:
    with pytest.raises(ValidationError):
        Hypothesis.model_validate(
            {
                "claim": "x",
                "dims": {},
                "confidence": "low",
                "evidence": json.dumps({"query_id": "Q1", "summary": "not a list"}),
            }
        )


def test_the_coercion_is_recorded_in_the_validation_context() -> None:
    context: dict[str, object] = {}
    ReportDraft.model_validate(
        {
            "incident_present": True,
            "hypotheses": json.dumps(
                [
                    {
                        "claim": "x",
                        "dims": json.dumps({"a.b": "1"}),
                        "confidence": "low",
                        "evidence": [{"query_id": "Q1", "summary": "rows"}],
                    }
                ]
            ),
        },
        context=context,
    )
    assert "hypotheses" in context["coerced_fields"]  # type: ignore[operator]
    assert "dims" in context["coerced_fields"]  # type: ignore[operator]


def test_no_coercion_recorded_when_the_fields_were_already_the_right_shape() -> None:
    context: dict[str, object] = {}
    ReportDraft.model_validate({"incident_present": False}, context=context)
    assert "coerced_fields" not in context


# --------------------------------------------------------------------------
# Review fixes: RejectedCandidate.dims/.evidence, and a JSON null negation
# --------------------------------------------------------------------------


def test_rejected_candidate_dims_and_evidence_as_json_strings_coerce() -> None:
    candidate = RejectedCandidate.model_validate(
        {
            "claim": "the eu-west-1 db.query latency",
            "dims": json.dumps({"cloud.region": "eu-west-1"}),
            "reason": "present the whole window, not a step at onset",
            "evidence": json.dumps([{"query_id": "Q1", "summary": "flat before and after"}]),
        }
    )
    assert candidate.dims == {"cloud.region": "eu-west-1"}
    assert candidate.evidence == [Evidence(query_id="Q1", summary="flat before and after")]


def test_rejected_candidate_evidence_of_the_wrong_container_type_still_fails() -> None:
    with pytest.raises(ValidationError):
        RejectedCandidate.model_validate(
            {
                "claim": "x",
                "reason": "y",
                "evidence": json.dumps({"query_id": "Q1", "summary": "not a list"}),
            }
        )


def test_a_json_null_negation_coerces_to_none() -> None:
    """negation: Evidence | None accepts the JSON text of its own null case,
    not only a real object; a model that serialised the whole field as JSON
    should not be punished for the one case that means "no negation"."""
    hypothesis = Hypothesis.model_validate(
        {
            "claim": "x",
            "dims": {},
            "confidence": "low",
            "evidence": [{"query_id": "Q1", "summary": "rows"}],
            "negation": "null",
        }
    )
    assert hypothesis.negation is None


def test_a_json_null_negation_is_recorded_as_a_coercion() -> None:
    context: dict[str, object] = {}
    Hypothesis.model_validate(
        {
            "claim": "x",
            "dims": {},
            "confidence": "low",
            "evidence": [{"query_id": "Q1", "summary": "rows"}],
            "negation": "null",
        },
        context=context,
    )
    assert context["coerced_fields"] == ["negation"]  # type: ignore[comparison-overlap]


def test_a_non_null_non_json_negation_string_still_fails() -> None:
    with pytest.raises(ValidationError):
        Hypothesis.model_validate(
            {
                "claim": "x",
                "dims": {},
                "confidence": "low",
                "evidence": [{"query_id": "Q1", "summary": "rows"}],
                "negation": "not json and not an object",
            }
        )


# --------------------------------------------------------------------------
# EDW-1362: a stray wrapper key around the whole report
#
# checkout-error-surge-adyen repeat 4 called submit_report with the whole
# report nested under a stray "permalink" key. ReportDraft (extra="forbid")
# rejected the outer dict as missing incident_present and carrying an
# unknown permalink field, and an investigation that had found adyen filed
# nothing.
# --------------------------------------------------------------------------


def _valid_report_dict() -> dict[str, object]:
    return {
        "incident_present": True,
        "hypotheses": [
            {
                "claim": "the checkout path got slow",
                "dims": {"a.b": "1"},
                "slow_or_failing_span": "some.span",
                "confidence": "high",
                "evidence": [{"query_id": "Q1", "summary": "P99 went from 180ms to 1100ms"}],
                "negation": {"query_id": "Q2", "summary": "P99 flat outside the population"},
            }
        ],
        "affected_population": "12% of requests",
        "onset_estimate": "2026-09-03T02:47:20Z",
        "not_checked": ["cart.size"],
    }


def test_the_adyen_4_shape_validates_to_the_same_report_as_the_plain_form() -> None:
    plain = ReportDraft.model_validate(_valid_report_dict())
    context: dict[str, object] = {}
    wrapped = ReportDraft.model_validate({"permalink": _valid_report_dict()}, context=context)
    assert wrapped == plain
    assert context["coerced_fields"] == ["wrapper:permalink"]


# --------------------------------------------------------------------------
# EDW-1365: partially_checked, the third list between not_checked and
# rejected_candidates
# --------------------------------------------------------------------------


def test_partially_checked_round_trips_through_the_draft() -> None:
    check = PartialCheck(
        subject="cart.size",
        queried_as="broke down duration_ms by cart.size across the window",
        reading="per_value",
        not_run="did not look at individual values below 8 for a threshold",
    )
    parsed = draft(partially_checked=[check])
    assert parsed.partially_checked == [check]


def test_partially_checked_as_a_json_string_coerces() -> None:
    entries = [
        {
            "subject": "cart.size",
            "queried_as": "broke down duration_ms by cart.size",
            "reading": "per_value",
            "not_run": "did not look at values below 8",
        }
    ]
    as_list = draft(partially_checked=entries)
    as_json_string = draft(partially_checked=json.dumps(entries))
    assert as_list == as_json_string


def test_partially_checked_forbids_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        PartialCheck.model_validate(
            {
                "subject": "cart.size",
                "queried_as": "x",
                "reading": "per_value",
                "not_run": "y",
                "extra": "z",
            }
        )


def test_partially_checked_requires_a_reading() -> None:
    with pytest.raises(ValidationError):
        PartialCheck.model_validate({"subject": "cart.size", "queried_as": "x", "not_run": "y"})


def test_partially_checked_other_measurement_accepts_a_measurement() -> None:
    check = PartialCheck(
        subject="duration_ms",
        queried_as="broke down by name",
        reading="other_measurement",
        measurement="P99(duration_ms)",
        not_run="did not run P99 on it, only COUNT",
    )
    assert draft(partially_checked=[check]).partially_checked == [check]


def test_partially_checked_round_trips_through_report_write_and_load(tmp_path: Path) -> None:
    check = PartialCheck(
        subject="exception.type",
        queried_as="broke down errors by exception.type",
        reading="per_value",
        not_run="did not read exception.message for each type",
    )
    report = Report.from_draft(
        draft(partially_checked=[check]),
        run_id="run-partial",
        scenario_id="s",
        provider="fake",
        model="fake-model",
    )
    path = report.write(tmp_path)
    reloaded = load_report(path)
    assert reloaded.partially_checked == [check]


def test_a_two_key_wrapper_still_fails() -> None:
    with pytest.raises(ValidationError):
        ReportDraft.model_validate({"permalink": _valid_report_dict(), "extra": "x"})


def test_a_wrapper_whose_inner_dict_lacks_incident_present_still_fails() -> None:
    with pytest.raises(ValidationError):
        ReportDraft.model_validate({"permalink": {"hypotheses": []}})


def test_a_one_key_input_whose_value_is_not_a_dict_still_fails() -> None:
    with pytest.raises(ValidationError):
        ReportDraft.model_validate({"permalink": "not a dict at all"})


# --------------------------------------------------------------------------
# The schema stop reason, and the files written before it existed
# --------------------------------------------------------------------------


def _process(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "run_id": "run-old",
        "scenario_id": "control-quiet",
        "provider": "test",
        "model": "m",
    }
    base.update(overrides)
    return base


def test_an_old_schema_rejection_loads_as_a_schema_stop() -> None:
    """Before the `schema` stop reason, a second schema rejection was recorded
    as `report` with the rejection as the first message. It reads back as
    what it was."""
    r = Report.model_validate(
        _process(
            stop_reason="report",
            validation_failed=True,
            validation_messages=[SCHEMA_REJECTION + "\n1 validation error for ReportDraft"],
        )
    )
    assert r.stop_reason == "schema"


@pytest.mark.parametrize(
    "fields",
    [
        {"stop_reason": "report", "validation_failed": True, "validation_messages": ["partial: x"]},
        {"stop_reason": "report", "validation_failed": False, "validation_messages": []},
        {"stop_reason": "report", "validation_failed": True, "validation_messages": []},
        {"stop_reason": "wall_cap", "validation_failed": False, "validation_messages": []},
    ],
)
def test_other_reports_keep_their_stop_reason(fields: dict[str, object]) -> None:
    r = Report.model_validate(_process(**fields))
    assert r.stop_reason == fields["stop_reason"]
