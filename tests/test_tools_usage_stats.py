"""Tests for tools/usage_stats.py: command normalization and the output contract.

The output contract is the point: the script reads transcripts that carry
conversation text, tool results, and possibly secrets, and may print only
counts, tool names, normalized command shapes, and Agent descriptions, with
no line over 120 characters, no key-looking substring, and no memory file
name. A synthetic transcript tree in `tmp_path` plants each of those and
checks they do not come out. Every planted key is built at runtime so no
source line matches the secrets hook's patterns.

When the real transcript directory exists on this machine the same contract
is checked against it, and the tables committed in `docs/agentic-workflow.md`
are regenerated with the `--before` timestamp the document names and compared
byte for byte, the way `evals/report.md` is pinned.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from receipts.settings import REPO_ROOT

SCRIPT = REPO_ROOT / "tools" / "usage_stats.py"
DOC = REPO_ROOT / "docs" / "agentic-workflow.md"
FORBIDDEN = ("sk-ant", "hcaik", "nvapi", "Bearer")
BEGIN, END = "<!-- usage_stats: begin -->", "<!-- usage_stats: end -->"

PLANTED_SK = "sk-ant-" + "p" * 12
PLANTED_HC = "hcaik_" + "p" * 12
PLANTED_NV = "nvapi-" + "p" * 12
PLANTED_PROMPT = "Authorization: Bearer " + PLANTED_NV


def _load():
    spec = importlib.util.spec_from_file_location("usage_stats", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["usage_stats"] = module
    spec.loader.exec_module(module)
    return module


stats = _load()


@pytest.mark.parametrize(
    ("command", "shapes"),
    [
        (
            "cd /Users/x/proj && uv run python -m evals.report",
            ["cd", "uv run python -m evals.report"],
        ),
        ('grep -n "a\\|b" f.py | head -5', ["grep", "head"]),
        ('git -c user.name="Edwin Knuth" -c user.email=x@y commit -m "m"', ["git commit"]),
        ("uv run pytest -q 2>&1 | tail -1", ["uv run pytest", "tail"]),
        ("python3 - <<'EOF'\nimport json\nprint(1)\nEOF", ["python3 -"]),
        ("uv run python -c 'import x\nprint(x)'", ["uv run python -c"]),
        ("for i in 1 2 3; do echo $i; done", ["for", "echo"]),
        ("gh pr view 28 --json state", ["gh pr view"]),
        ("/tmp/scratch/r19pass.sh", ["<path>"]),
        (
            "LOG=/tmp/x.log nohup uv run python -m evals.run --scenarios all",
            ["uv run python -m evals.run"],
        ),
        ("make lint && make test", ["make lint", "make test"]),
        ("uv run ruff format --check .", ["uv run ruff format"]),
        ("", []),
    ],
)
def test_command_shapes(command: str, shapes: list[str]) -> None:
    assert stats.command_shapes(command) == shapes


def test_families_match_the_pass_shape() -> None:
    cmd = "uv run python -m evals.run --scenarios trigger-checkout-latency --emit --repeats 1"
    assert stats.command_families(cmd) == ["eval pass with --emit"]
    assert stats.command_families("mv evals/results/full evals/results/park/full") == [
        "park or move a results column"
    ]
    assert "commit" in stats.command_families('git -c user.name="E K" commit -m x')


def test_parse_ts_accepts_z_and_offsets() -> None:
    z = stats.parse_ts("2026-09-06T10:00:00Z")
    offset = stats.parse_ts("2026-09-06T12:00:00+02:00")
    assert z == offset
    assert stats.parse_ts("not a time") is None
    assert stats.parse_ts(None) is None


def _line(**fields: object) -> str:
    return json.dumps(fields) + "\n"


def _assistant(*blocks: dict, ts: str = "2026-09-06T10:00:00Z") -> str:
    return _line(type="assistant", timestamp=ts, message={"content": list(blocks)})


def _user_text(text: str, ts: str = "2026-09-06T10:00:01Z") -> str:
    return _line(type="user", timestamp=ts, message={"content": text})


def _tool_result(text: str) -> str:
    return _line(
        type="user",
        timestamp="2026-09-06T10:00:02Z",
        message={"content": [{"type": "tool_result", "content": text}]},
    )


@pytest.fixture
def transcripts(tmp_path: Path) -> Path:
    """One orchestrator session, one subagent session, one empty session, and a late
    entry after the cutoff, with secrets planted in every slot."""
    root = tmp_path / "project"
    root.mkdir()
    session = root / "aaaaaaaa-0000.jsonl"
    session.write_text(
        _user_text("start the pass and give me the prompt for the next step")
        + _assistant(
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": f"export KEY={PLANTED_SK} && git status"},
            },
            {
                "type": "tool_use",
                "name": "Agent",
                "input": {
                    "description": f"Implement with {PLANTED_HC} in the label " + "x" * 200,
                    "model": "sonnet",
                    "subagent_type": "general-purpose",
                    "prompt": PLANTED_PROMPT,
                },
            },
        )
        + _tool_result("Permission for this action was denied by the classifier")
        + _line(
            type="user", toolDenialKind="automode-blocked", message={"content": "x"}, isMeta=True
        )
        + _user_text("[Request interrupted by user]")
        + _user_text("<task-notification>ignored</task-notification>")
        + _user_text("merge it")
        + _assistant(
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ts="2026-09-07T00:00:00Z",
        )
        + "{not json\n"
    )
    (root / "bbbbbbbb-0000.jsonl").write_text(_line(type="mode", sessionId="b"))
    sub = root / "aaaaaaaa-0000" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(
        _assistant(
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "uv run pytest -q | tail -1"},
            },
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x"}},
        )
    )
    memory = root / "memory"
    memory.mkdir()
    (memory / "private-person-note.md").write_text("one two three " + PLANTED_SK + "\n")
    return root


def test_synthetic_tree_counts(transcripts: Path) -> None:
    sessions = stats.load(transcripts)
    orch = [s for s in sessions if s.kind == "orchestrator"]
    subs = [s for s in sessions if s.kind == "subagent"]
    assert len(orch) == 1 and len(subs) == 1, "the empty session is dropped"
    s = orch[0]
    assert s.tools == {"Bash": 2, "Agent": 1}
    assert s.shapes == {"export": 1, "git status": 1, "ls": 1}
    assert s.user_turns == 2 and s.short_user_turns == 2 and s.interrupts == 1
    assert s.user_families["asks for a handoff prompt or to clear context"] == 1
    assert s.user_families["says merge"] == 1
    assert s.denials == {"automode-blocked": 1, "denial text in a tool result": 1}
    assert s.bad_lines == 1
    assert s.spawns[0]["model"] == "sonnet"
    assert s.spawns[0]["prompt_chars"] == len(PLANTED_PROMPT)
    assert subs[0].shapes == {"uv run pytest": 1, "tail": 1}


def test_before_drops_late_entries(transcripts: Path) -> None:
    before = stats.parse_ts("2026-09-06T23:59:59Z")
    s = next(s for s in stats.load(transcripts, before) if s.kind == "orchestrator")
    assert s.tools == {"Bash": 1, "Agent": 1}
    assert "ls" not in s.shapes
    early = stats.parse_ts("2026-09-01T00:00:00Z")
    assert stats.load(transcripts, early) == []


def _check_output(text: str) -> None:
    lines = text.splitlines()
    assert lines, "no output"
    for line in lines:
        assert len(line) <= 120, line
        for token in FORBIDDEN:
            assert token not in line, line


def test_cli_output_is_scrubbed(transcripts: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(transcripts)],
        capture_output=True,
        text=True,
        check=True,
    )
    _check_output(proc.stdout)
    assert "| Bash | 2 | 1 | 3 |" in proc.stdout
    assert "| automode-blocked | 1 |" in proc.stdout
    assert "<redacted>" in proc.stdout
    for planted in (PLANTED_SK, PLANTED_NV, PLANTED_HC):
        assert planted not in proc.stdout
    assert "1 files, 4 words" in proc.stdout
    assert "private-person-note" not in proc.stdout
    assert "bbbbbbbb" not in proc.stdout


def test_cli_before_flag(transcripts: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(transcripts),
            "--before",
            "2026-09-06T23:59:59Z",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "| Bash | 1 | 1 | 2 |" in proc.stdout


def test_cli_rejects_a_missing_root(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path / "missing")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert proc.stdout == ""


def _doc_command() -> list[str]:
    """The `usage_stats.py ...` command the document says it was generated with."""
    m = re.search(r"`uv run python tools/usage_stats\.py([^`]*)`", DOC.read_text())
    assert m, "docs/agentic-workflow.md must name its usage_stats.py command"
    return [sys.executable, str(SCRIPT), *m.group(1).split()]


def _doc_block() -> str:
    text = DOC.read_text()
    start, end = text.index(BEGIN) + len(BEGIN), text.index(END)
    return text[start:end].strip("\n")


def _without_memory(text: str) -> str:
    return text.split("## Memory files", 1)[0].rstrip("\n")


def test_doc_names_a_before_timestamp() -> None:
    command = _doc_command()
    assert "--before" in command
    assert stats.parse_ts(command[command.index("--before") + 1]) is not None


@pytest.mark.skipif(not stats.DEFAULT_ROOT.is_dir(), reason="no local transcripts")
def test_real_transcripts_keep_the_contract_and_pin_the_doc() -> None:
    proc = subprocess.run(_doc_command(), capture_output=True, text=True, check=True)
    _check_output(proc.stdout)
    assert _without_memory(proc.stdout) == _without_memory(_doc_block()), (
        "docs/agentic-workflow.md tables differ from the script's output for the timestamp "
        "the document names; regenerate them with the command in the document's header"
    )
