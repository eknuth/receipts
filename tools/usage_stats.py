"""Count how Claude Code was used on Receipts, from the session transcripts.

Reads every session JSONL under the Claude Code project directory (the
orchestrator sessions at the top level and the subagent sessions under
`<session>/subagents/`) and prints markdown tables of aggregates: tool calls by
tool name, shell commands by normalized shape, subagent spawns, permission
denials, user turn counts, and the size of the memory directory. It never
prints a line of conversation text, a tool result, a command argument, or a
memory file name: only counts, tool names, normalized command shapes, and the
short label an Agent call carries as its `description`. A final scrub replaces
any key-looking substring that still reaches the output, and a test pins that.

    uv run python tools/usage_stats.py [--root DIR] [--top N] [--before ISO-8601]

`--before` drops every transcript entry stamped after the given time, so a
run over a live session reproduces: the tables in `docs/agentic-workflow.md`
carry the timestamp they were made with, and a test regenerates them.

Method notes, so the numbers can be read honestly:

- One JSONL line per event. Tool calls are `tool_use` blocks in assistant
  messages. Tool results are `tool_result` blocks in user messages. User turns
  are user messages whose content is text, with the meta and tool-result ones
  excluded, and lines that start with `<` (injected notifications) excluded.
  A session with no turns and no tool calls (or none before the cutoff) is
  dropped.
- A shell command is cut at a heredoc or an inline program, tokenized with
  `shlex` so a `|` inside a quoted pattern stays put, split at `&&`, `||`,
  `;`, `|`, and newlines, and each segment is reduced to a shape: the first
  token, plus the subcommand for `git`, `gh`, `make`, `uv run`, and the module
  for `python -m`. Paths become `<path>`. So `cd ~/proj && git status` counts
  once for `cd` and once for `git status`.
- Denials are the `toolDenialKind` field on a transcript line plus tool results
  that begin "Permission for this action was denied". Permission prompts that
  were approved leave no record in the transcript, so they cannot be counted.
- Transcripts can be compacted or truncated by Claude Code; a session whose
  early turns were summarized away undercounts. The session table shows what
  each file holds.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".claude" / "projects" / "-Users-eknuth-proj-receipts"
MAX_LINE = 120
SECRET_RE = re.compile(r"sk-ant|hcaik|hcamk|nvapi|Bearer|AKIA[0-9A-Z]{4,}")
DENIAL_PREFIX = "Permission for this action was denied"
INTERRUPT_PREFIX = "[Request interrupted by user"
SHORT_TURN = 200

# Families of shell activity that the tooling in `.claude/` is meant to carry.
# Each is a regex over the whole command text; a command counts once per family.
FAMILIES: list[tuple[str, str]] = [
    ("eval pass with --emit", r"evals\.run\b.*--emit"),
    ("eval pass without --emit", r"evals\.run\b(?!.*--emit)"),
    ("render the report", r"evals\.report\b"),
    ("park or move a results column", r"\bmv\b.*evals/results"),
    ("read grade.json or report.json by hand", r"grade\.json|report\.json"),
    ("read a tool_log by hand", r"tool_log"),
    ("check for a running process", r"\bpgrep\b|\bps aux\b|\bps -ef\b"),
    ("read the gen/runs manifests", r"gen/runs"),
    ("branch for an issue", r"git checkout -b|git switch -c"),
    ("commit", r"\bgit\b[^\n;|&]*\bcommit\b"),
    ("run tests", r"uv run pytest|make test"),
    ("run lint", r"uv run ruff|make lint"),
    ("touch .env", r"\.env\b"),
    ("gh pr", r"\bgh pr\b"),
]

# Families of user turns, over the orchestrator sessions. A turn counts once per family.
USER_FAMILIES: list[tuple[str, str]] = [
    (
        "asks for a handoff prompt or to clear context",
        r"prompt for the next|clear (the )?context|handoff",
    ),
    ("asks what is next", r"what'?s next|what next"),
    ("says merge", r"\bmerge"),
    ("asks for an adversarial review", r"adversarial"),
    ("corrects the voice", r"litotes|irony|em dash"),
    ("asks about credits or budget", r"credit|budget|\$\d"),
    ("names the browser harness", r"browser"),
]

WRAPPERS = {"nohup", "time", "caffeinate", "exec", "sudo", "env"}
KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "{", "}", "(", ")"}
CLOSERS = {"done", "fi", "esac"}
# A line that starts inline program text: the rest of the command is that program, not shell.
PROGRAM_START = re.compile(r"""(python3?|uv run python3?)\s+(-c\s+["']|-\s*$|-\s*<<)""")


@dataclass
class Session:
    name: str
    kind: str  # "orchestrator" or "subagent"
    first_ts: str = ""
    last_ts: str = ""
    assistant_turns: int = 0
    user_turns: int = 0
    short_user_turns: int = 0
    interrupts: int = 0
    tools: Counter = field(default_factory=Counter)
    shapes: Counter = field(default_factory=Counter)
    families: Counter = field(default_factory=Counter)
    user_families: Counter = field(default_factory=Counter)
    spawns: list[dict] = field(default_factory=list)
    denials: Counter = field(default_factory=Counter)
    bad_lines: int = 0

    @property
    def empty(self) -> bool:
        return not (self.assistant_turns or self.user_turns or self.tools)


def shape_of(segment: list[str]) -> str | None:
    """The normalized shape of one tokenized shell segment, or None for an empty one."""
    tokens = list(segment)
    while tokens and (
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0])
        or tokens[0] in WRAPPERS
        or tokens[0] in KEYWORDS
    ):
        tokens = tokens[1:]
    if not tokens or tokens[0] in CLOSERS or tokens[0][0] in "\"'#":
        return None
    head = tokens[0]
    if head.startswith(("/", "~", "$", ".")) or head.endswith((".sh", ".py")):
        return "<path>"
    if head in ("uv",) and len(tokens) >= 3 and tokens[1] == "run":
        rest = tokens[2:]
        if rest[0] in ("python", "python3"):
            return "uv run python " + _python_shape(rest[1:])
        if rest[0] in ("ruff",) and len(rest) > 1:
            return f"uv run ruff {rest[1]}"
        return f"uv run {rest[0]}"
    if head in ("python", "python3"):
        return f"{head} " + _python_shape(tokens[1:])
    if head == "git":
        rest = tokens[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-c", "-C") else rest[1:]
        return f"git {rest[0]}" if rest else "git"
    if head in ("make", "linear", "docker", "cargo", "npm"):
        return f"{head} {tokens[1]}" if len(tokens) > 1 else head
    if head == "gh":
        return " ".join([head, *tokens[1:3]])
    return head


def _python_shape(rest: list[str]) -> str:
    if not rest:
        return "<repl>"
    if rest[0] == "-m" and len(rest) > 1:
        return f"-m {rest[1]}"
    if rest[0] in ("-c", "-"):
        return rest[0]
    if rest[0].startswith("-"):
        return rest[0]
    return "<script>"


def segments(command: str) -> list[list[str]]:
    """Split a shell command into token lists, one per simple command.

    The text is cut at a heredoc or an inline program first. Then `shlex`
    tokenizes it with `&`, `|`, `;`, and newline as punctuation, so a `|`
    inside a quoted grep pattern stays inside its token. A command `shlex`
    cannot tokenize (an unbalanced quote) falls back to a plain split.
    """
    lines = []
    for line in command.splitlines():
        lines.append(line)
        if "<<" in line or PROGRAM_START.search(line):
            break
    text = "\n".join(lines)
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return [s.split() for s in re.split(r"&&|\|\||;|\||\n", text) if s.strip()]
    out: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok and set(tok) <= set("&|;\n"):
            if current:
                out.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        out.append(current)
    return out


def command_shapes(command: str) -> list[str]:
    out = []
    for seg in segments(command):
        shape = shape_of(seg)
        if shape:
            out.append(shape)
    return out


def command_families(command: str) -> list[str]:
    return [name for name, pattern in FAMILIES if re.search(pattern, command, re.S)]


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _blocks(record: dict) -> list[dict]:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def read_session(path: Path, kind: str, before: datetime | None = None) -> Session:
    s = Session(name=path.stem, kind=kind)
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                s.bad_lines += 1
                continue
            ts = rec.get("timestamp")
            if before is not None:
                stamped = parse_ts(ts)
                if stamped is not None and stamped > before:
                    continue
            if isinstance(ts, str):
                s.first_ts = s.first_ts or ts
                s.last_ts = ts
            if rec.get("toolDenialKind"):
                s.denials[str(rec["toolDenialKind"])] += 1
            rtype = rec.get("type")
            blocks = _blocks(rec)
            if rtype == "assistant":
                s.assistant_turns += 1
                for b in blocks:
                    if b.get("type") != "tool_use":
                        continue
                    name = str(b.get("name"))
                    s.tools[name] += 1
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    if name == "Bash":
                        cmd = str(inp.get("command", ""))
                        s.shapes.update(command_shapes(cmd))
                        s.families.update(command_families(cmd))
                    elif name == "Agent":
                        s.spawns.append(
                            {
                                "description": str(inp.get("description", ""))[:60],
                                "model": str(inp.get("model") or "inherit"),
                                "type": str(inp.get("subagent_type") or "default"),
                                "prompt_chars": len(str(inp.get("prompt", ""))),
                            }
                        )
            elif rtype == "user" and not rec.get("isMeta"):
                if any(b.get("type") == "tool_result" for b in blocks):
                    for b in blocks:
                        is_result = b.get("type") == "tool_result"
                        if is_result and _result_text(b).startswith(DENIAL_PREFIX):
                            s.denials["denial text in a tool result"] += 1
                    continue
                text = " ".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
                text = text.strip()
                if not text or text.startswith("<"):
                    continue
                if text.startswith(INTERRUPT_PREFIX):
                    s.interrupts += 1
                    continue
                s.user_turns += 1
                if len(text) < SHORT_TURN:
                    s.short_user_turns += 1
                lowered = text.lower()
                for name, pattern in USER_FAMILIES:
                    if re.search(pattern, lowered):
                        s.user_families[name] += 1
    return s


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


def load(root: Path, before: datetime | None = None) -> list[Session]:
    sessions = [read_session(p, "orchestrator", before) for p in sorted(root.glob("*.jsonl"))]
    sessions += [
        read_session(p, "subagent", before) for p in sorted(root.glob("*/subagents/*.jsonl"))
    ]
    return [s for s in sessions if not s.empty]


def memory_summary(root: Path) -> tuple[int, int]:
    """(file count, total words) of the memory directory. File names are never printed."""
    files = sorted((root / "memory").glob("*.md"))
    words = sum(len(p.read_text(encoding="utf-8", errors="replace").split()) for p in files)
    return len(files), words


def table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return lines


def render(sessions: list[Session], memory: tuple[int, int], top: int) -> list[str]:
    orch = [s for s in sessions if s.kind == "orchestrator"]
    subs = [s for s in sessions if s.kind == "subagent"]
    out: list[str] = []
    dates = sorted(s.first_ts[:10] for s in orch if s.first_ts)
    span = f"{dates[0]} to {dates[-1]}" if dates else "no dated sessions"

    out.append("## Sessions")
    out.append("")
    out.append(
        f"{len(orch)} orchestrator sessions and {len(subs)} subagent sessions, {span}. "
        f"Bad JSON lines skipped: {sum(s.bad_lines for s in sessions)}."
    )
    out.append("")
    rows = []
    for s in orch:
        rows.append(
            [
                s.name[:8],
                s.first_ts[:10],
                s.assistant_turns,
                s.user_turns,
                s.short_user_turns,
                s.interrupts,
                sum(s.tools.values()),
                s.tools["Bash"],
                s.tools["Agent"],
            ]
        )
    out += table(
        [
            "session",
            "date",
            "assistant",
            "user",
            "user<200",
            "interrupts",
            "tools",
            "bash",
            "spawns",
        ],
        rows,
    )
    tot_user = sum(s.user_turns for s in orch)
    tot_short = sum(s.short_user_turns for s in orch)
    tot_int = sum(s.interrupts for s in orch)
    out.append("")
    out.append(
        f"Orchestrator user turns: {tot_user}, of which {tot_short} under {SHORT_TURN} characters; "
        f"{tot_int} interrupts."
    )
    out.append("")
    out.append("## User turns by family")
    out.append("")
    uf = Counter()
    for s in orch:
        uf.update(s.user_families)
    out += table(
        ["family (orchestrator turns matching)", "count"], [[n, uf[n]] for n, _ in USER_FAMILIES]
    )
    out.append("")

    out.append("## Tool calls by tool name")
    out.append("")
    ot, st = Counter(), Counter()
    for s in orch:
        ot.update(s.tools)
    for s in subs:
        st.update(s.tools)
    names = sorted(set(ot) | set(st), key=lambda n: (-(ot[n] + st[n]), n))
    rows = [[n, ot[n], st[n], ot[n] + st[n]] for n in names[:top]]
    rows.append(["total", sum(ot.values()), sum(st.values()), sum(ot.values()) + sum(st.values())])
    out += table(["tool", "orchestrator", "subagent", "all"], rows)
    out.append("")

    out.append("## Shell commands by shape")
    out.append("")
    osh, ssh = Counter(), Counter()
    for s in orch:
        osh.update(s.shapes)
    for s in subs:
        ssh.update(s.shapes)
    shapes = sorted(set(osh) | set(ssh), key=lambda n: (-(osh[n] + ssh[n]), n))
    rows = [[n, osh[n], ssh[n], osh[n] + ssh[n]] for n in shapes[:top]]
    total_o, total_s = sum(osh.values()), sum(ssh.values())
    rows.append(["all segments", total_o, total_s, total_o + total_s])
    out += table(["shape", "orchestrator", "subagent", "all"], rows)
    out.append("")

    out.append("## Shell activity by family")
    out.append("")
    of, sf = Counter(), Counter()
    for s in orch:
        of.update(s.families)
    for s in subs:
        sf.update(s.families)
    rows = [[name, of[name], sf[name], of[name] + sf[name]] for name, _ in FAMILIES]
    out += table(["family (commands matching)", "orchestrator", "subagent", "all"], rows)
    out.append("")

    out.append("## Subagent spawns")
    out.append("")
    spawns = [(s.name[:8], sp) for s in orch for sp in s.spawns]
    by_model = Counter(sp["model"] for _, sp in spawns)
    by_type = Counter(sp["type"] for _, sp in spawns)
    out.append(
        f"{len(spawns)} Agent calls. By model: "
        + ", ".join(f"{k} {v}" for k, v in by_model.most_common())
        + ". By type: "
        + ", ".join(f"{k} {v}" for k, v in by_type.most_common())
        + "."
    )
    out.append("")
    rows = [
        [sid, sp["description"], sp["model"], sp["type"], sp["prompt_chars"]] for sid, sp in spawns
    ]
    out += table(["session", "description", "model", "type", "prompt chars"], rows)
    out.append("")

    out.append("## Permission denials")
    out.append("")
    den = Counter()
    for s in sessions:
        den.update(s.denials)
    rows = [[k, v] for k, v in sorted(den.items(), key=lambda kv: (-kv[1], kv[0]))]
    out += table(["kind", "count"], rows or [["none recorded", 0]])
    out.append("")
    out.append("Approved permission prompts leave no record in the transcript and are not counted.")
    out.append("")

    out.append("## Memory files")
    out.append("")
    out.append(f"{memory[0]} files, {memory[1]} words. Read at run time, not scoped by --before.")
    return out


def scrub(lines: list[str]) -> list[str]:
    """Replace any key-looking substring and clip every line to MAX_LINE characters."""
    out = []
    for line in lines:
        line = SECRET_RE.sub("<redacted>", line)
        if len(line) > MAX_LINE:
            line = line[: MAX_LINE - 3] + "..."
        out.append(line)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument(
        "--before", type=parse_ts, default=None, help="ISO 8601, e.g. 2026-09-06T20:00:00Z"
    )
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"no transcript directory at {args.root}", file=sys.stderr)
        return 1
    sessions = load(args.root, args.before)
    lines = render(sessions, memory_summary(args.root), args.top)
    print("\n".join(scrub(lines)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
