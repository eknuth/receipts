"""Print the two summary tables of evals/report.md for the recording.

Reads the generated report and prints the "Scores by scenario" and "Process by
config" sections through rich, so the recording shows the same numbers the README
cites, from the same file, with nothing retyped.
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "evals" / "report.md"
SECTIONS = ("Scores by scenario", "Process by config")


def section(text: str, title: str) -> str:
    """The heading and table of one `## title` section, without its prose."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if match is None:
        raise SystemExit(f"no section '{title}' in {REPORT}")
    rows = [line for line in match.group(1).splitlines() if line.startswith("|")]
    return f"## {title}\n\n" + "\n".join(rows) + "\n"


def main() -> int:
    text = REPORT.read_text()
    console = Console()
    for title in SECTIONS:
        console.print(Markdown(section(text, title)))
        console.print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
