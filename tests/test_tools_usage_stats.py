"""Tests for tools/usage_stats.py: command normalization and the output contract.

The output contract is the point: the script reads transcripts that carry
conversation text, tool results, and possibly secrets, and may print only
counts, tool names, normalized command shapes, and Agent descriptions, with
no line over 120 characters and no key-looking substring. A synthetic
transcript tree in `tmp_path` plants each of those and checks they do not
come out. When the real transcript directory exists on this machine the same
contract is checked against it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from receipts.settings import REPO_ROOT

SCRIPT = REPO_ROOT / "tools" / "usage_stats.py"
FORBIDDEN = ("sk-ant", "hcaik", "nvapi", "Bearer")


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


def _line(**fields: object) -> str:
    return json.dumps(fields) + "\n"


def _assistant(*blocks: dict) -> str:
    return _line(
        type="assistant", timestamp="2026-09-06T10:00:00Z", message={"content": list(blocks)}
    )


def _user_text(text: str) -> str:
    return _line(type="user", timestamp="2026-09-06T10:00:01Z", message={"content": text})


def _tool_result(text: str) -> str:
    return _line(
        type="user",
        timestamp="2026-09-06T10:00:02Z",
        message={"content": [{"type": "tool_result", "content": text}]},
    )


@pytest.fixture
def transcripts(tmp_path: Path) -> Path:
    """One orchestrator session and one subagent session, with secrets planted in every slot."""
    root = tmp_path / "project"
    root.mkdir()
    session = root / "aaaaaaaa-0000.jsonl"
    session.write_text(
        _user_text("start the pass and give me the prompt for the next step")
        + _assistant(
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "export KEY=sk-ant-planted && git status"},
            },
            {
                "type": "tool_use",
                "name": "Agent",
                "input": {
                    "description": "Implement with hcaik_planted in the label " + "x" * 200,
                    "model": "sonnet",
                    "subagent_type": "general-purpose",
                    "prompt": "Authorization: Bearer nvapi-planted",
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
        + "{not json\n"
    )
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
    (memory / "note.md").write_text("one two three sk-ant-not-a-key\n")
    return root


def test_synthetic_tree_counts(transcripts: Path) -> None:
    sessions = stats.load(transcripts)
    orch = [s for s in sessions if s.kind == "orchestrator"]
    subs = [s for s in sessions if s.kind == "subagent"]
    assert len(orch) == 1 and len(subs) == 1
    s = orch[0]
    assert s.tools == {"Bash": 1, "Agent": 1}
    assert s.shapes == {"export": 1, "git status": 1}
    assert s.user_turns == 2 and s.short_user_turns == 2 and s.interrupts == 1
    assert s.user_families["asks for a handoff prompt or to clear context"] == 1
    assert s.user_families["says merge"] == 1
    assert s.denials == {"automode-blocked": 1, "denial text in a tool result": 1}
    assert s.bad_lines == 1
    assert s.spawns[0]["model"] == "sonnet" and s.spawns[0]["prompt_chars"] == 35
    assert subs[0].shapes == {"uv run pytest": 1, "tail": 1}


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
    assert "| Bash | 1 | 1 | 2 |" in proc.stdout
    assert "| automode-blocked | 1 |" in proc.stdout
    assert "<redacted>" in proc.stdout
    assert "planted" not in proc.stdout.replace("<redacted>-planted", "").replace(
        "<redacted>_planted", ""
    )
    assert "| note.md | 4 |" in proc.stdout


def test_cli_rejects_a_missing_root(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path / "missing")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert proc.stdout == ""


@pytest.mark.skipif(not stats.DEFAULT_ROOT.is_dir(), reason="no local transcripts")
def test_real_transcripts_keep_the_contract() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        check=True,
    )
    _check_output(proc.stdout)
