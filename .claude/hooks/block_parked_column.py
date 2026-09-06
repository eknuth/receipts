"""PreToolUse hook on Bash: a parked results column sits four levels deep.

What it replaces: the gotcha in every handoff memory since 2026-09-05. The
report reads `evals/results/*/*/*/grade.json`, so a column moved to
`evals/results/<park>/<scenario>/<n>` is counted as a live config named
`<park>`, and the mean in `evals/report.md` moves for a reason that has
nothing to do with the change under test. It happened once with a three-level
park and was found by reading the report. A parked column goes to
`evals/results/<park>/<config>/<scenario>/<n>`.

Blocks, with exit 2 and a reason on stderr, an `mv` or `cp` whose destination
would leave cells at three levels under `evals/results/` under a name that is
not a live config (`full`, `no-negation`, `no-notchecked`, each with an
optional provider suffix such as `full-nvidia`). Renaming a live column to
another live name passes. Everything else passes through: exit 0, no output.
Standard library only.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

LIVE_COLUMN = re.compile(r"^(full|no-negation|no-notchecked)(-[a-z0-9]+)?$")
RESULTS = "evals/results/"


def results_depth(path: str) -> int | None:
    """Components under `evals/results/`, or None when the path is not under it."""
    idx = path.find(RESULTS)
    if idx < 0:
        return None
    rest = path[idx + len(RESULTS) :].strip("/")
    return len([p for p in rest.split("/") if p and p != "."]) if rest else 0


def top_component(path: str) -> str:
    idx = path.find(RESULTS)
    rest = path[idx + len(RESULTS) :].strip("/")
    return rest.split("/")[0] if rest else ""


def segments(command: str) -> list[list[str]]:
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars="();|&\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        tokens = command.replace("&&", " ; ").split()
    out: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok and set(tok) <= set("&|;\n()"):
            if current:
                out.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        out.append(current)
    return out


def check(command: str) -> str | None:
    if RESULTS not in command:
        return None
    for seg in segments(command):
        if not seg or seg[0] not in ("mv", "cp"):
            continue
        paths = [t for t in seg[1:] if not t.startswith("-")]
        if len(paths) < 2:
            continue
        dest = paths[-1]
        d_dest = results_depth(dest)
        if d_dest is None:
            continue
        for src in paths[:-1]:
            d_src = results_depth(src)
            if d_src is None or d_src == 0 or d_src > 3:
                continue
            # Cells sit 3 - d_src levels below the source. After the move they sit at
            # d_dest + 3 - d_src. Three means the report glob reads them as live.
            if d_dest + 3 - d_src == 3 and not LIVE_COLUMN.match(top_component(dest)):
                return (
                    f"{seg[0]} blocked: {dest} would leave cells at three levels under "
                    "evals/results/, where evals/report.py reads them as a live config. Park a "
                    "column four levels deep: mkdir -p evals/results/<park> && mv "
                    "evals/results/<config> evals/results/<park>/<config> (CLAUDE.md, Tooling)."
                )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = str((payload.get("tool_input") or {}).get("command", ""))
    reason = check(command)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
