"""PostToolUse hook on Bash: after a commit, say so if lint is red.

What it replaces: the CLAUDE.md rule that `make lint` and `make test` pass
before an issue is called done, applied at the commit. The transcripts show
over three hundred lint runs and about a hundred commits, and one PR where the
lint run happened after the commit that needed it. A PreToolUse block on every
commit would make each commit wait on the lint run; a PostToolUse warning costs
the same second but never stands between a person and their commit.

After any `git commit` this runs `make lint` at the top level of the git
repository the session's `cwd` is in (`git rev-parse --show-toplevel`), so a
worktree session lints the checkout it committed to; `CLAUDE_PROJECT_DIR` is
the fallback outside a repository. In a worktree session `CLAUDE_PROJECT_DIR`
stays at the main checkout, so the hook that runs is main's copy, and it
inspects the repository at the payload `cwd`, which is the worktree. When lint
fails, the tail of its output goes to stderr with exit 2, which PostToolUse
feeds back to the model as a warning;
the commit already happened and nothing is undone. When it passes, or the
command was not a commit, exit 0 with no output, and so does any unexpected
error. Standard library only, no network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

IS_COMMIT = re.compile(r"\bgit\b[^\n;|&]*\bcommit\b")


def project_dir(cwd: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    fallback = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(fallback) if fallback else None


def lint(project: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["make", "lint"], cwd=project, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"make lint did not run: {exc}"
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output


def run() -> int:
    payload = json.load(sys.stdin)
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload["tool_input"]["command"]
    if not isinstance(command, str) or not IS_COMMIT.search(command):
        return 0
    project = project_dir(Path(str(payload.get("cwd") or ".")))
    if project is None or not (project / "Makefile").is_file():
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


def main() -> int:
    try:
        return run()
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
