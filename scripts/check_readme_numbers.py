"""Assert that every number in README.md is one a reader can go and check.

    uv run python scripts/check_readme_numbers.py

The README is the argument, and its numbers are its evidence. This script is the
same rule the project puts on the agent, turned on the README: a figure may only
appear here if it appears in a generated file that something else produced, or in
a short allowlist below where every entry names where it came from.

Four checks:

1. Every numeric token in the README prose and tables appears as a numeric token
   in `evals/report.md` (generated from the grade files), in `evals/grader.md`
   (the weights and penalties), or in ALLOWLIST.
2. The em dash count is zero.
3. The "What it does not do" section has exactly five bullets.
4. The prose word count, outside tables and fenced code, is between 900 and 1300.

Code fences and inline code spans are stripped before tokens are collected, so a
command line or an identifier in backticks is not held to the rule; a table cell
is, because a reader reads a table as a claim.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SOURCES = (ROOT / "evals" / "report.md", ROOT / "evals" / "grader.md")

MIN_WORDS = 900
MAX_WORDS = 1300
NOT_DONE_HEADING = "## What it does not do"
NOT_DONE_BULLETS = 5

# Numbers the generated files do not carry. Each one names its source.
ALLOWLIST: dict[str, str] = {
    # `requires-python = ">=3.12"` in pyproject.toml, quoted in the "Run it" prerequisites.
    "3.12": "pyproject.toml requires-python",
}

# A run of digits, with optional thousands separators, decimal part, and percent sign.
# The separator alternative comes first so `1,501,110` wins over `1`, and neither
# alternative can end on a comma, so a number at the end of a clause stays a number.
NUMBER = re.compile(r"\d+(?:,\d{3})+(?:\.\d+)?%?|\d+(?:\.\d+)?%?")
FENCE = re.compile(r"^```")
INLINE_CODE = re.compile(r"`[^`]*`")


def strip_code(text: str) -> str:
    """Drop fenced blocks and inline code spans, keeping line structure."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if FENCE.match(line.strip()):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else INLINE_CODE.sub(" ", line))
    return "\n".join(out)


def tokens_with_lines(text: str) -> list[tuple[str, int]]:
    """Every numeric token in `text`, paired with its 1-based line number.

    A leading `-` counts as part of the number only when it starts a word, so
    `-0.50` in a table cell is negative and the `4` in `claude-sonnet-4-5` is not.
    """
    found: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in NUMBER.finditer(line):
            start = match.start()
            token = match.group(0)
            if start > 0 and line[start - 1] == "-":
                before = line[start - 2] if start >= 2 else ""
                if before == "" or before.isspace() or before in "([":
                    token = "-" + token
            found.append((token, lineno))
    return found


def source_tokens() -> set[str]:
    known = set(ALLOWLIST)
    for path in SOURCES:
        known.update(token for token, _ in tokens_with_lines(strip_code(path.read_text())))
    return known


def prose_words(text: str) -> int:
    """Words outside tables and fenced code, counted on the raw README."""
    count = 0
    in_fence = False
    for line in text.splitlines():
        if FENCE.match(line.strip()):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith("|"):
            continue
        count += len(line.split())
    return count


def not_done_bullets(text: str) -> int:
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == NOT_DONE_HEADING)
    except StopIteration:
        return -1
    count = 0
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            count += 1
    return count


def main() -> int:
    raw = README.read_text()
    known = source_tokens()
    failures: list[str] = []

    unknown = [
        (token, lineno)
        for token, lineno in tokens_with_lines(strip_code(raw))
        if token not in known
    ]
    if unknown:
        failures.append(
            f"{len(unknown)} number(s) in README.md appear in neither "
            f"{SOURCES[0].name}, {SOURCES[1].name}, nor the allowlist:"
        )
        lines = raw.splitlines()
        for token, lineno in unknown:
            failures.append(f"  line {lineno}: {token}   in: {lines[lineno - 1].strip()[:90]}")

    em_dashes = raw.count("—")
    if em_dashes:
        failures.append(f"em dash count is {em_dashes}, expected 0")

    bullets = not_done_bullets(raw)
    if bullets == -1:
        failures.append(f"no {NOT_DONE_HEADING!r} section found")
    elif bullets != NOT_DONE_BULLETS:
        failures.append(f"{NOT_DONE_HEADING!r} has {bullets} bullets, expected {NOT_DONE_BULLETS}")

    words = prose_words(raw)
    print(f"README prose words (outside tables and code fences): {words}")
    if not MIN_WORDS <= words <= MAX_WORDS:
        failures.append(f"prose word count {words} is outside {MIN_WORDS} to {MAX_WORDS}")

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("README.md: every number checks out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
