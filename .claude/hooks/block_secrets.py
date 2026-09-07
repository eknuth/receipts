"""PreToolUse hook on Bash: keep keys and the secrets file out of git.

What it replaces: the rule in CLAUDE.md that secrets come only from `.env` and
never reach code, a commit, a fixture, or `~/.claude.json`. Every session so
far has checked this by reading the diff before a commit, which is a step a
person or a model can skip once and never notice. The transcripts show over a
hundred commands that touch `.env` and about a hundred commits, so the check
now runs in code at the moment it matters.

Blocks, with exit 2 and a reason on stderr:

- `git add` or `git commit` naming `.env`, `.claude.json`, or
  `honeycomb_oauth.json` (agent/auth.py's OAuth token store, R12/EDW-1334;
  it defaults to `~/.receipts` outside the repo, but this catches it if one
  is ever staged from somewhere else) as a path.
- `git commit` when the staged diff (plus the working tree for `-a`) adds a
  line matching a key pattern: `sk-ant-`, `hcaik_`, `hcamk_`, `nvapi-`, an
  AWS access key id, `x-honeycomb-team:` followed by a key-shaped value, an
  OAuth `access_token` or `refresh_token` JSON field with a value, or
  `NVIDIA_API_KEY=` with a value on a line that is not in `.env.example`.
  Placeholders in angle brackets or starting with `$` pass. The stderr names
  the file and the pattern, never the value.

The diff is read where the commit will run: the hook walks the command's
segments in order, follows `cd <path>`, and honors `git -C <path>`, starting
from the payload's `cwd`. `.env.example` is read from that repository's top
level. In a worktree session `CLAUDE_PROJECT_DIR` stays at the main checkout,
so the hook that runs is main's copy, and it inspects the repository at the
payload `cwd`, which is the worktree. Text after a heredoc marker (`<<`) is a
document, not shell, and is not read. Everything else passes through: exit 0,
no output. Any unexpected error also exits 0, because a hook that fails closed
would block every Bash call. Standard library only, no network.
"""

from __future__ import annotations

import json
import os
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
    ("x-honeycomb-team header with a value", re.compile(r"x-honeycomb-team:\s*[A-Za-z0-9_-]{16,}")),
    # R12 (EDW-1334): agent/auth.py's OAuth token store, in case one is ever
    # staged from outside its default ~/.receipts location. Its JSON shape
    # is {"tokens": {"access_token": "...", "refresh_token": "...", ...}},
    # so a real one always has one of these keys with a non-empty value.
    ("OAuth access_token in a committed file", re.compile(r'"access_token"\s*:\s*"[^"\s]{8,}"')),
    ("OAuth refresh_token in a committed file", re.compile(r'"refresh_token"\s*:\s*"[^"\s]{8,}"')),
]
ENV_ASSIGNMENT = re.compile(r"^\s*(NVIDIA_API_KEY)=(\S*)")
FORBIDDEN_PATHS = (".env", ".claude.json", "honeycomb_oauth.json")
SEPARATORS = set("&|;\n()")


def shell_part(command: str) -> str:
    """The command text before the first heredoc marker."""
    return command.split("<<", 1)[0]


def segments(command: str) -> list[list[str]]:
    text = shell_part(command)
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


def git_call(tokens: list[str]) -> tuple[str | None, list[str], str | None]:
    """(subcommand, its arguments, the `-C` directory) for a git segment, else (None, [], None)."""
    if not tokens or tokens[0] != "git":
        return None, [], None
    rest = tokens[1:]
    directory = None
    while rest and rest[0].startswith("-"):
        if rest[0] == "-C" and len(rest) > 1:
            directory = rest[1]
            rest = rest[2:]
        elif rest[0] == "-c":
            rest = rest[2:]
        else:
            rest = rest[1:]
    return (rest[0], rest[1:], directory) if rest else (None, [], directory)


def resolve(base: Path, target: str) -> Path:
    expanded = os.path.expanduser(target)
    return (base / expanded).resolve() if not os.path.isabs(expanded) else Path(expanded)


def names_forbidden_path(args: list[str]) -> str | None:
    for arg in args:
        if Path(arg).name in FORBIDDEN_PATHS:
            return arg
    return None


def toplevel(directory: Path) -> Path:
    """The git top level of `directory`, else `CLAUDE_PROJECT_DIR`, else `directory`."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    fallback = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(fallback) if fallback else directory


def example_lines(project: Path) -> set[str]:
    path = project / ".env.example"
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(errors="replace").splitlines()}


def _git_diff(cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "diff", *args, "--no-color", "--unified=0"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def staged_diff(cwd: Path, *, include_working_tree: bool) -> str:
    proc = _git_diff(cwd, "HEAD") if include_working_tree else _git_diff(cwd, "--cached")
    if proc is not None and proc.returncode != 0:
        # No HEAD yet (first commit): the cached diff is against the empty tree.
        proc = _git_diff(cwd, "--cached")
    return proc.stdout if proc is not None and proc.returncode == 0 else ""


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
    directory = cwd
    for seg in segments(command):
        if seg[0] == "cd":
            directory = resolve(directory, seg[1]) if len(seg) > 1 else Path.home()
            continue
        sub, args, dash_c = git_call(seg)
        if sub not in ("add", "commit"):
            continue
        hit = names_forbidden_path(args)
        if hit:
            return f"git {sub} names {hit}: the secrets file stays out of git (CLAUDE.md, Rules)."
        if sub == "commit":
            where = resolve(directory, dash_c) if dash_c else directory
            all_tracked = any(
                a in ("-a", "--all")
                or (a.startswith("-") and not a.startswith("--") and "a" in a[1:])
                for a in args
            )
            diff = staged_diff(where, include_working_tree=all_tracked)
            findings = scan_diff(diff, example_lines(toplevel(where)))
            if findings:
                listed = "; ".join(sorted(set(findings)))
                return (
                    "commit blocked: an added line looks like a key or names the secrets file "
                    f"({listed}). Secrets come only from .env (CLAUDE.md, Rules). Remove the line, "
                    "or if it is a placeholder, write it in angle brackets."
                )
    return None


def run() -> int:
    payload = json.load(sys.stdin)
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload["tool_input"]["command"]
    if not isinstance(command, str) or "git" not in command:
        return 0
    cwd = Path(str(payload.get("cwd") or "."))
    reason = check(command, cwd)
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
