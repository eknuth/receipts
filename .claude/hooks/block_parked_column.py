"""PreToolUse hook on Bash: a parked results column sits four levels deep.

What it replaces: the gotcha in every handoff memory since 2026-09-05. The
report reads `evals/results/*/*/*/grade.json`, so a column moved to
`evals/results/<park>/<scenario>/<n>` is counted as a live config named
`<park>`, and the mean in `evals/report.md` moves for a reason that has
nothing to do with the change under test. It happened once with a three-level
park and was found by reading the report. A parked column goes to
`evals/results/<park>/<config>/<scenario>/<n>`.

The hook simulates the command's `mkdir`, `mv`, and `cp` segments in order,
following `cd`, and blocks only when the final layout leaves cells at three
levels under `evals/results/` under a name that is not a live config (`full`,
`no-negation`, `no-notchecked`, each with an optional provider suffix such as
`full-nvidia`). A destination that exists on disk, was created earlier in the
same command, or ends in a slash is read as "move into", so
`mkdir -p evals/results/r18-pass && mv evals/results/full evals/results/r18-pass`
lands at four levels and passes, and so does a move through a temporary name
that ends at four levels. Renaming a live column to another live name passes.
`rsync`, `tar`, and moves done from inside a script are not covered.
Everything else passes through: exit 0, no output, and so does any unexpected
error. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

LIVE_COLUMN = re.compile(r"^(full|no-negation|no-notchecked)(-[a-z0-9]+)?$")
RESULTS = "evals/results/"
SEPARATORS = set("&|;\n()")


def segments(command: str) -> list[list[str]]:
    text = command.split("<<", 1)[0]
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars="();|&\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        tokens = text.replace("&&", " ; ").split()
    out: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok and set(tok) <= SEPARATORS:
            if current:
                out.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        out.append(current)
    return out


def under_results(path: str) -> str | None:
    """The path relative to `evals/results/` without a trailing slash, or None."""
    idx = path.find(RESULTS)
    if idx < 0:
        return None
    rest = path[idx + len(RESULTS) :].strip("/")
    return "/".join(p for p in rest.split("/") if p and p != ".")


def depth(rel: str) -> int:
    return len(rel.split("/")) if rel else 0


def resolve(base: Path, target: str) -> Path:
    expanded = os.path.expanduser(target)
    return Path(expanded) if os.path.isabs(expanded) else base / expanded


class Layout:
    """What the command does to `evals/results/`, tracked by relative path.

    `cells_below` maps a relative path to how many levels its cells sit below
    it: 2 for a config, 1 for a scenario, 0 for a cell. `created` holds the
    directories `mkdir` made in this command.
    """

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.cells_below: dict[str, int] = {}
        self.created: set[str] = set()

    def is_dir(self, raw: str, rel: str) -> bool:
        if rel in self.created:
            return True
        return resolve(self.cwd, raw.rstrip("/") or "/").is_dir()

    def cells_of(self, rel: str) -> int | None:
        if rel in self.cells_below:
            return self.cells_below[rel]
        d = depth(rel)
        return 3 - d if 1 <= d <= 3 else None

    def mkdir(self, args: list[str]) -> None:
        for raw in args:
            if raw.startswith("-"):
                continue
            rel = under_results(raw)
            if rel is not None:
                self.created.add(rel)

    def move(self, args: list[str], *, copy: bool) -> None:
        paths = [a for a in args if not a.startswith("-")]
        if len(paths) < 2:
            return
        raw_dest = paths[-1]
        dest = under_results(raw_dest)
        for raw_src in paths[:-1]:
            src = under_results(raw_src)
            if src is None:
                continue
            cells = self.cells_of(src)
            if not copy:
                self.cells_below.pop(src, None)
            if dest is None or cells is None:
                continue
            into = raw_dest.endswith("/") or self.is_dir(raw_dest, dest)
            final = f"{dest}/{Path(src).name}".strip("/") if into else dest
            self.cells_below[final] = cells
            if not copy:
                self.created.discard(src)

    def offenders(self) -> list[str]:
        out = []
        for rel, cells in self.cells_below.items():
            if depth(rel) + cells == 3 and not LIVE_COLUMN.match(rel.split("/")[0]):
                out.append(rel)
        return out


def check(command: str, cwd: Path) -> str | None:
    if RESULTS not in command:
        return None
    layout = Layout(cwd)
    for seg in segments(command):
        head = seg[0]
        if head == "cd":
            layout.cwd = resolve(layout.cwd, seg[1]) if len(seg) > 1 else Path.home()
        elif head == "mkdir":
            layout.mkdir(seg[1:])
        elif head in ("mv", "cp"):
            layout.move(seg[1:], copy=head == "cp")
    offenders = layout.offenders()
    if not offenders:
        return None
    where = ", ".join(f"evals/results/{rel}" for rel in offenders)
    return (
        f"blocked: {where} would leave cells at three levels under evals/results/, where "
        "evals/report.py reads them as a live config. Park a column four levels deep: "
        "mkdir -p evals/results/<park> && mv evals/results/<config> "
        "evals/results/<park>/<config> (CLAUDE.md, Tooling)."
    )


def run() -> int:
    payload = json.load(sys.stdin)
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload["tool_input"]["command"]
    if not isinstance(command, str):
        return 0
    reason = check(command, Path(str(payload.get("cwd") or ".")))
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


def main() -> int:
    try:
        return run()
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
