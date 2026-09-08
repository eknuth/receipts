"""Compact text rendering of Honeycomb MCP tool results, sized for a model.

The hosted Honeycomb MCP returns its read-tool results as pre-formatted
Markdown text (a `text` content block), not structured JSON: every fixture
in `tests/fixtures/mcp/` captured from a live call shows this. So the
renderers here parse that Markdown (a `# Results` or bare table, a
`## Dimensions` block, a trailing `Metadata:` block of `key: value` lines)
and re-render it into the compact shapes the spec asks for: an aligned,
row-capped table for `run_query`; a top-N differentiator list for
`run_bubbleup`; an indented, span-capped waterfall for `get_trace`.
Anything else, or anything that does not parse as expected, falls back to
the raw text (or pretty JSON, if the server ever does return structured
content) truncated to 4 KB.

When a `run_query` call asked for a `granularity`, the server's only series
data is a `# Time Series` heading holding one fenced ASCII dot chart per
calculation (and per calculation and group, for a breakdown query): a
header line naming the calculation and its `[min - max]` range, 12 plot
rows of 120 columns each, an axis line, and a label line naming the first
and last bucket. The chart's x axis spans exactly those two buckets over
`_CHART_WIDTH` columns, one bucket per data point interpolated to the
next; it does not span the query's `from`..`to` (the server can start its
first bucket later than `from`, when data does, and can silently pick a
different granularity than the one asked for; the Metadata block's
`granularity` is the one that matches the chart). `_build_series_table`
reads the label line and the Metadata granularity to find each bucket's
column, samples a small neighborhood of columns around it, and appends the
result, plus a one-line note of the chart's per-row resolution (and of a
granularity substitution, when the server made one), after the Results
table. Without a `granularity` in the query spec, that chart is still
stripped by `_format_json`'s fallback path as before, so the model never
sees it; a parse failure in `_build_series_table` is the one path where
the raw chart blocks do reach the model, verbatim and size-capped,
alongside a logged warning, rather than silently dropping the series.

`format_tool_result` is the single entry point `agent/mcp_client.py` calls
after a `tools/call` response is parsed. `extract_ids` is called
separately, on the raw text, to pull `query_id` and `permalink` out of the
Metadata block for the `ToolResult` the caller sees. `breakdown_values` is
called separately too, by `agent/loop.py`, to read the values a `run_query`
breakdown actually took off the same `# Results` table, so a report's claim
about what it did or did not read can be checked against them.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

MAX_JSON_BYTES = 4096
MAX_QUERY_ROWS = 25
MAX_TRACE_SPANS = 60
MAX_BUBBLEUP_DIFFERENTIATORS = 10
MAX_SERIES_ROWS = 40

# The measured width of one chart's plot columns (see the module docstring):
# every plot line is a `│` followed by exactly this many columns.
_CHART_WIDTH = 120
_CHART_ROW_MARK = "·"  # the dot the chart uses to mark a value
# "Sample that column with a one-column neighbourhood either side": three
# columns total, centred on the bucket's own column.
_COLUMN_NEIGHBORHOOD = 1

logger = logging.getLogger(__name__)

# Metadata keys that carry a query/analysis identifier or a UI permalink, in
# the priority order tools tend to emit them. Different tools use different
# names for the same idea (a query run's primary key, a trace result's, a
# BubbleUp analysis riding on its source query's).
_QUERY_ID_KEYS = ("query_run_pk", "trace_result_pk", "bubbleup_result_id")
_PERMALINK_KEYS = ("query_url", "trace_link", "bubble_up_url")

_METADATA_LINE = re.compile(r"^  ([A-Za-z0-9_]+): (.*)$")
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_TIME_SERIES_BLOCK = re.compile(r"^# Time Series\s*\n```.*?^```\s*\n?", re.MULTILINE | re.DOTALL)
# The whole `# Time Series` section (every chart block in it), stopping at
# the next top-level heading (e.g. `# Heatmaps`), the `---` before Metadata,
# or the end of the text.
_TIME_SERIES_SECTION = re.compile(
    r"^# Time Series\s*\n(?P<body>.*?)(?=^# |\n---\n|\Z)", re.MULTILINE | re.DOTALL
)
_CHART_BLOCK = re.compile(r"```\n(?P<block>.*?)```", re.DOTALL)
_CHART_HEADER = re.compile(r"^(?P<name>.+?)\s*\[(?P<lo>[^\]]+?)\s*-\s*(?P<hi>[^\]]+)\]\s*$")
_CHART_NUMBER = re.compile(r"^(-?[0-9.]+)\s*([KMB]?)$")
# The label line under a chart's axis: minute-precision timestamps naming
# the first, middle, and last bucket (`2026-09-04T06:30Z`). Only the first
# and last matter; the middle one is display only.
_LABEL_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z")
_BUBBLEUP_COLUMN = re.compile(
    r"\*\*(?P<col>[^*]+)\*\*\s*\((?P<populated>[^)]*)\)\s*\n(?P<bullets>(?:-.*\n?)+)"
)
_BUBBLEUP_BULLET = re.compile(r"^-\s*(?P<value>.+?):\s*(?P<base>[\d.]+)%.*?(?P<sel>[\d.]+)%")


def parse_metadata_block(text: str) -> dict[str, str]:
    """Pull the trailing `Metadata:\\n  key: value` block out of `text`.

    Stops at the first line, after `Metadata:`, that is not a two-space
    indented `key: value` line. Quoted values have their quotes stripped.
    Returns an empty dict if there is no such block.
    """
    metadata: dict[str, str] = {}
    in_block = False
    for line in text.splitlines():
        if not in_block:
            if line.strip() == "Metadata:":
                in_block = True
            continue
        match = _METADATA_LINE.match(line)
        if match is None:
            break
        metadata[match.group(1)] = match.group(2).strip('"')
    return metadata


def extract_ids(text: str) -> tuple[str | None, str | None]:
    """The query/analysis id and UI permalink from a tool result's Metadata block."""
    metadata = parse_metadata_block(text)
    query_id = next((metadata[k] for k in _QUERY_ID_KEYS if k in metadata), None)
    permalink = next((metadata[k] for k in _PERMALINK_KEYS if k in metadata), None)
    return query_id, permalink


def extract_bubbleup_result_id(text: str) -> str | None:
    """A `run_bubbleup` result's own id, for paging into it as `bubbleup_result_id`.

    The live server does not send a plain `bubbleup_result_id:` Metadata
    line (see `tests/fixtures/mcp/run_bubbleup.json`, captured from a real
    call): the id is the `bubbleup_result` query parameter on `bubble_up_url`
    instead, e.g. `...?tab=bubbleup&bubbleup_result=ujVTPWn4uyd`. That is the
    primary source here. A literal `bubbleup_result_id` Metadata key, should
    the server ever send one, is a fallback.
    """
    metadata = parse_metadata_block(text)
    url = metadata.get("bubble_up_url")
    if url:
        values = parse_qs(urlsplit(url).query).get("bubbleup_result")
        if values:
            return values[0]
    return metadata.get("bubbleup_result_id")


def parse_results_table(
    text: str, heading: str | None = "# Results"
) -> tuple[list[str], list[list[str]]] | None:
    """The `(headers, rows)` of a tool result's Markdown table, or None.

    Public because `gen/verify.py` reads numbers out of a `run_query` result
    rather than showing it to a model. `heading` defaults to the `# Results`
    section every aggregate query returns; pass None to take the first table
    in the text.
    """
    return _parse_markdown_table(text, heading)


def parse_column_types(text: str) -> dict[str, str]:
    """Column name to lowercased type, from a `get_dataset_columns` result's `# Columns` table.

    Empty when there is no such table. `agent/mcp_client.py` uses this to
    retype a BubbleUp group selection's values against the dataset's actual
    schema (`boolean`, `integer`, `float`, `string`, the exact words the
    hosted MCP uses) rather than guessing from the value's own shape.
    """
    table = _parse_markdown_table(text, heading="# Columns")
    if table is None:
        return {}
    headers, rows = table
    try:
        name_idx = headers.index("Name")
        type_idx = headers.index("Type")
    except ValueError:
        return {}
    return {
        row[name_idx]: row[type_idx].strip().lower()
        for row in rows
        if len(row) > max(name_idx, type_idx) and row[name_idx]
    }


def format_tool_result(name: str, payload: Any, *, args: dict[str, Any] | None = None) -> str:
    """Render one tool's result compactly.

    `payload` is `structured_content` if the server sent any, otherwise the
    joined text content (the common case for the hosted Honeycomb MCP).
    `args` is the arguments the tool was called with, used to describe a
    `run_query` call's shape (the response text does not repeat it).
    """
    if isinstance(payload, str):
        if name == "run_query":
            rendered = _format_run_query(payload, args)
            if rendered is not None:
                return rendered
        elif name == "run_bubbleup":
            rendered = _format_bubbleup(payload)
            if rendered is not None:
                return rendered
        elif name == "get_trace":
            rendered = _format_trace(payload)
            if rendered is not None:
                return rendered
    return _format_json(payload)


def breakdown_values(payload: Any, *, args: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """The values each breakdown column took in a `run_query` result's rows.

    Read from the same `# Results` table `_format_run_query` renders, capped
    to the same `MAX_QUERY_ROWS` rows the rendered table shows: a row beyond
    that cap was never in what the model saw, so a value that appears only
    there does not count as read. `OTHER` and `TOTAL`, the two placeholder
    rows the server adds when a breakdown has more groups than the query
    asked to see, are not values of the column and are left out; so is a
    blank cell, which means the column is absent from that row rather than
    naming a value.

    Empty for anything that is not a `run_query` result read this way: a
    `run_bubbleup` result is baseline-vs-selection percentages per column, not
    a row of values, and neither it nor `get_trace`'s waterfall has a
    `# Results` table to parse. Also empty for a `run_query` whose `query_spec`
    carried no `breakdowns`, whose result carries no `# Results` table at all
    (an error message, or a response that came back as structured JSON rather
    than the server's usual Markdown), or that is not a string in the first
    place.
    """
    if not isinstance(payload, str):
        return {}
    spec = _get_query_spec(args)
    breakdowns = spec.get("breakdowns")
    if not isinstance(breakdowns, list) or not breakdowns:
        return {}
    table = _parse_markdown_table(payload, heading="# Results")
    if table is None:
        return {}
    headers, rows = table
    rows = rows[:MAX_QUERY_ROWS]
    out: dict[str, list[str]] = {}
    for column in breakdowns:
        if not isinstance(column, str) or column not in headers:
            continue
        idx = headers.index(column)
        values: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if idx >= len(row):
                continue
            value = row[idx]
            if not value or value in ("OTHER", "TOTAL") or value in seen:
                continue
            seen.add(value)
            values.append(value)
        if values:
            out[column] = values
    return out


def format_error(name: str, message: str) -> str:
    """The text a model sees when the server flags a call as an error."""
    body = message.strip() or "(no message)"
    return f"{name} failed: {body}"


def _format_json(value: Any) -> str:
    """Pretty JSON (or plain text, if `value` already is text), truncated to 4 KB.

    Text has the ASCII time-series chart removed first; it is large and
    carries nothing a model can use. Truncation cuts at a line boundary.
    """
    if isinstance(value, str):
        text = _TIME_SERIES_BLOCK.sub("", value).strip()
        if not text:
            return "(empty result)"
    else:
        text = json.dumps(value, indent=2, sort_keys=True, default=str)
    return truncate(text, MAX_JSON_BYTES)


def truncate(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` bytes of UTF-8 at the last newline before the limit."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    head = encoded[:limit].decode("utf-8", errors="ignore")
    cut = head.rfind("\n")
    if cut > limit // 2:
        head = head[:cut]
    return f"{head}\n... truncated, {len(encoded)} bytes total"


def _parse_markdown_table(
    text: str, heading: str | None = None
) -> tuple[list[str], list[list[str]]] | None:
    """Parse a `| a | b |` Markdown table, optionally the one under `heading`.

    Returns `(headers, rows)`, or `None` if no table is found. `heading`,
    when given, is matched against a whole stripped line (e.g. `# Results`);
    the table is the first one found after it.
    """
    lines = text.splitlines()
    start = 0
    if heading is not None:
        for i, line in enumerate(lines):
            if line.strip() == heading:
                start = i + 1
                break
        else:
            return None

    i = start
    while i < len(lines) and not lines[i].strip().startswith("|"):
        i += 1
    if i + 1 >= len(lines):
        return None
    header_line, sep_line = lines[i], lines[i + 1]
    if not sep_line.strip().startswith("|"):
        return None

    headers = _split_cells(header_line)
    rows: list[list[str]] = []
    j = i + 2
    while j < len(lines) and lines[j].strip().startswith("|"):
        rows.append(_split_cells(lines[j]))
        j += 1
    return headers, rows


def _split_cells(line: str) -> list[str]:
    """Cells of one `| a | b |` row. A `\\|` inside a cell is a literal pipe."""
    inner = line.strip().strip("|")
    return [c.strip().replace("\\|", "|") for c in _UNESCAPED_PIPE.split(inner)]


def _render_table(
    headers: list[str], rows: list[list[str]], max_rows: int, overflow_label: str = "more rows"
) -> str:
    """An aligned, whitespace-padded table, capped at `max_rows` data rows."""
    shown = rows[:max_rows]
    widths = [len(h) for h in headers]
    for row in shown:
        for idx, cell in enumerate(row):
            if idx < len(widths):
                widths[idx] = max(widths[idx], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return "  ".join(
            cell.ljust(widths[idx]) for idx, cell in enumerate(cells) if idx < len(widths)
        )

    lines = [fmt_row(headers), fmt_row(["-" * w for w in widths])]
    lines.extend(fmt_row(row) for row in shown)
    if len(rows) > max_rows:
        lines.append(f"... {len(rows) - max_rows} {overflow_label}")
    return "\n".join(lines)


def _get_query_spec(args: dict[str, Any] | None) -> dict[str, Any]:
    """The `query_spec` argument as a dict, whether it arrived as one or as a JSON string.

    The agent always sends a dict, but a run recorded through some other
    path (a replayed tool log, a hand-built fixture) can carry it as text.
    Anything that is not a dict, or a string that does not parse as one,
    is treated as an absent spec rather than raising.
    """
    spec = (args or {}).get("query_spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError:
            return {}
    return spec if isinstance(spec, dict) else {}


def _describe_query_spec(args: dict[str, Any] | None) -> str:
    """One line naming the calculations, filters, breakdowns, and time range asked for."""
    spec = _get_query_spec(args)

    calc_parts = []
    for calc in spec.get("calculations") or []:
        op = calc.get("op", "?")
        column = calc.get("column")
        calc_parts.append(f"{op}({column})" if column else op)
    calc_str = ", ".join(calc_parts) if calc_parts else "none"

    filter_parts = []
    for flt in spec.get("filters") or []:
        column = flt.get("column", "?")
        op = flt.get("op", "=")
        value = flt.get("value", "")
        filter_parts.append(f"{column} {op} {value}".strip())
    filter_str = "; ".join(filter_parts) if filter_parts else "none"

    breakdowns = spec.get("breakdowns") or []
    breakdown_str = ", ".join(breakdowns) if breakdowns else "none"

    # The hosted MCP names the bounds `from` and `to`. Stored tool logs from
    # before the server renamed them carry `start_time` and `end_time`, and
    # those still render so every log reads the same.
    if spec.get("from") or spec.get("to"):
        time_range = f"{spec.get('from', '?')}..{spec.get('to', '?')}"
    elif spec.get("start_time") or spec.get("end_time"):
        time_range = f"{spec.get('start_time', '?')}..{spec.get('end_time', '?')}"
    else:
        time_range = spec.get("time_range", "default")
    dataset = (args or {}).get("dataset_slug") or "environment-wide"

    return (
        f"run_query on {dataset}: calculations=[{calc_str}] filters=[{filter_str}] "
        f"breakdowns=[{breakdown_str}] time_range={time_range}"
    )


class _SeriesParseError(Exception):
    """A `# Time Series` chart did not parse the way the geometry doc says it should.

    Always caught inside `_render_time_series`; never escapes this module.
    Its message names the reason, for the warning log.
    """


@dataclass
class _ChartBlock:
    name: str
    lo: float
    hi: float
    plot_lines: list[str]  # each padded/trimmed to _CHART_WIDTH characters, top row first
    label_line: str | None = None  # the line under the axis naming the first/last bucket


def _parse_chart_number(text: str) -> float:
    """A chart axis endpoint: `814.72`, `1.9K`, `41.2`, `1.1M`, `2.3B`."""
    match = _CHART_NUMBER.match(text.strip())
    if match is None:
        raise _SeriesParseError(f"could not parse chart axis number {text!r}")
    value = float(match.group(1))
    suffix = match.group(2)
    if suffix == "K":
        value *= 1_000
    elif suffix == "M":
        value *= 1_000_000
    elif suffix == "B":
        value *= 1_000_000_000
    return value


def _parse_chart_block(raw: str) -> _ChartBlock:
    """One fenced chart block's header, plot rows, and label line.

    Raises `_SeriesParseError` naming the reason on anything that does not
    match the measured geometry: a header without a `[min - max]` range, or
    a plot line wider than `_CHART_WIDTH` columns. A plot line that is
    narrower (trailing spaces trimmed by whatever captured the fixture,
    typically an all-blank top row) is padded out rather than treated as a
    parse failure.
    """
    lines = raw.splitlines()
    if not lines or not lines[0].strip():
        raise _SeriesParseError("chart block has no header line")
    header = lines[0].strip()
    match = _CHART_HEADER.match(header)
    if match is None:
        raise _SeriesParseError(f"chart header has no [min - max] range: {header!r}")
    name = match.group("name").strip()
    lo = _parse_chart_number(match.group("lo"))
    hi = _parse_chart_number(match.group("hi"))

    plot_lines: list[str] = []
    label_line: str | None = None
    for idx, line in enumerate(lines[1:], start=1):
        if line.startswith("└"):
            if idx + 1 < len(lines):
                label_line = lines[idx + 1]
            break
        if not line.startswith("│"):
            continue
        content = line[1:]
        if len(content) < _CHART_WIDTH:
            content = content.ljust(_CHART_WIDTH)
        elif len(content) > _CHART_WIDTH:
            raise _SeriesParseError(
                f"{name!r} plot line is {len(content)} columns, expected {_CHART_WIDTH}"
            )
        plot_lines.append(content)
    if not plot_lines:
        raise _SeriesParseError(f"{name!r} chart has no plot rows")
    return _ChartBlock(name=name, lo=lo, hi=hi, plot_lines=plot_lines, label_line=label_line)


def _parse_axis_labels(label_line: str | None) -> tuple[datetime, datetime] | None:
    """The first and last bucket start times named on a chart's label line, or None.

    The label line names three buckets at minute precision (first, middle,
    last); only the first and last matter here.
    """
    if not label_line:
        return None
    matches = _LABEL_TIME.findall(label_line)
    if len(matches) < 2:
        return None
    first = datetime.strptime(matches[0], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
    last = datetime.strptime(matches[-1], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
    return first, last


def _extract_time_series_section(text: str) -> str | None:
    """The body of the `# Time Series` heading (every chart block in it), or None."""
    match = _TIME_SERIES_SECTION.search(text)
    return match.group("body") if match else None


def _parse_number_maybe(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_time_bound(value: Any) -> datetime | None:
    """A query spec's `from`/`to`: an epoch number, or an ISO 8601 string (`Z` or offset)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


def _round_to_sig_figs(value: float, sig: int) -> float:
    if value == 0:
        return 0.0
    magnitude = math.floor(math.log10(abs(value)))
    factor = 10 ** (sig - 1 - magnitude)
    return round(value * factor) / factor


def _chart_row_step(chart: _ChartBlock) -> float:
    """One plot row's worth of `chart`'s `[lo - hi]` range."""
    nrows = len(chart.plot_lines)
    if nrows <= 1:
        return chart.hi - chart.lo
    return (chart.hi - chart.lo) / (nrows - 1)


def _format_series_value(value: float, chart: _ChartBlock) -> str:
    """`value`, rounded to 3 significant figures; a whole number when a chart row
    is worth at least one whole unit. An all-integer range is not enough on its
    own: a `[0 - 1]` chart would render every value as 0 or 1."""
    if abs(_chart_row_step(chart)) >= 1:
        return str(int(round(value)))
    rounded = _round_to_sig_figs(value, 3)
    if rounded == 0:
        return "0"
    decimals = max(0, 2 - math.floor(math.log10(abs(rounded))))
    return f"{rounded:.{decimals}f}"


def _bucket_column(i: int, buckets: int) -> int:
    """Bucket `i`'s column on the chart's own axis: the first bucket at column 0,
    the last at `_CHART_WIDTH - 1`, evenly spaced between."""
    if buckets <= 1:
        return 0
    return math.floor(i / (buckets - 1) * (_CHART_WIDTH - 1))


def _sample_bucket(chart: _ChartBlock, col: int) -> str:
    """Bucket value at chart column `col`: dot rows averaged over a small
    neighborhood of columns around it (see `_COLUMN_NEIGHBORHOOD`)."""
    nrows = len(chart.plot_lines)
    lo_col = max(0, col - _COLUMN_NEIGHBORHOOD)
    hi_col = min(_CHART_WIDTH - 1, col + _COLUMN_NEIGHBORHOOD)
    hit_rows = [
        row
        for c in range(lo_col, hi_col + 1)
        for row, line in enumerate(chart.plot_lines)
        if c < len(line) and line[c] == _CHART_ROW_MARK
    ]
    if not hit_rows:
        return ""
    mean_row = sum(hit_rows) / len(hit_rows)
    if nrows <= 1:
        value = chart.hi
    else:
        value = chart.hi - (mean_row / (nrows - 1)) * (chart.hi - chart.lo)
    return _format_series_value(value, chart)


def _chart_row_resolution(chart: _ChartBlock) -> str:
    """`chart`'s per-row value: one row's worth of its `[lo - hi]` range."""
    return f"{chart.name} {_format_series_value(_chart_row_step(chart), chart)}"


def _chart_resolution_line(charts: list[_ChartBlock]) -> str | None:
    """One line naming each chart's per-row resolution, so a model does not read
    a one-row wobble on a flat series as a real change."""
    parts = [_chart_row_resolution(chart) for chart in charts if len(chart.plot_lines) > 1]
    if not parts:
        return None
    return f"one chart row is {', '.join(parts)}; smaller moves are within the chart's resolution"


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _build_series_table(text: str, spec: dict[str, Any], spec_granularity: Any) -> str:
    """The compact series table, or raises `_SeriesParseError` naming why it could not build one."""
    section = _extract_time_series_section(text)
    if section is None:
        raise _SeriesParseError("no '# Time Series' heading, though the spec asked for one")

    raw_blocks = [m.group("block") for m in _CHART_BLOCK.finditer(section)]
    if not raw_blocks:
        raise _SeriesParseError("'# Time Series' heading has no chart blocks under it")

    charts: list[_ChartBlock] = []
    for raw in raw_blocks:
        try:
            charts.append(_parse_chart_block(raw))
        except _SeriesParseError as exc:
            # One malformed chart among several loses just that chart, not
            # the whole table.
            logger.warning("run_query time series: dropping one chart block: %s", exc)
    if not charts:
        raise _SeriesParseError("no chart block under '# Time Series' parsed")

    metadata = parse_metadata_block(text)
    granularity = _parse_number_maybe(metadata.get("granularity"))
    if granularity is None:
        granularity = _parse_number_maybe(spec_granularity)
    if not granularity:
        raise _SeriesParseError("no usable granularity in the Metadata block or the query spec")

    # The chart's own axis spans its first bucket's start to its last
    # bucket's start, named on the label line under the axis, not the
    # query's `from`..`to`. `from`/`to`, when they parse, only refine the
    # label's minute-precision endpoint to the exact second, and only when
    # doing so agrees with the label (it can disagree: the server can start
    # its first bucket later than `from`, when data does).
    label_bounds = None
    for chart in charts:
        label_bounds = _parse_axis_labels(chart.label_line)
        if label_bounds is not None:
            break
    if label_bounds is None:
        raise _SeriesParseError("could not read the chart's axis labels")
    label_first, label_last = label_bounds

    first_bucket = label_first
    from_dt = _parse_time_bound(spec.get("from"))
    if from_dt is not None:
        aligned = math.floor(from_dt.timestamp() / granularity) * granularity
        aligned_dt = datetime.fromtimestamp(aligned, tz=UTC)
        if aligned_dt.replace(second=0, microsecond=0) == label_first:
            first_bucket = aligned_dt

    last_bucket = label_last
    to_dt = _parse_time_bound(spec.get("to"))
    if to_dt is not None:
        # The aligned bucket containing the last instant *before* `to`: a
        # `to` that lands exactly on a granularity boundary must not be
        # read as the start of one bucket past the data.
        aligned = math.floor((to_dt.timestamp() - 1e-6) / granularity) * granularity
        aligned_dt = datetime.fromtimestamp(aligned, tz=UTC)
        if aligned_dt.replace(second=0, microsecond=0) == label_last:
            last_bucket = aligned_dt

    span_seconds = (last_bucket - first_bucket).total_seconds()
    if span_seconds < 0:
        raise _SeriesParseError("the chart's axis labels are out of order")
    buckets = round(span_seconds / granularity) + 1
    if buckets <= 0:
        raise _SeriesParseError("the chart's axis labels and granularity computed zero buckets")

    # One calculation's columns sit together, and within a calculation its
    # groups sort together too, rather than in whatever order the server
    # emitted the chart blocks.
    charts.sort(key=lambda c: tuple(c.name.split(" - ", 1)))

    headers = ["bucket_start", *(chart.name for chart in charts)]
    rows: list[list[str]] = []
    for i in range(buckets):
        bucket_time = first_bucket + timedelta(seconds=i * granularity)
        col = _bucket_column(i, buckets)
        row = [bucket_time.strftime("%Y-%m-%dT%H:%M:%SZ")]
        row.extend(_sample_bucket(chart, col) for chart in charts)
        rows.append(row)

    table = _render_table(headers, rows, MAX_SERIES_ROWS, overflow_label="buckets omitted")

    notes: list[str] = []
    asked = _parse_number_maybe(spec_granularity)
    if asked is not None and asked != granularity:
        notes.append(f"granularity {_format_number(granularity)} s (asked {_format_number(asked)})")
    resolution = _chart_resolution_line(charts)
    if resolution:
        notes.append(resolution)
    if notes:
        table = f"{table}\n{'; '.join(notes)}"
    return table


def _render_time_series(text: str, args: dict[str, Any] | None) -> str | None:
    """A compact series table for a `run_query` call that asked for a `granularity`.

    None when the spec carried no granularity (today's behavior, unchanged)
    or when parsing the chart failed; a parse failure logs a warning and, if
    any chart blocks were found at all, falls back to including them
    verbatim (size-capped, with the warning first) rather than raising.
    """
    spec = _get_query_spec(args)
    spec_granularity = spec.get("granularity")
    if spec_granularity is None:
        return None
    try:
        return _build_series_table(text, spec, spec_granularity)
    except _SeriesParseError as exc:
        logger.warning("run_query time series: %s; falling back to the raw chart blocks", exc)
        section = _extract_time_series_section(text)
        if not section or not section.strip():
            return None
        raw = f"(time series parse warning: {exc})\n# Time Series\n{section.strip()}"
        return truncate(raw, MAX_JSON_BYTES)


def _format_run_query(text: str, args: dict[str, Any] | None) -> str | None:
    table = _parse_markdown_table(text, heading="# Results")
    if table is None:
        return None
    headers, rows = table

    parts = [_describe_query_spec(args), "", _render_table(headers, rows, MAX_QUERY_ROWS)]

    series = _render_time_series(text, args)
    if series:
        parts.append("")
        parts.append(series)

    query_id, permalink = extract_ids(text)
    footer = []
    if query_id:
        footer.append(f"query_id: {query_id}")
    if permalink:
        footer.append(f"permalink: {permalink}")
    if footer:
        parts.append("")
        parts.extend(footer)
    return "\n".join(parts)


def _format_bubbleup(text: str) -> str | None:
    columns = [
        (
            m.group("col").strip(),
            [ln.strip() for ln in m.group("bullets").splitlines() if ln.strip()],
        )
        for m in _BUBBLEUP_COLUMN.finditer(text)
    ]
    if not columns:
        return None

    top = columns[:MAX_BUBBLEUP_DIFFERENTIATORS]
    lines = [f"Top {len(top)} of {len(columns)} differentiators (baseline vs selection):"]
    for column, bullets in top:
        lines.append(f"  {column}")
        for bullet in bullets:
            match = _BUBBLEUP_BULLET.match(bullet)
            if match is None:
                lines.append(f"    {bullet}")
                continue
            lines.append(
                f"    {match.group('value')}: baseline {match.group('base')}% "
                f"-> selection {match.group('sel')}%"
            )
    if len(columns) > len(top):
        lines.append(f"... {len(columns) - len(top)} more columns")

    query_id, permalink = extract_ids(text)
    if query_id:
        lines.append(f"query_id: {query_id}")
    if permalink:
        lines.append(f"permalink: {permalink}")
    return "\n".join(lines)


def _format_trace(text: str) -> str | None:
    table = _parse_markdown_table(text)
    if table is None:
        return None
    headers, rows = table
    if "span_id" not in headers or "depth" not in headers:
        return None
    index = {name: i for i, name in enumerate(headers)}

    def cell(row: list[str], key: str) -> str:
        i = index.get(key)
        return row[i] if i is not None and i < len(row) else ""

    def start_ns(row: list[str]) -> int:
        try:
            return int(cell(row, "start_unix_ns"))
        except ValueError:
            return 0

    rows_sorted = sorted(rows, key=start_ns)
    shown = rows_sorted[:MAX_TRACE_SPANS]

    lines = []
    for row in shown:
        try:
            depth = int(cell(row, "depth"))
        except ValueError:
            depth = 0
        try:
            duration_ms = int(cell(row, "duration_ns")) / 1_000_000
            duration_str = f"{duration_ms:.1f}ms"
        except ValueError:
            duration_str = "?ms"
        name = cell(row, "name") or "(unnamed)"
        service = cell(row, "service")
        label = f"{name} ({service})" if service else name
        status = cell(row, "status_code").upper()
        error = cell(row, "error").lower() == "true"
        flag = " ERROR" if error or status == "ERROR" else ""
        lines.append(f"{'  ' * depth}{label} {duration_str}{flag}")
    if len(rows_sorted) > MAX_TRACE_SPANS:
        lines.append(f"... {len(rows_sorted) - MAX_TRACE_SPANS} more")

    metadata = parse_metadata_block(text)
    trace_id = metadata.get("trace_id")
    span_count = len(rows_sorted)
    header = f"trace {trace_id} ({span_count} spans)" if trace_id else f"trace ({span_count} spans)"

    query_id, permalink = extract_ids(text)
    footer = []
    if query_id:
        footer.append(f"query_id: {query_id}")
    if permalink:
        footer.append(f"permalink: {permalink}")

    parts = [header, "", *lines]
    if footer:
        parts.append("")
        parts.extend(footer)
    return "\n".join(parts)
