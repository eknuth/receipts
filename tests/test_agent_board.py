"""Tests for agent/board.py: panel selection, the duplicate-board check, and
the Markdown parsing of `create_board` and `list_boards`.

Both tools' real shape is Markdown text, not JSON (see the module docstring
in `agent/board.py`); `tests/fixtures/mcp/create_board*.json` and
`list_boards*.json` are sanitized captures from a live call on 2026-09-07,
and the tests that parse them drive the assertions from that real text
rather than a hand-built dict. No test here makes a network call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.board import (
    BoardResult,
    _find_existing,
    _parse_created_board,
    board_name,
    ensure_board,
    run_tag,
)
from agent.report import Evidence, Hypothesis, Report
from agent.telemetry import Telemetry, disabled_run_trace

FIXTURES = Path(__file__).parent / "fixtures" / "mcp"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def text_of(fixture: dict) -> str:
    return "\n".join(fixture["content_texts"])


def make_report(**overrides: Any) -> Report:
    base: dict[str, Any] = {
        "run_id": "run-abcdef123456",
        "scenario_id": "payments-stripe-v251-uswest",
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "incident_present": True,
        "hypotheses": [
            Hypothesis(
                claim="stripe calls in payments gained about 800ms in us-west-2 on 2.5.1",
                dims={"deployment.version": "2.5.1"},
                confidence="high",
                evidence=[
                    Evidence(query_id="Q1", summary="P99 went from 180ms to 980ms"),
                    Evidence(query_id="Q2", summary="12% of requests affected"),
                    Evidence(query_id="Q3", summary="onset at minute 10"),
                    Evidence(query_id="Q4", summary="a fourth query, past the cap"),
                ],
                negation=Evidence(query_id="Q5", summary="P99 flat outside 2.5.1"),
            )
        ],
        "not_checked": ["inventory-db timeouts"],
    }
    base.update(overrides)
    return Report(**base)


@dataclass
class _Result:
    """Stands in for `HoneycombMCP.call`'s return: only `.text` and
    `.is_error` matter to `agent/board.py`, which reads the Markdown off
    `.text` the same way a real `ToolResult` carries it."""

    text: str = ""
    is_error: bool = False


def _boards_markdown(boards: list[dict[str, str]], *, page: int, total_pages: int) -> str:
    """A `list_boards`-shaped page: the real table header and a `Metadata:`
    block naming `page` and `total_pages`, matching `tests/fixtures/mcp/list_boards.json`."""
    lines = ["# Boards", ""]
    if boards:
        lines.append(
            "| ID | Name | Description | Private | QueryCount | SLOCount | TextCount "
            "| UpdatedAt | Tags |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for b in boards:
            lines.append(
                f"| {b['id']} | {b['name']} |  | false | 0 | 0 | 1 | 2026-09-07T00:00:00Z |  |"
            )
    lines += [
        "",
        "---",
        "Metadata:",
        "  environment: receipts-demo",
        "  items_per_page: 25",
        f"  page: {page}",
        f"  total_items: {len(boards)}",
        f"  total_pages: {total_pages}",
    ]
    return "\n".join(lines)


def _created_board_markdown(board_id: str, board_url: str, name: str) -> str:
    """A `create_board`-shaped success reply, matching
    `tests/fixtures/mcp/create_board.json`'s real shape."""
    return (
        "Board created successfully.\n\n---\nMetadata:\n"
        f"  board_id: {board_id}\n"
        f"  board_name: {name}\n"
        f'  board_url: "{board_url}"\n'
        "  environment: receipts-demo\n"
        "  text_count: 1\n"
        '  updated_at: "2026-09-07T00:00:00Z"\n'
    )


@dataclass
class FakeBoardMCP:
    """Records every call and answers with real-shaped Markdown.

    `boards` is a single page's worth (every test here fits on one page
    except the dedicated pagination test below, which uses its own fake).
    """

    boards: list[dict[str, str]] = field(default_factory=list)
    create_error_text: str | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    created_count: int = 0

    async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls.append((name, dict(args or {})))
        if name == "list_boards":
            page = (args or {}).get("page", 1)
            return _Result(text=_boards_markdown(self.boards, page=page, total_pages=1))
        if name == "create_board":
            if self.create_error_text is not None:
                return _Result(text=self.create_error_text, is_error=True)
            self.created_count += 1
            board_id = f"brd-{self.created_count}"
            board_url = (
                f"https://ui.honeycomb.io/acme-team/environments/receipts-demo/board/{board_id}"
            )
            entry_name = (args or {})["name"]
            self.boards.append({"id": board_id, "name": entry_name})
            return _Result(text=_created_board_markdown(board_id, board_url, entry_name))
        raise AssertionError(f"unexpected call to {name!r}")


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------


def test_board_name_is_receipts_scenario_and_the_first_eight_of_run_id() -> None:
    report = make_report()
    assert board_name(report) == f"receipts payments-stripe-v251-uswest {report.run_id[:8]}"
    assert board_name(report) == "receipts payments-stripe-v251-uswest run-abcd"


def test_the_top_three_evidence_queries_become_query_panels_plus_one_text_panel() -> None:
    from agent.board import _panels

    panels = _panels(make_report())
    query_panels = [p for p in panels if p["type"] == "query"]
    text_panels = [p for p in panels if p["type"] == "text"]

    assert [p["id"] for p in query_panels] == ["Q1", "Q2", "Q3"]  # capped at three, the top's order
    assert len(text_panels) == 1
    assert "id" not in text_panels[0]
    assert panels[-1]["type"] == "text"  # text panel last


def test_every_query_panel_carries_a_nonempty_name() -> None:
    """The live server rejects a query panel with no `name` even though the
    documented schema calls it optional (verified 2026-09-07); every panel
    `_panels` builds must carry one."""
    from agent.board import _panels

    panels = _panels(make_report())
    query_panels = [p for p in panels if p["type"] == "query"]
    assert len(query_panels) == 3
    for panel in query_panels:
        assert panel["name"].strip()


def test_a_query_panels_name_comes_from_its_evidence_summary() -> None:
    from agent.board import _panels

    panels = _panels(make_report())
    query_panels = [p for p in panels if p["type"] == "query"]
    assert query_panels[0]["name"] == "P99 went from 180ms to 980ms"
    assert query_panels[0]["description"] == "P99 went from 180ms to 980ms"


def test_a_long_evidence_summary_is_cut_to_a_heading_but_kept_in_the_description() -> None:
    """A live board built on 2026-09-07 titled every panel with a whole
    paragraph of evidence and was unreadable. The name is a heading; the
    full summary belongs in the description."""
    from agent.board import MAX_PANEL_NAME, _panels

    summary = (
        "Cart sizes 8-12 show P99 jumping from ~200-280ms before 15:41 to ~1600-2000ms after, "
        "while cart sizes 1-7 stayed flat. Split across time confirms 15:41:00Z as the onset."
    )
    report = make_report(
        hypotheses=[
            Hypothesis(
                claim="large carts slowed",
                confidence="high",
                evidence=[Evidence(query_id="Q1", summary=summary)],
            )
        ]
    )
    panel = next(p for p in _panels(report) if p["type"] == "query")
    assert len(panel["name"]) <= MAX_PANEL_NAME + 3
    assert "\n" not in panel["name"]
    assert panel["name"].startswith("Cart sizes 8-12 show P99")
    # The second sentence is a heading's worth of noise, so it is cut.
    assert "onset" not in panel["name"]
    # Nothing is lost: the panel still carries the whole summary.
    assert panel["description"] == summary


def test_a_blank_evidence_summary_still_produces_a_nonempty_panel_name() -> None:
    """A blank summary must not take the whole board down with it (create_board
    rejects a nameless query panel outright); a numbered fallback stands in."""
    from agent.board import _panels

    report = make_report(
        hypotheses=[
            Hypothesis(
                claim="one query, blank summary",
                confidence="low",
                evidence=[Evidence(query_id="Q1", summary="   ")],
            )
        ]
    )
    panels = _panels(report)
    query_panel = next(p for p in panels if p["type"] == "query")
    assert query_panel["name"] == "Evidence 1"


def test_fewer_than_three_evidence_items_degrades_to_fewer_query_panels() -> None:
    from agent.board import _panels

    report = make_report(
        hypotheses=[
            Hypothesis(
                claim="one query only",
                confidence="low",
                evidence=[Evidence(query_id="Q1", summary="the only query")],
            )
        ]
    )
    panels = _panels(report)
    query_panels = [p for p in panels if p["type"] == "query"]
    assert [p["id"] for p in query_panels] == ["Q1"]
    assert sum(1 for p in panels if p["type"] == "text") == 1


def test_no_hypothesis_produces_only_the_text_panel() -> None:
    from agent.board import _panels

    report = make_report(incident_present=False, hypotheses=[])
    panels = _panels(report)
    assert panels == [p for p in panels if p["type"] == "text"]
    assert len(panels) == 1


def test_the_text_panel_carries_the_report_summary() -> None:
    from agent.board import _panels

    panels = _panels(make_report())
    text_panel = next(p for p in panels if p["type"] == "text")
    assert "stripe calls in payments gained about 800ms" in text_panel["content"]
    assert "inventory-db timeouts" in text_panel["content"]
    assert "id" not in text_panel
    assert "name" not in text_panel


# --------------------------------------------------------------------------
# The tag rule
# --------------------------------------------------------------------------


def test_run_tag_accepts_the_real_run_id_shape() -> None:
    assert run_tag("run-4155490e2a44") == "run:run-4155490e2a44"


def test_run_tag_rejects_a_value_that_would_not_satisfy_the_tag_rule() -> None:
    assert run_tag("4155490e2a44") is None  # does not start with a letter
    assert run_tag("RUN-4155490e2a44") is None  # uppercase first character


# --------------------------------------------------------------------------
# Parsing the real (sanitized) Markdown fixtures
# --------------------------------------------------------------------------


def test_create_board_success_parsed_from_the_real_fixture() -> None:
    fixture = load("create_board")
    board_id, board_url = _parse_created_board(_Result(text=text_of(fixture)))
    assert board_id == "FAKEbrd0001x"
    assert board_url == (
        "https://ui.honeycomb.io/acme-team/environments/receipts-demo/board/FAKEbrd0001x"
    )


async def test_find_existing_parses_both_rows_of_the_real_list_boards_fixture() -> None:
    fixture = load("list_boards")
    text = text_of(fixture)

    class OneCallMCP:
        def __init__(self) -> None:
            self.calls = 0

        async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            assert name == "list_boards"
            self.calls += 1
            return _Result(text=text)

    tagged = OneCallMCP()
    result = await _find_existing(
        tagged,
        environment_slug="receipts-demo",
        name="receipts probe tagged",
        tag=None,
        run_id="run-fake",
        scenario_id="fake-scenario",
        trace=disabled_run_trace(),
    )
    assert result == ("FAKEbrd0002x", None)
    assert tagged.calls == 1  # total_pages: 1 in the fixture, so no second page is fetched

    untagged = OneCallMCP()
    result2 = await _find_existing(
        untagged,
        environment_slug="receipts-demo",
        name="receipts probe text only",
        tag=None,
        run_id="run-fake",
        scenario_id="fake-scenario",
        trace=disabled_run_trace(),
    )
    assert result2 == ("FAKEbrd0001x", None)


async def test_find_existing_with_the_real_empty_list_boards_fixture_finds_nothing() -> None:
    fixture = load("list_boards_empty")
    text = text_of(fixture)

    class EmptyMCP:
        async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            return _Result(text=text)

    result = await _find_existing(
        EmptyMCP(),
        environment_slug="receipts-demo",
        name="anything at all",
        tag=None,
        run_id="run-fake",
        scenario_id="fake-scenario",
        trace=disabled_run_trace(),
    )
    assert result is None


async def test_find_existing_reads_pages_up_to_the_metadata_total_pages() -> None:
    """`total_pages` from the server's own Metadata block drives pagination,
    not a fixed guess: the match here sits on page 2 of 2."""
    pages = {
        1: _boards_markdown(
            [{"id": "brd-x", "name": "receipts other-scenario run-0000"}], page=1, total_pages=2
        ),
        2: _boards_markdown(
            [{"id": "brd-y", "name": "receipts payments-stripe-v251-uswest run-abcd"}],
            page=2,
            total_pages=2,
        ),
    }

    class PagedMCP:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            assert name == "list_boards"
            page = (args or {})["page"]
            self.calls.append(page)
            return _Result(text=pages[page])

    mcp = PagedMCP()
    result = await ensure_board(make_report(), mcp, environment_slug="receipts-demo")
    assert result.created is False
    assert result.board_id == "brd-y"
    assert mcp.calls == [1, 2]


# --------------------------------------------------------------------------
# The duplicate check
# --------------------------------------------------------------------------


async def test_two_calls_for_the_same_run_id_create_exactly_one_board() -> None:
    mcp = FakeBoardMCP()
    report = make_report()

    first = await ensure_board(report, mcp, environment_slug="receipts-demo")
    second = await ensure_board(report, mcp, environment_slug="receipts-demo")

    assert mcp.created_count == 1
    assert first.created is True
    assert second.created is False
    assert first.board_id == second.board_id
    # The freshly created board gets a url straight from create_board; the
    # rediscovered one does not, since list_boards' table has no url column
    # (confirmed live 2026-09-07) and this module does not reconstruct one.
    assert first.board_url is not None
    assert second.board_url is None


async def test_list_boards_runs_before_create_board() -> None:
    mcp = FakeBoardMCP()
    await ensure_board(make_report(), mcp, environment_slug="receipts-demo")
    assert [name for name, _ in mcp.calls] == ["list_boards", "create_board"]


async def test_create_board_is_named_scoped_and_carries_named_query_panels() -> None:
    mcp = FakeBoardMCP()
    report = make_report()
    await ensure_board(report, mcp, environment_slug="receipts-demo")
    _, args = next(call for call in mcp.calls if call[0] == "create_board")
    assert args["name"] == board_name(report)
    assert args["environment_slug"] == "receipts-demo"
    assert len(args["panels"]) == 4  # 3 query + 1 text
    query_panels = [p for p in args["panels"] if p["type"] == "query"]
    assert all(p["name"] for p in query_panels)


async def test_a_board_matching_by_name_on_a_later_page_is_still_found() -> None:
    """A board from an earlier ensure_board call for a different run sits in
    front of the one being looked for; the match is by name, not position."""
    mcp = FakeBoardMCP(boards=[{"id": "brd-other", "name": "receipts other-scenario run-0000"}])
    report = make_report()
    result = await ensure_board(report, mcp, environment_slug="receipts-demo")
    assert result.created is True  # the existing board's name does not match
    assert mcp.created_count == 1


async def test_a_failed_create_board_is_recorded_and_never_raises() -> None:
    """The real minimal-panel rejection text, captured live: `create_board`
    can fail outright (a nameless query panel, a quota, or anything else the
    server flags), and that must come back as a BoardResult, never a raise."""
    fixture = load("create_board_error")
    mcp = FakeBoardMCP(create_error_text=text_of(fixture))
    result = await ensure_board(make_report(), mcp, environment_slug="receipts-demo")
    assert result.created is False
    assert result.board_id is None
    assert result.error == "Unable to create board"


async def test_a_list_boards_error_never_falls_through_to_create_board() -> None:
    """`_find_existing` used to return `None` for both "looked, found
    nothing" and "the lookup failed", and `ensure_board` created on either.
    A 429 mid-matrix then minted a duplicate board, the exact thing
    acceptance criterion 3 forbids. A failed lookup must be recorded as an
    error instead, across as many calls as it keeps failing on."""

    @dataclass
    class FailingListMCP:
        list_calls: int = 0
        create_calls: int = 0

        async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            if name == "list_boards":
                self.list_calls += 1
                return _Result(text="rate limited", is_error=True)
            if name == "create_board":
                self.create_calls += 1
                return _Result(text="should never be called")
            raise AssertionError(f"unexpected call to {name!r}")

    mcp = FailingListMCP()
    report = make_report()

    first = await ensure_board(report, mcp, environment_slug="receipts-demo")
    second = await ensure_board(report, mcp, environment_slug="receipts-demo")

    assert mcp.create_calls == 0
    assert mcp.list_calls == 2
    assert first.created is False
    assert second.created is False
    assert first.board_id is None
    assert second.board_id is None
    assert first.error == "rate limited"
    assert second.error == "rate limited"


async def test_an_unparseable_but_nonempty_listing_never_falls_through_to_create_board() -> None:
    """`is_error=False` and `total_items > 0` are not proof the listing was
    readable: a `# Boards` table with unexpected headers (`Id | Title`
    instead of the documented `ID`/`Name`) parses to zero usable rows, and
    an earlier version of `_find_existing` read that the same as a genuinely
    empty listing, returning "not found" and letting `ensure_board` mint a
    duplicate. `total_items` says the server thinks there is a board here;
    with no row this function could check `name` against, it must report a
    failed lookup instead of a miss."""
    text = (
        "# Boards\n\n"
        "| Id | Title |\n"
        "| --- | --- |\n"
        "| brd-x | receipts payments-stripe-v251-uswest run-abcdef123456 |\n\n"
        "---\nMetadata:\n"
        "  environment: receipts-demo\n"
        "  items_per_page: 25\n"
        "  page: 1\n"
        "  total_items: 1\n"
        "  total_pages: 1\n"
    )

    @dataclass
    class UnparseableListMCP:
        list_calls: int = 0
        create_calls: int = 0

        async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            if name == "list_boards":
                self.list_calls += 1
                return _Result(text=text)
            if name == "create_board":
                self.create_calls += 1
                return _Result(text="should never be called")
            raise AssertionError(f"unexpected call to {name!r}")

    mcp = UnparseableListMCP()
    result = await ensure_board(make_report(), mcp, environment_slug="receipts-demo")

    assert mcp.create_calls == 0
    assert result.created is False
    assert result.board_id is None
    assert result.error is not None


async def test_a_raising_call_is_recorded_and_never_raises() -> None:
    class RaisingMCP:
        async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            raise RuntimeError("connection reset")

    result = await ensure_board(make_report(), RaisingMCP(), environment_slug="receipts-demo")
    assert result.created is False
    assert "connection reset" in (result.error or "")


def test_board_result_is_a_dataclass_with_the_documented_fields() -> None:
    result = BoardResult(board_id="brd-1", board_url="https://x", created=True)
    assert result.error is None


# --------------------------------------------------------------------------
# Telemetry (R23, EDW-1370): create_board and list_boards as execute_tool
# spans, with the scenario id kept off both
# --------------------------------------------------------------------------


async def test_create_board_and_list_boards_get_execute_tool_spans() -> None:
    """A first `ensure_board` call for a run with no board yet: one
    `list_boards` (nothing found) and one `create_board`, each traced."""
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = telemetry.start_handoff("run-abcdef123456", conversation_id="conv-1")
    mcp = FakeBoardMCP()

    result = await ensure_board(
        make_report(), mcp, environment_slug="receipts-demo", trace=run_trace
    )
    telemetry.flush()

    assert result.created is True
    spans = exporter.get_finished_spans()
    list_spans = [s for s in spans if s.name == "execute_tool list_boards"]
    create_spans = [s for s in spans if s.name == "execute_tool create_board"]
    assert len(list_spans) == 1
    assert len(create_spans) == 1
    for span in list_spans + create_spans:
        assert span.attributes["gen_ai.conversation.id"] == "conv-1"
    assert list_spans[0].attributes["gen_ai.tool.name"] == "list_boards"
    assert create_spans[0].attributes["gen_ai.tool.name"] == "create_board"


async def test_create_board_span_never_carries_the_scenario_id() -> None:
    """`board_name` puts `report.scenario_id` in `create_board`'s real
    `name` argument by design (R12); the span must not carry it anywhere,
    in the arguments or in the result, even though the real call to
    Honeycomb still does (that call is unaffected by this test)."""
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = telemetry.start_handoff("run-abcdef123456", conversation_id="conv-1")
    mcp = FakeBoardMCP()
    report = make_report(scenario_id="payments-stripe-v251-uswest")

    result = await ensure_board(report, mcp, environment_slug="receipts-demo", trace=run_trace)
    telemetry.flush()

    # The real call still carries it: this test is about the span, not the wire.
    _, create_args = next(call for call in mcp.calls if call[0] == "create_board")
    assert "payments-stripe-v251-uswest" in create_args["name"]
    assert result.created is True

    spans = exporter.get_finished_spans()
    create_span = next(s for s in spans if s.name == "execute_tool create_board")
    for value in create_span.attributes.values():
        assert "payments-stripe-v251-uswest" not in str(value)


async def test_list_boards_span_never_carries_the_scenario_id_from_a_matching_row() -> None:
    """A rerun where the board already exists: `list_boards`' real result
    text has a row named after this run's board (which carries the scenario
    id), and the span must not repeat it either."""
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(exporter=exporter)
    run_trace = telemetry.start_handoff("run-abcdef123456", conversation_id="conv-1")
    report = make_report(scenario_id="payments-stripe-v251-uswest")
    mcp = FakeBoardMCP(boards=[{"id": "brd-1", "name": board_name(report)}])

    result = await ensure_board(report, mcp, environment_slug="receipts-demo", trace=run_trace)
    telemetry.flush()

    assert result.created is False
    assert result.board_id == "brd-1"

    spans = exporter.get_finished_spans()
    list_span = next(s for s in spans if s.name == "execute_tool list_boards")
    for value in list_span.attributes.values():
        assert "payments-stripe-v251-uswest" not in str(value)


async def test_ensure_board_with_no_trace_still_completes() -> None:
    mcp = FakeBoardMCP()
    result = await ensure_board(make_report(), mcp, environment_slug="receipts-demo")
    assert result.created is True
