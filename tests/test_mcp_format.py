"""Formatting tests, against fixtures captured from live Honeycomb MCP calls
(and two synthesized fixtures for cases the live smoke dataset is too small
to exercise: the row and span caps). See tests/fixtures/mcp/*.json.
"""

import copy
import json
import logging
import re
from pathlib import Path

from agent.format import (
    MAX_JSON_BYTES,
    extract_bubbleup_result_id,
    extract_ids,
    format_error,
    format_tool_result,
    parse_column_types,
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


def test_extract_bubbleup_result_id_from_live_fixture() -> None:
    """The live server's Metadata block has no `bubbleup_result_id:` line
    (see the raw fixture): the id is the `bubbleup_result` query parameter
    on `bubble_up_url` instead."""
    fixture = load("run_bubbleup")
    result_id = extract_bubbleup_result_id(text_of(fixture))
    assert result_id == "ujVTPWn4uyd"

    # Distinct from the query it was built on, which is what query_id (via
    # extract_ids) reports for a run_bubbleup result.
    query_id, _ = extract_ids(text_of(fixture))
    assert query_id == "ig2gXHFcbfm"
    assert result_id != query_id


def test_extract_bubbleup_result_id_falls_back_to_a_literal_metadata_key() -> None:
    text = "# BubbleUp Analysis\n\n---\nMetadata:\n  bubbleup_result_id: legacy-id\n"
    assert extract_bubbleup_result_id(text) == "legacy-id"


def test_extract_bubbleup_result_id_absent_when_neither_source_is_present() -> None:
    fixture = load("run_query")
    assert extract_bubbleup_result_id(text_of(fixture)) is None


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


def test_query_spec_with_from_and_to_is_described() -> None:
    args = {
        "dataset_slug": "receipts-shop",
        "query_spec": {
            "calculations": [{"op": "COUNT"}],
            "from": 1700000000,
            "to": 1700001800,
        },
    }
    text = "# Results\n\n| COUNT |\n| --- |\n| 1 |\n"
    out = format_tool_result("run_query", text, args=args)
    assert "time_range=1700000000..1700001800" in out


def test_a_query_spec_recorded_with_the_old_start_and_end_time_names_still_renders() -> None:
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


def test_parse_column_types_reads_the_columns_table() -> None:
    fixture = load("get_dataset_columns")
    types = parse_column_types(text_of(fixture))
    assert types["duration_ms"] == "float"
    assert types["name"] == "string"
    assert types["span.num_events"] == "integer"


def test_parse_column_types_is_empty_with_no_table() -> None:
    assert parse_column_types("no table here, just prose") == {}


def test_format_error_names_the_tool_and_message() -> None:
    assert format_error("run_query", "Invalid or missing dataset: nope") == (
        "run_query failed: Invalid or missing dataset: nope"
    )
    assert format_error("get_trace", "  ") == "get_trace failed: (no message)"


# --- Time series (a `granularity` in the query spec) -----------------------
#
# The hosted MCP's only series data is an ASCII dot chart under a
# `# Time Series` heading (see the raw fixtures this module's fixtures were
# built from, captured live on 2026-09-05). `format_tool_result` reads that
# chart back into a compact bucket table. These fixtures share one scenario
# run (`run-cd1ec0dcfc51`): a P99 that steps up partway through the window.
#
# The chart's x axis spans its own first bucket's start to its own last
# bucket's start (named on the label line under the axis), not the query's
# `from`..`to`: the server can start its first bucket later than `from`,
# when data does, and can silently substitute a different granularity than
# the one asked for. The first and last bucket here hold a partial window's
# worth of data (53 s and 67 s of a 120 s bucket), which is why their COUNT
# reads below the ~1800/bucket a full bucket holds at 15 rps. The expected
# values below are read off the rendered table itself (recorded once,
# reviewed by hand against the raw chart) rather than recomputed
# independently, since the point of these tests is to pin the rendering,
# not to re-derive it.


def _series_table_rows(rendered: str) -> tuple[list[str], list[list[str]]]:
    """The `(headers, rows)` of the appended series table (space-aligned, not `|`-delimited)."""
    lines = rendered.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("bucket_start"))
    headers = re.split(r"\s{2,}", lines[start].rstrip())
    rows = []
    for ln in lines[start + 2 :]:
        if not ln.strip() or ln.startswith("...") or ln.startswith("one chart row is"):
            break
        rows.append(re.split(r"\s{2,}", ln.rstrip()))
    return headers, rows


def test_series_table_no_breakdown_120s() -> None:
    fixture = load("run_query_series_nobreak_120")
    rendered = format_tool_result("run_query", text_of(fixture), args=fixture["args"])
    headers, rows = _series_table_rows(rendered)

    assert headers == ["bucket_start", "COUNT", "P99(duration_ms)"]
    # The chart's own first and last buckets, from its label line: eleven
    # 120 s buckets from 06:30:00 to 06:50:00, not ten from 06:31:07.
    assert [row[0] for row in rows] == [
        "2026-09-04T06:30:00Z",
        "2026-09-04T06:32:00Z",
        "2026-09-04T06:34:00Z",
        "2026-09-04T06:36:00Z",
        "2026-09-04T06:38:00Z",
        "2026-09-04T06:40:00Z",
        "2026-09-04T06:42:00Z",
        "2026-09-04T06:44:00Z",
        "2026-09-04T06:46:00Z",
        "2026-09-04T06:48:00Z",
        "2026-09-04T06:50:00Z",
    ]
    # Baseline through 06:38:00, elevated from 06:40:00: the step sits
    # between them, matching the ground truth from two windowed queries
    # against the same run without granularity (P99 323.86 for
    # 06:31:07Z..06:41:07Z, 864.10 for 06:41:07Z..06:51:07Z).
    assert [row[2] for row in rows] == [
        "257",
        "318",
        "257",
        "318",
        "318",
        "804",
        "804",
        "804",
        "804",
        "865",
        "865",
    ]
    # COUNT's range in the chart header is integral ("696 - 1.9K") and one
    # chart row is worth 109 (>= 1), so values render as whole numbers. The
    # two edge buckets hold a partial window (53 s, 67 s) at 15 rps: well
    # below the ~1790/bucket the full buckets read.
    assert [row[1] for row in rows] == [
        "751",
        "1754",
        "1791",
        "1791",
        "1791",
        "1791",
        "1791",
        "1791",
        "1791",
        "1791",
        "915",
    ]


def test_series_table_resolution_line() -> None:
    fixture = load("run_query_series_nobreak_120")
    rendered = format_tool_result("run_query", text_of(fixture), args=fixture["args"])
    # (1900 - 696) / 11 = 109.5 -> 109; (926 - 257) / 11 = 60.8 -> 61.
    assert (
        "one chart row is COUNT 109, P99(duration_ms) 61; "
        "smaller moves are within the chart's resolution" in rendered
    )


def test_series_table_notes_a_granularity_substitution() -> None:
    """The server can silently substitute a granularity; when it does, say so."""
    fixture = load("run_query_series_nobreak_120")
    args = copy.deepcopy(fixture["args"])
    args["query_spec"]["granularity"] = 300  # the fixture's own Metadata says 120
    rendered = format_tool_result("run_query", text_of(fixture), args=args)
    assert "granularity 120 s (asked 300); one chart row is COUNT 109" in rendered


def test_series_table_breakdown_columns_sort_by_calculation_then_group() -> None:
    fixture = load("run_query_series_breakdown_version_120")
    rendered = format_tool_result("run_query", text_of(fixture), args=fixture["args"])
    headers, rows = _series_table_rows(rendered)

    assert headers == [
        "bucket_start",
        "COUNT - 2.5.0",
        "COUNT - 2.5.1",
        "COUNT - 2.6.1",
        "P99(duration_ms) - 2.5.0",
        "P99(duration_ms) - 2.5.1",
        "P99(duration_ms) - 2.6.1",
    ]
    assert len(rows) == 11

    def column(name: str) -> list[float]:
        idx = headers.index(name)
        return [float(row[idx]) for row in rows]

    p99_2_6_1 = column("P99(duration_ms) - 2.6.1")
    p99_2_5_1 = column("P99(duration_ms) - 2.5.1")
    p99_2_5_0 = column("P99(duration_ms) - 2.5.0")

    # The faulted group steps: its last bucket reads far above its first.
    assert p99_2_6_1[-1] - p99_2_6_1[0] > 400
    # The unaffected groups stay flat across the whole window.
    assert max(p99_2_5_1) - min(p99_2_5_1) < 100
    assert max(p99_2_5_0) - min(p99_2_5_0) < 100

    # Every COUNT column renders as whole numbers, including COUNT - 2.6.1
    # (chart range "41.2 - 171": not integral endpoints, but a per-row step
    # of 11.8, so the whole-number rule still applies. It is the per-row
    # step that gates this, not the endpoints.)
    count_columns = [h for h in headers if h.startswith("COUNT")]
    for name in count_columns:
        assert all("." not in row[headers.index(name)] for row in rows)


def test_series_table_heatmap_shows_the_true_baseline_and_the_step() -> None:
    """Regression case for the x-axis bug: sampling against `from`..`to` instead
    of the chart's own first/last bucket read this fixture's 06:40 bucket as
    609 and its 06:50 bucket as 460; both should read close to the ground
    truth once buckets are indexed against the chart's own axis."""
    fixture = load("run_query_series_heatmap_60")
    rendered = format_tool_result("run_query", text_of(fixture), args=fixture["args"])
    headers, rows = _series_table_rows(rendered)

    # The server draws no dot chart for HEATMAP, only for COUNT and P99.
    assert headers == ["bucket_start", "COUNT", "P99(duration_ms)"]
    # The chart's own first and last buckets: twenty-one 60 s buckets from
    # 06:31:00 to 06:51:00.
    assert rows[0][0] == "2026-09-04T06:31:00Z"
    assert rows[-1][0] == "2026-09-04T06:51:00Z"
    assert len(rows) == 21

    by_start = {row[0]: row for row in rows}
    p99_0640 = float(by_start["2026-09-04T06:40:00Z"][2])
    p99_0641 = float(by_start["2026-09-04T06:41:00Z"][2])
    count_0650 = float(by_start["2026-09-04T06:50:00Z"][1])
    # One chart row is COUNT 87, P99(duration_ms) 63 here.
    assert abs(p99_0640 - 320) <= 63
    assert abs(count_0650 - 900) <= 87
    # The step: the very next bucket jumps well past one row's resolution.
    assert p99_0641 - p99_0640 > 63


def test_series_table_absent_without_a_granularity() -> None:
    """No `granularity` in the query spec: output is unchanged from before this feature.

    The raw text here still carries a `# Time Series` chart (the server drew
    one with its own default granularity), which is exactly the case this
    guards: the model never asked for a series, so it never sees one.
    """
    fixture = load("run_query_series_nogranularity")
    rendered = format_tool_result("run_query", text_of(fixture), args=fixture["args"])
    assert "# Time Series" not in rendered
    assert "bucket_start" not in rendered
    assert "│" not in rendered  # the │ chart border
    assert "query_id: tpdRBvdtBsj" in rendered


def test_series_table_caps_at_40_buckets() -> None:
    fixture = load("run_query_series_synthetic_45buckets")
    rendered = format_tool_result("run_query", text_of(fixture), args=fixture["args"])
    _headers, rows = _series_table_rows(rendered)
    assert len(rows) == 40
    assert "... 5 buckets omitted" in rendered


_LABEL_LINE = " 2026-01-01T00:00Z" + " " * 34 + "2026-01-01T00:05Z" + " " * 34 + "2026-01-01T00:10Z"


def _make_chart(header: str, plot_lines: list[str], label_line: str = _LABEL_LINE) -> str:
    return f"{header}\n" + "\n".join(plot_lines) + "\n└" + "─" * 120 + "\n" + label_line + "\n"


def _make_result_text(chart_blocks: list[str], granularity: int = 60) -> str:
    blocks = "\n\n".join(f"```\n{chart}```" for chart in chart_blocks)
    return (
        "# Results\n\n| COUNT |\n| --- |\n| 5 |\n\n\n"
        f"# Time Series\n\n{blocks}\n\n"
        f"---\nMetadata:\n  granularity: {granularity}\n  query_run_pk: synthPK\n"
    )


_TEN_MINUTE_ARGS = {
    "query_spec": {
        "granularity": 60,
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-01T00:10:00Z",
    }
}


def test_series_table_falls_back_to_raw_charts_when_a_header_has_no_range(
    caplog,
) -> None:
    plot_line = "│" + " " * 120
    chart = _make_chart("COUNT", [plot_line] * 12)
    text = _make_result_text([chart])

    with caplog.at_level(logging.WARNING):
        rendered = format_tool_result("run_query", text, args=_TEN_MINUTE_ARGS)

    assert "bucket_start" not in rendered  # no parsed series table
    assert "```" in rendered and "COUNT" in rendered  # the raw chart, verbatim
    assert "(time series parse warning:" in rendered
    assert any("range" in record.message for record in caplog.records)


def test_series_table_raw_fallback_is_capped_at_4kb(caplog) -> None:
    """A parse failure with a lot of raw chart text to fall back to (six
    breakdown chart blocks, ~13 KB here) is still capped, like every other
    fallback in this module."""
    fixture = load("run_query_series_breakdown_version_120")
    # Strip every chart header's `[min - max]` range so every block fails
    # to parse and the whole `# Time Series` section falls back verbatim.
    corrupted = re.sub(r"\s\[[^\]]*\]", "", text_of(fixture))

    with caplog.at_level(logging.WARNING):
        rendered = format_tool_result("run_query", corrupted, args=fixture["args"])

    assert "bucket_start" not in rendered
    fallback_start = rendered.index("(time series parse warning:")
    fallback_end = rendered.index("\n\nquery_id:")
    fallback = rendered[fallback_start:fallback_end]
    assert len(fallback.encode("utf-8")) <= MAX_JSON_BYTES + 100
    assert "truncated" in fallback


def test_series_table_small_integral_range_does_not_collapse_to_0_or_1() -> None:
    """A `[0 - 1]` chart: the endpoints are integral, but one row is worth
    only 0.09, so values must not all round to 0 or 1."""
    plot_line_dot = "│" + "·" * 120
    plot_line_blank = "│" + " " * 120
    plot_lines = [plot_line_blank] * 5 + [plot_line_dot] + [plot_line_blank] * 6
    chart = _make_chart("COUNT [0 - 1]", plot_lines)
    text = _make_result_text([chart])

    rendered = format_tool_result("run_query", text, args=_TEN_MINUTE_ARGS)
    _headers, rows = _series_table_rows(rendered)

    assert rows
    assert all(row[1] not in ("0", "1") for row in rows)
    assert "one chart row is COUNT 0.0909;" in rendered


def test_series_table_chart_number_parses_a_billions_suffix() -> None:
    plot_line_dot = "│" + "·" * 120
    plot_line_blank = "│" + " " * 120
    plot_lines = [plot_line_blank] * 11 + [plot_line_dot]  # bottom row: the range's lo
    chart = _make_chart("COUNT [1B - 2B]", plot_lines)
    text = _make_result_text([chart])

    rendered = format_tool_result("run_query", text, args=_TEN_MINUTE_ARGS)
    _headers, rows = _series_table_rows(rendered)

    assert rows
    assert all(row[1] == "1000000000" for row in rows)


def test_series_table_pads_a_short_plot_line_instead_of_dropping_the_chart() -> None:
    """A plot line narrower than 120 columns (trailing spaces trimmed, the
    common shape for an otherwise-blank row) is padded, not treated as a
    parse failure."""
    plot_line_dot = "│" + "·" * 120
    plot_line_blank = "│" + " " * 120
    plot_lines = [plot_line_blank] * 3 + [plot_line_dot] + [plot_line_blank] * 7 + ["│"]
    chart = _make_chart("COUNT [10 - 20]", plot_lines)
    text = _make_result_text([chart])

    rendered = format_tool_result("run_query", text, args=_TEN_MINUTE_ARGS)
    _headers, rows = _series_table_rows(rendered)
    assert rows  # parsed, not a raw fallback


def test_series_table_drops_one_bad_chart_but_keeps_the_rest(caplog) -> None:
    """One malformed chart block among several loses just that chart, with a
    warning, not the whole table."""
    good_plot = ["│" + " " * 120] * 11 + ["│" + "·" * 120]
    good_chart = _make_chart("COUNT [10 - 20]", good_plot)
    bad_chart = _make_chart("BROKEN", good_plot)
    text = _make_result_text([bad_chart, good_chart])

    with caplog.at_level(logging.WARNING):
        rendered = format_tool_result("run_query", text, args=_TEN_MINUTE_ARGS)

    headers, rows = _series_table_rows(rendered)
    assert headers == ["bucket_start", "COUNT"]
    assert rows
    assert any("dropping one chart block" in record.message for record in caplog.records)


def test_series_table_query_spec_as_json_string_parses_like_the_dict_form() -> None:
    fixture = load("run_query_series_nobreak_120")
    dict_args = fixture["args"]
    string_args = {**dict_args, "query_spec": json.dumps(dict_args["query_spec"])}

    rendered_dict = format_tool_result("run_query", text_of(fixture), args=dict_args)
    rendered_string = format_tool_result("run_query", text_of(fixture), args=string_args)
    assert rendered_dict == rendered_string


def test_series_table_adds_under_1kb_for_the_120s_two_calculation_case() -> None:
    fixture = load("run_query_series_nobreak_120")
    with_series = format_tool_result("run_query", text_of(fixture), args=fixture["args"])

    args_without_granularity = copy.deepcopy(fixture["args"])
    del args_without_granularity["query_spec"]["granularity"]
    without_series = format_tool_result(
        "run_query", text_of(fixture), args=args_without_granularity
    )

    added_bytes = len(with_series.encode("utf-8")) - len(without_series.encode("utf-8"))
    assert 0 < added_bytes < 1024
