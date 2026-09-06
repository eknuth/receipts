"""PreToolUse hook on Bash: one emit at a time.

What it replaces: the sentence in CLAUDE.md that says never run two emits at
once. The team's ingest limit is 4,000 events per second; going over returns
HTTP 200 with an empty `partial_success` and the spans are dropped, with an
email at most once a day as the only notice. The issue that added this hook
records two emits run at once. The NVIDIA pass queued behind the EDW-1367
after-pass on 2026-09-05 was started by a loop that polled the other pass's log
once a minute, the shape in which the tail of one pass overlaps the head of
the next, and the memory file says never again. Over thirty commands in the
transcripts are a person checking for a running process by hand before
starting a pass. This does that check in code.

Blocks, with exit 2 and a reason on stderr, any command that would start an
emit (`evals.run` with `--emit`, `gen.emit` or `gen/emit.py`, or a pass script
named `*pass*.sh`) while a process whose command line matches
`evals.run|gen.emit` is already running.

Known gap: a pass launched as a script (`nohup zsh r20pass.sh &`) shows up in
`pgrep` under the script's name only until its first `evals.run` child starts,
and the hook sees the command text, not what the script will run. Naming the
script `*pass*.sh` is what makes the launch visible here; `pass-run` says so.
The pattern can be overridden with `RECEIPTS_EMIT_PATTERN` so the test can
plant a harmless marker process. Everything else passes through: exit 0, no
output, and so does any unexpected error. Standard library only, no network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

STARTS_EMIT = re.compile(
    r"(evals\.run\b[^\n;|&]*--emit)|(\bgen\.emit\b)|(gen/emit\.py)|(\S*pass\S*\.sh\b)"
)
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


def run() -> int:
    payload = json.load(sys.stdin)
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload["tool_input"]["command"]
    if not isinstance(command, str):
        return 0
    reason = check(command, os.environ.get("RECEIPTS_EMIT_PATTERN", DEFAULT_PATTERN))
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
