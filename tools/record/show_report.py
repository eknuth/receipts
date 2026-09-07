"""Print the report `python -m agent` wrote for a run id, top first.

The agent's own render scrolls past the top of a terminal on a long report, so the
recording clears the screen and prints the same render again from the saved
`report.json`. Nothing here is typed by hand: the render is `agent.__main__.render`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

from agent.__main__ import render
from agent.report import Report

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: show_report.py <run id>", file=sys.stderr)
        return 2
    path = ROOT / "evals" / "results" / argv[0] / "report.json"
    if not path.exists():
        print(f"no report at {path}", file=sys.stderr)
        return 1
    render(Report.model_validate_json(path.read_text()), Console())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
