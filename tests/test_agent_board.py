"""Tests for agent/board.py: panel selection and the duplicate-board check.

No test here makes a network call; `FakeBoardMCP` stands in for a
`HoneycombMCP` opened with `allow_write=True`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.board import BoardResult, board_name, ensure_board, run_tag
from agent.report import Evidence, Hypothesis, Report


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
    raw: Any
    text: str = ""
    is_error: bool = False


@dataclass
class FakeBoardMCP:
    """Records every call and answers from a queue keyed by tool name.

    `boards` is the list `list_boards` returns from its first page, mutated
    directly by a test to simulate a board that already exists.
    """

    boards: list[dict[str, Any]] = field(default_factory=list)
    create_result: Any = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    created_count: int = 0

    async def call(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls.append((name, dict(args or {})))
        if name == "list_boards":
            return _Result(raw={"boards": list(self.boards)})
        if name == "create_board":
            self.created_count += 1
            board = {
                "id": f"brd-{self.created_count}",
                "url": f"https://ui.honeycomb.io/team/boards/brd-{self.created_count}",
            }
            self.boards.append({**board, "name": (args or {})["name"]})
            if self.create_result is not None:
                return self.create_result
            return _Result(raw=board)
        raise AssertionError(f"unexpected call to {name!r}")


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------


def test_board_name_is_receipts_scenario_and_the_first_eight_of_run_id() -> None:
    report = make_report()
    assert board_name(report) == f"receipts payments-stripe-v251-uswest {report.run_id[:8]}"
    assert board_name(report) == "receipts payments-stripe-v251-uswest run-abcd"


async def test_the_top_three_evidence_queries_become_query_panels_plus_one_text_panel() -> None:
    from agent.board import _panels

    panels = _panels(make_report())
    query_panels = [p for p in panels if p["type"] == "query"]
    text_panels = [p for p in panels if p["type"] == "text"]

    assert [p["id"] for p in query_panels] == ["Q1", "Q2", "Q3"]  # capped at three, the top's order
    assert len(text_panels) == 1
    assert "id" not in text_panels[0]
    assert panels[-1]["type"] == "text"  # text panel last


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


# --------------------------------------------------------------------------
# The tag rule
# --------------------------------------------------------------------------


def test_run_tag_accepts_the_real_run_id_shape() -> None:
    assert run_tag("run-4155490e2a44") == "run:run-4155490e2a44"


def test_run_tag_rejects_a_value_that_would_not_satisfy_the_tag_rule() -> None:
    assert run_tag("4155490e2a44") is None  # does not start with a letter
    assert run_tag("RUN-4155490e2a44") is None  # uppercase first character


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
    assert first.board_url == second.board_url


async def test_list_boards_runs_before_create_board() -> None:
    mcp = FakeBoardMCP()
    await ensure_board(make_report(), mcp, environment_slug="receipts-demo")
    assert [name for name, _ in mcp.calls] == ["list_boards", "create_board"]


async def test_create_board_is_named_and_scoped_to_the_right_environment() -> None:
    mcp = FakeBoardMCP()
    report = make_report()
    await ensure_board(report, mcp, environment_slug="receipts-demo")
    _, args = next(call for call in mcp.calls if call[0] == "create_board")
    assert args["name"] == board_name(report)
    assert args["environment_slug"] == "receipts-demo"
    assert len(args["panels"]) == 4  # 3 query + 1 text


async def test_a_board_matching_by_name_on_a_later_page_is_still_found() -> None:
    """A board from an earlier ensure_board call for a different run sits in
    front of the one being looked for; the match is by name, not position."""
    mcp = FakeBoardMCP(
        boards=[{"id": "brd-other", "url": "https://x", "name": "receipts other-scenario run-0000"}]
    )
    report = make_report()
    result = await ensure_board(report, mcp, environment_slug="receipts-demo")
    assert result.created is True  # the existing board's name does not match
    assert mcp.created_count == 1


async def test_a_failed_create_board_is_recorded_and_never_raises() -> None:
    mcp = FakeBoardMCP(create_result=_Result(raw={}, text="quota exceeded", is_error=True))
    result = await ensure_board(make_report(), mcp, environment_slug="receipts-demo")
    assert result.created is False
    assert result.board_id is None
    assert result.error == "quota exceeded"


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
