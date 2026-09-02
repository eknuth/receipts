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

`format_tool_result` is the single entry point `agent/mcp_client.py` calls
after a `tools/call` response is parsed. `extract_ids` is called
separately, on the raw text, to pull `query_id` and `permalink` out of the
Metadata block for the `ToolResult` the caller sees.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_JSON_BYTES = 4096
MAX_QUERY_ROWS = 25
MAX_TRACE_SPANS = 60
MAX_BUBBLEUP_DIFFERENTIATORS = 10

# Metadata keys that carry a query/analysis identifier or a UI permalink, in
# the priority order tools tend to emit them. Different tools use different
# names for the same idea (a query run's primary key, a trace result's, a
# BubbleUp analysis riding on its source query's).
_QUERY_ID_KEYS = ("query_run_pk", "trace_result_pk", "bubbleup_result_id")
_PERMALINK_KEYS = ("query_url", "trace_link", "bubble_up_url")

_METADATA_LINE = re.compile(r"^  ([A-Za-z0-9_]+): (.*)$")
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


def _format_json(value: Any) -> str:
    """Pretty JSON (or plain text, if `value` already is text), truncated to 4 KB."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, indent=2, sort_keys=True, default=str)
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_JSON_BYTES:
        return text
    truncated = encoded[:MAX_JSON_BYTES].decode("utf-8", errors="ignore")
    return f"{truncated}\n... truncated, {len(encoded)} bytes total"


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

    headers = [c.strip() for c in header_line.strip().strip("|").split("|")]
    rows: list[list[str]] = []
    j = i + 2
    while j < len(lines) and lines[j].strip().startswith("|"):
        rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
        j += 1
    return headers, rows


def _render_table(headers: list[str], rows: list[list[str]], max_rows: int) -> str:
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
        lines.append(f"... {len(rows) - max_rows} more rows")
    return "\n".join(lines)


def _describe_query_spec(args: dict[str, Any] | None) -> str:
    """One line naming the calculations, filters, breakdowns, and time range asked for."""
    spec = (args or {}).get("query_spec") or {}

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

    time_range = spec.get("time_range", "default")
    dataset = (args or {}).get("dataset_slug") or "environment-wide"

    return (
        f"run_query on {dataset}: calculations=[{calc_str}] filters=[{filter_str}] "
        f"breakdowns=[{breakdown_str}] time_range={time_range}"
    )


def _format_run_query(text: str, args: dict[str, Any] | None) -> str | None:
    table = _parse_markdown_table(text, heading="# Results")
    if table is None:
        return None
    headers, rows = table

    parts = [_describe_query_spec(args), "", _render_table(headers, rows, MAX_QUERY_ROWS)]

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
