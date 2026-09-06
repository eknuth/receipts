"""PreToolUse hook on Bash: keep keys and the secrets file out of git.

What it replaces: the rule in CLAUDE.md that secrets come only from `.env` and
never reach code, a commit, a fixture, or `~/.claude.json`. Every session so
far has checked this by reading the diff before a commit, which is a step a
person or a model can skip once and never notice. The transcripts show 119
commands that touch `.env` and about a hundred commits, so the check now runs
in code at the moment it matters.

Blocks, with exit 2 and a reason on stderr:

- `git add` or `git commit` naming `.env` or `.claude.json` as a path.
- `git commit` when the staged diff (plus the working tree for `-a`) adds a
  line matching a key pattern: `sk-ant-`, `hcaik_`, `hcamk_`, `nvapi-`, an
  AWS access key id, `x-honeycomb-team:` with a value, or `NVIDIA_API_KEY=`
  with a value on a line that is not in `.env.example`. Placeholders in angle
  brackets or starting with `$` pass. The stderr names the file and the
  pattern, never the value.

Everything else passes through: exit 0, no output. Standard library only.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

KEY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("sk-ant- key", re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")),
    ("hcaik_ ingest key", re.compile(r"hcaik_[A-Za-z0-9]{8,}")),
    ("hcamk_ management key", re.compile(r"hcamk_[A-Za-z0-9]{8,}")),
    ("nvapi- key", re.compile(r"nvapi-[A-Za-z0-9_-]{8,}")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("x-honeycomb-team header with a value", re.compile(r"x-honeycomb-team:\s*(?![<$\s])\S+")),
]
ENV_ASSIGNMENT = re.compile(r"^\+?\s*(NVIDIA_API_KEY)=(\S*)")
FORBIDDEN_PATHS = (".env", ".claude.json")


def git_subcommand(tokens: list[str]) -> tuple[str | None, list[str]]:
    """The git subcommand and its arguments, skipping `-c k=v` and other leading options."""
    if not tokens or tokens[0] != "git":
        return None, []
    rest = tokens[1:]
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in ("-c", "-C") else rest[1:]
    return (rest[0], rest[1:]) if rest else (None, [])


def segments(command: str) -> list[list[str]]:
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars="();|&\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        tokens = command.replace("&&", " ; ").replace("||", " ; ").split()
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


def names_forbidden_path(args: list[str]) -> str | None:
    for arg in args:
        base = Path(arg).name
        if base in FORBIDDEN_PATHS:
            return arg
    return None


def example_lines(project: Path) -> set[str]:
    path = project / ".env.example"
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(errors="replace").splitlines()}


def staged_diff(cwd: Path, *, include_working_tree: bool) -> str:
    args = ["git", "diff", "HEAD"] if include_working_tree else ["git", "diff", "--cached"]
    try:
        proc = subprocess.run(
            [*args, "--no-color", "--unified=0"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        # No HEAD yet (first commit): fall back to the cached diff against the empty tree.
        try:
            proc = subprocess.run(
                ["git", "diff", "--cached", "--no-color", "--unified=0"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return proc.stdout


def scan_diff(diff: str, allowed: set[str]) -> list[str]:
    """Findings as `file: pattern` strings for every added line that looks like a key."""
    findings = []
    current = "?"
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        if Path(current).name in FORBIDDEN_PATHS:
            findings.append(f"{current}: the secrets file itself")
            continue
        for label, pattern in KEY_PATTERNS:
            if pattern.search(added):
                findings.append(f"{current}: {label}")
        m = ENV_ASSIGNMENT.match(added)
        if m and m.group(2) and not m.group(2).startswith(("<", "$")):
            if added.strip() not in allowed:
                findings.append(f"{current}: {m.group(1)}= with a value not in .env.example")
    return findings


def check(command: str, cwd: Path) -> str | None:
    """The reason to block, or None."""
    for seg in segments(command):
        sub, args = git_subcommand(seg)
        if sub not in ("add", "commit"):
            continue
        hit = names_forbidden_path(args)
        if hit:
            return f"git {sub} names {hit}: the secrets file stays out of git (CLAUDE.md, Rules)."
        if sub == "commit":
            all_tracked = any(
                a in ("-a", "--all")
                or (a.startswith("-") and "a" in a[1:] and not a.startswith("--"))
                for a in args
            )
            findings = scan_diff(
                staged_diff(cwd, include_working_tree=all_tracked), example_lines(cwd)
            )
            if findings:
                listed = "; ".join(sorted(set(findings)))
                return (
                    "commit blocked: an added line looks like a key or names the secrets file "
                    f"({listed}). Secrets come only from .env (CLAUDE.md, Rules). Remove the line, "
                    "or if it is a placeholder, write it in angle brackets."
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
    if "git" not in command:
        return 0
    cwd = Path(payload.get("cwd") or ".")
    reason = check(command, cwd)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
