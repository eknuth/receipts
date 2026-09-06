"""PreToolUse hook on Bash: one emit at a time.

What it replaces: the sentence in CLAUDE.md that says never run two emits at
once. The team's ingest limit is 4,000 events per second; going over returns
HTTP 200 with an empty `partial_success` and the spans are dropped, with an
email at most once a day as the only notice. EDW-1368's Context lists two
emits run at once among the things got wrong, and the handoff memory carries
the rule never to run overlapping emits. Over thirty commands in the
transcripts are a person checking for a running process by hand before
starting a pass. This does that check in code.

Blocks, with exit 2 and a reason on stderr, a command that would start an emit
while a process whose command line matches `evals.run|gen.emit` is already
running. A command starts an emit when one of its segments, after `nohup`,
`time`, `env`, and `VAR=x` prefixes are stripped, has `uv run python`,
`python`, or `python3` at its head with `-m evals.run` and `--emit` among the
arguments, or `-m gen.emit`, or `gen/emit.py`; or has `zsh`, `bash`, `sh`, or
a bare path at its head whose script basename is `*pass*.sh` (the one English
word that shape catches, `bypass.sh`, is excluded). Text after a heredoc
marker (`<<`) is a document, not shell. A command that only mentions a pass
script or an emit inside a string or a filename (`grep`, `cat`, `echo`,
`git commit -m`) passes: a pass runs for ninety minutes and the hook must not
refuse every command for that long.

Known gap: a pass launched as a script shows up in `pgrep` under the script's
name only until its first `evals.run` child starts, and the hook sees the
launch command, not what the script will run. Naming the script `*pass*.sh`
is what makes the launch visible here; `pass-run` says so. In a worktree
session `CLAUDE_PROJECT_DIR` stays at the main checkout, so the hook that runs
is main's copy; this hook reads only the command text and the process list.
The pattern can be overridden with `RECEIPTS_EMIT_PATTERN` so the test can
plant a harmless marker process. Everything else passes through: exit 0, no
output, and so does any unexpected error. Standard library only, no network.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

DEFAULT_PATTERN = r"evals\.run|gen\.emit"
PASS_SCRIPT = re.compile(r"^(?!bypass\.sh$)(?=.*pass).*\.sh$")
WRAPPERS = {"nohup", "time", "env", "caffeinate", "exec", "sudo"}
SHELLS = {"zsh", "bash", "sh"}
SEPARATORS = set("&|;\n()")


def segments(command: str) -> list[list[str]]:
    text = command.split("<<", 1)[0]
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars="();|&\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        tokens = text.replace("&&", " ; ").replace("||", " ; ").split()
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


def strip_prefixes(tokens: list[str]) -> list[str]:
    while tokens and (
        tokens[0] in WRAPPERS or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0])
    ):
        tokens = tokens[1:]
    return tokens


def python_args(tokens: list[str]) -> list[str] | None:
    """The arguments after a `python`, `python3`, or `uv run python[3]` head, else None."""
    if tokens[0] in ("python", "python3"):
        return tokens[1:]
    if tokens[0] == "uv" and len(tokens) >= 3 and tokens[1] == "run":
        if tokens[2] in ("python", "python3"):
            return tokens[3:]
    return None


def starts_emit(segment: list[str]) -> bool:
    tokens = strip_prefixes(segment)
    if not tokens:
        return False
    args = python_args(tokens)
    if args is not None:
        module = (
            args[args.index("-m") + 1]
            if "-m" in args and args.index("-m") + 1 < len(args)
            else None
        )
        if module == "evals.run" and "--emit" in args:
            return True
        if module == "gen.emit":
            return True
        return any(a == "gen/emit.py" or a.endswith("/gen/emit.py") for a in args)
    head = tokens[0]
    if head in SHELLS:
        scripts = [a for a in tokens[1:] if not a.startswith("-")]
        return bool(scripts) and bool(PASS_SCRIPT.match(Path(scripts[0]).name))
    if "/" in head or head.endswith(".sh"):
        return bool(PASS_SCRIPT.match(Path(head).name))
    return False


def running_pids(pattern: str) -> list[str]:
    try:
        proc = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [pid for pid in proc.stdout.split() if pid.isdigit() and int(pid) != os.getpid()]


def check(command: str, pattern: str) -> str | None:
    if not any(starts_emit(seg) for seg in segments(command)):
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
