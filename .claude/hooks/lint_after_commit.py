"""PostToolUse hook on Bash: after a commit, say so if lint is red.

What it replaces: the CLAUDE.md rule that `make lint` and `make test` pass
before an issue is called done, applied at the commit. The transcripts show
over three hundred lint runs and about a hundred commits, and one PR where the
lint run happened after the commit that needed it. A PreToolUse block on every commit
would make each commit wait on the lint run; a PostToolUse warning costs the
same second but never stands between a person and their commit.

After any `git commit` this runs `make lint` in the project directory
(`CLAUDE_PROJECT_DIR`, else the hook's cwd). When it fails, the tail of the
lint output goes to stderr with exit 2, which PostToolUse feeds back to the
model as a warning; the commit already happened and nothing is undone. When
it passes, or the command was not a commit, exit 0 with no output. Standard
library only, no network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

IS_COMMIT = re.compile(r"\bgit\b[^\n;|&]*\bcommit\b")


def lint(project: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["make", "lint"], cwd=project, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"make lint did not run: {exc}"
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not IS_COMMIT.search(command):
        return 0
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".")
    if not (project / "Makefile").exists():
        return 0
    ok, output = lint(project)
    if ok:
        return 0
    tail = "\n".join(output.splitlines()[-15:])
    print(
        "lint is red after this commit; fix it and amend or commit again before calling "
        f"the issue done (CLAUDE.md, Rules).\n{tail}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
