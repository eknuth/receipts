"""Formatting tests, against fixtures captured from live Honeycomb MCP calls
(and two synthesized fixtures for cases the live smoke dataset is too small
to exercise: the row and span caps). See tests/fixtures/mcp/*.json.
"""

import json
from pathlib import Path

from agent.format import (
    MAX_JSON_BYTES,
    extract_ids,
    format_error,
    format_tool_result,
    parse_metadata_block,
    truncate,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mcp"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def text_of(fixture: dict) -> str:
    return "\n".join(fixture["content_texts"])


def test_parse_metadata_block_strips_quotes_and_stops_at_blank_line() -> None:
    fixture = load("run_query")
    metadata = parse_metadata_block(text_of(fixture))
    assert metadata["query_run_pk"] == "JsqoYo7iPFW"
    assert metadata["query_url"].startswith("https://ui.honeycomb.io/")
    assert "Dataset semantics" not in metadata  # the block after Metadata: is not absorbed


def test_extract_ids_run_query() -> None:
    fixture = load("run_query")
    query_id, permalink = extract_ids(text_of(fixture))
    assert query_id == "JsqoYo7iPFW"
    assert (
        permalink
        == "https://ui.honeycomb.io/acme-team/environments/receipts-demo/datasets/receipts-smoke/result/JsqoYo7iPFW"
    )


def test_extract_ids_get_trace() -> None:
    fixture = load("get_trace")
    query_id, permalink = extract_ids(text_of(fixture))
    assert query_id == "xzqzcEtfJ7z"
    assert "trace_id=5b8aa5a2d2c872e8321cf37308d69df2" in permalink


def test_extract_ids_absent_for_workspace_context() -> None:
    fixture = load("get_workspace_context")
    query_id, permalink = extract_ids(text_of(fixture))
    assert query_id is None
    assert permalink is None


def test_format_run_query_live_fixture() -> None:
    fixture = load("run_query")
    rendered = format_tool_result("run_query", text_of(fixture), args=fixture["args"])
    assert "run_query on receipts-smoke" in rendered
    assert "calculations=[COUNT]" in rendered
    assert "breakdowns=[service.name]" in rendered
    assert "COUNT" in rendered and "service.name" in rendered
    assert "receipts-smoke" in rendered
    assert "query_id: JsqoYo7iPFW" in rendered
    assert "permalink: https://ui.honeycomb.io/" in rendered


def test_format_run_query_caps_at_25_rows() -> None:
    fixture = load("run_query_synthetic_30rows")
    rendered = format_tool_result("run_query", text_of(fixture), args=fixture["args"])
    lines = rendered.splitlines()
    data_lines = [ln for ln in lines if "customer-" in ln]
    assert len(data_lines) == 25
    assert "... 5 more rows" in rendered
    assert "filters=[cloud.region = us-west-2]" in rendered


def test_format_run_bubbleup_live_fixture() -> None:
    fixture = load("run_bubbleup")
    rendered = format_tool_result("run_bubbleup", text_of(fixture))
    assert rendered.startswith("Top 10 of 12 differentiators")
    assert "duration_ms" in rendered
    assert "baseline 0.0% -> selection 100.0%" in rendered
    assert "... 2 more columns" in rendered
    assert "query_id: ig2gXHFcbfm" in rendered
    assert "permalink: https://ui.honeycomb.io/" in rendered


def test_format_get_trace_live_fixture_single_span() -> None:
    fixture = load("get_trace")
    rendered = format_tool_result("get_trace", text_of(fixture))
    assert "trace 5b8aa5a2d2c872e8321cf37308d69df2 (1 spans)" in rendered
    assert "day0.smoke (receipts-smoke) 5.0ms" in rendered
    assert "query_id: xzqzcEtfJ7z" in rendered


def test_format_get_trace_caps_at_60_spans_and_indents_by_depth() -> None:
    fixture = load("get_trace_synthetic_70spans")
    rendered = format_tool_result("get_trace", text_of(fixture))
    assert "trace synthetic0000000000000000000001 (70 spans)" in rendered
    assert "... 10 more" in rendered
    lines = rendered.splitlines()
    # depth-3 spans (inventory-db) are indented three levels (6 spaces) deeper
    # than depth-0 spans (gateway).
    gateway_line = next(ln for ln in lines if "gateway.op" in ln)
    inventory_line = next(ln for ln in lines if "inventory-db.op" in ln)
    assert gateway_line.startswith("gateway.op")
    assert inventory_line.startswith("      inventory-db.op")


def test_format_get_trace_flags_error_span() -> None:
    fixture = load("get_trace_synthetic_70spans")
    rendered = format_tool_result("get_trace", text_of(fixture))
    error_lines = [ln for ln in rendered.splitlines() if ln.rstrip().endswith("ERROR")]
    assert len(error_lines) == 1
    assert "payments.op" in error_lines[0]


def test_format_falls_back_to_json_for_unrecognized_tool() -> None:
    payload = {"team": {"name": "acme"}, "environments": [{"slug": "prod"}]}
    rendered = format_tool_result("get_workspace_context", payload)
    parsed_back = json.loads(rendered)
    assert parsed_back == payload


def test_format_truncates_to_4kb_with_ascii_marker() -> None:
    payload = {"columns": [{"name": f"col_{i}", "type": "string"} for i in range(500)]}
    rendered = format_tool_result("get_dataset_columns", payload)
    assert len(rendered.encode("utf-8")) <= MAX_JSON_BYTES + 100
    assert "..." in rendered
    assert "…" not in rendered  # ASCII "...", never the unicode ellipsis
    assert "truncated" in rendered


def test_format_text_payload_under_4kb_is_returned_verbatim() -> None:
    fixture = load("get_dataset_columns")
    rendered = format_tool_result("get_dataset_columns", text_of(fixture))
    assert "duration_ms" in rendered
    assert len(text_of(fixture).encode("utf-8")) <= MAX_JSON_BYTES
    assert "truncated" not in rendered


def test_escaped_pipe_in_a_cell_is_kept() -> None:
    text = "# Results\n\n| COUNT | http.route |\n| --- | --- |\n| 3 | /a\\|b |\n"
    out = format_tool_result("run_query", text, args={"dataset_slug": "d"})
    assert "/a|b" in out


def test_fallback_strips_the_time_series_chart() -> None:
    fixture = load("run_query")
    text = text_of(fixture).replace("# Results", "# Unexpected heading")
    out = format_tool_result("run_query", text, args={})
    assert "# Time Series" not in out
    assert "\u2502" not in out
    assert "query_run_pk" in out


def test_empty_text_renders_as_empty_result() -> None:
    assert format_tool_result("get_dataset", "", args={}) == "(empty result)"


def test_truncation_cuts_at_a_line_boundary() -> None:
    line = "x" * 99 + "\n"
    text = line * 60
    out = truncate(text, MAX_JSON_BYTES)
    body = out.split("\n... truncated")[0]
    assert len(body.encode()) <= MAX_JSON_BYTES
    assert all(len(ln) == 99 for ln in body.split("\n"))


def test_query_spec_with_start_and_end_time_is_described() -> None:
    args = {
        "dataset_slug": "receipts-shop",
        "query_spec": {
            "calculations": [{"op": "COUNT"}],
            "start_time": 1700000000,
            "end_time": 1700001800,
        },
    }
    text = "# Results\n\n| COUNT |\n| --- |\n| 1 |\n"
    out = format_tool_result("run_query", text, args=args)
    assert "time_range=1700000000..1700001800" in out


def test_format_error_names_the_tool_and_message() -> None:
    assert format_error("run_query", "Invalid or missing dataset: nope") == (
        "run_query failed: Invalid or missing dataset: nope"
    )
    assert format_error("get_trace", "  ") == "get_trace failed: (no message)"
