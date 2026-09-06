"""PreToolUse hook on Bash: one emit at a time.

What it replaces: the sentence in CLAUDE.md that says never run two emits at
once. The team's ingest limit is 4,000 events per second; going over returns
HTTP 200 with an empty `partial_success` and the spans are dropped, with an
email at most once a day as the only notice. Two emits overlapped once on
2026-09-05 when an NVIDIA pass was queued behind an Anthropic pass, and the
memory file says never again. Thirty commands in the transcripts are a person
checking `pgrep` by hand before starting a pass. This does that check in code.

Blocks, with exit 2 and a reason on stderr, any command that would start an
emit (`evals.run` with `--emit`, or `gen.emit` / `gen/emit.py`) while a
process whose command line matches `evals.run|gen.emit` is already running.
The pattern can be overridden with `RECEIPTS_EMIT_PATTERN` so the test can
plant a harmless marker process. Everything else passes through: exit 0, no
output. Standard library only, no network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

STARTS_EMIT = re.compile(r"(evals\.run\b[^\n;|&]*--emit)|(\bgen\.emit\b)|(gen/emit\.py)")
DEFAULT_PATTERN = r"evals\.run|gen\.emit"


def running_pids(pattern: str) -> list[str]:
    try:
        proc = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [pid for pid in proc.stdout.split() if pid.isdigit() and int(pid) != os.getpid()]


def check(command: str, pattern: str) -> str | None:
    if not STARTS_EMIT.search(command):
        return None
    pids = running_pids(pattern)
    if not pids:
        return None
    return (
        f"emit blocked: a process matching '{pattern}' is already running (pid "
        f"{', '.join(pids)}). The team's ingest cap is 4,000 events per second; over it, "
        "Honeycomb returns 200 and drops spans silently. Wait for the running pass or emit "
        "to finish, then start this one (CLAUDE.md, Generator)."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = str((payload.get("tool_input") or {}).get("command", ""))
    reason = check(command, os.environ.get("RECEIPTS_EMIT_PATTERN", DEFAULT_PATTERN))
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
