"""Tests for the Claude Code hooks under `.claude/hooks/`.

Each hook is run as a subprocess with a PreToolUse or PostToolUse JSON payload
on stdin, the way Claude Code runs it, and the test asserts the exit code and
the stderr. A hook that matches nothing must exit 0 with no output. The
secrets hook gets a throwaway git repository in `tmp_path`; the emit hook is
pointed at a marker process the test starts itself; the lint hook gets a
Makefile whose `lint` target fails on purpose.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from receipts.settings import REPO_ROOT

HOOKS = REPO_ROOT / ".claude" / "hooks"
SECRETS = HOOKS / "block_secrets.py"
DOUBLE_EMIT = HOOKS / "block_double_emit.py"
PARKED = HOOKS / "block_parked_column.py"
LINT = HOOKS / "lint_after_commit.py"


def run_hook(
    script: Path,
    payload: dict | str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(script)],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=60,
    )


def bash(command: str, cwd: Path | None = None, *, event: str = "PreToolUse") -> dict:
    return {
        "hook_event_name": event,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd or REPO_ROOT),
    }


def assert_pass(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "" and proc.stderr == ""


# --------------------------------------------------------------------------
# Every hook: pass-through
# --------------------------------------------------------------------------


@pytest.mark.parametrize("script", [SECRETS, DOUBLE_EMIT, PARKED, LINT])
def test_other_tools_and_bad_input_pass_through(script: Path, tmp_path: Path) -> None:
    assert_pass(run_hook(script, {"tool_name": "Read", "tool_input": {"file_path": "/x"}}))
    assert_pass(run_hook(script, "{not json"))
    assert_pass(run_hook(script, bash("ls -la", tmp_path)))


# --------------------------------------------------------------------------
# block_secrets
# --------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with one clean commit and a `.env.example`."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / ".env.example").write_text("NVIDIA_API_KEY=\nHONEYCOMB_INGEST_KEY=\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    subprocess.run(["git", "add", name], cwd=repo, check=True)


def test_secrets_clean_commit_passes(repo: Path) -> None:
    stage(repo, "b.py", "y = 2\nheader = 'x-honeycomb-team: <ingest key>'\n")
    assert_pass(run_hook(SECRETS, bash("git commit -m 'clean'", repo)))


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("KEY = 'sk-ant-api03-abcdefghijklmnop'\n", "sk-ant- key"),
        ("k = 'hcaik_0123456789abcdef'\n", "hcaik_ ingest key"),
        ("k = 'hcamk_0123456789abcdef'\n", "hcamk_ management key"),
        ("k = 'nvapi-0123456789abcdef'\n", "nvapi- key"),
        ("k = 'AKIAIOSFODNN7EXAMPLE'\n", "AWS access key id"),
        ("headers = {'x-honeycomb-team: abc123def'}\n", "x-honeycomb-team header with a value"),
        ("NVIDIA_API_KEY=abc123\n", "NVIDIA_API_KEY= with a value not in .env.example"),
    ],
)
def test_secrets_commit_with_a_key_is_blocked(repo: Path, text: str, label: str) -> None:
    stage(repo, "leak.txt", text)
    proc = run_hook(SECRETS, bash("git commit -m 'oops'", repo))
    assert proc.returncode == 2
    assert "leak.txt: " + label in proc.stderr
    assert "abc123" not in proc.stderr and "sk-ant-api03" not in proc.stderr


def test_secrets_commit_through_git_dash_c_is_still_checked(repo: Path) -> None:
    stage(repo, "leak.txt", "k = 'nvapi-0123456789abcdef'\n")
    cmd = 'git -c user.name="Edwin Knuth" -c user.email=e@x commit -m "m"'
    assert run_hook(SECRETS, bash(cmd, repo)).returncode == 2


def test_secrets_env_example_line_passes(repo: Path) -> None:
    stage(repo, "notes.txt", "NVIDIA_API_KEY=\nNVIDIA_API_KEY=<your key>\n")
    assert_pass(run_hook(SECRETS, bash("git commit -m 'placeholders'", repo)))


def test_secrets_commit_all_reads_the_working_tree(repo: Path) -> None:
    (repo / "a.py").write_text("x = 'sk-ant-api03-abcdefghijklmnop'\n")
    assert_pass(run_hook(SECRETS, bash("git commit -m 'nothing staged'", repo)))
    assert run_hook(SECRETS, bash("git commit -am 'all'", repo)).returncode == 2


@pytest.mark.parametrize(
    "command",
    ["git add .env", "git add src/.env", "git commit .env -m x", "git add ~/.claude.json"],
)
def test_secrets_env_file_is_blocked(repo: Path, command: str) -> None:
    proc = run_hook(SECRETS, bash(command, repo))
    assert proc.returncode == 2
    assert "secrets file" in proc.stderr


def test_secrets_env_example_and_other_git_pass(repo: Path) -> None:
    assert_pass(run_hook(SECRETS, bash("git add .env.example", repo)))
    assert_pass(run_hook(SECRETS, bash("git status && git diff .env", repo)))


def test_secrets_outside_a_repo_passes(tmp_path: Path) -> None:
    assert_pass(run_hook(SECRETS, bash("git commit -m x", tmp_path)))


# --------------------------------------------------------------------------
# block_double_emit
# --------------------------------------------------------------------------

EMIT = "uv run python -m evals.run --scenarios trigger-checkout-latency --emit --repeats 1"


@pytest.fixture
def marker() -> str:
    """A sleeping process whose command line carries a unique marker, killed on exit."""
    token = f"receipts-hook-marker-{os.getpid()}"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", token])
    time.sleep(0.2)
    try:
        yield token
    finally:
        proc.kill()
        proc.wait()


def test_double_emit_blocked_while_a_marker_runs(marker: str) -> None:
    env = {"RECEIPTS_EMIT_PATTERN": marker}
    for command in (
        EMIT,
        "uv run python -m gen.emit --scenario control-quiet",
        "python gen/emit.py",
    ):
        proc = run_hook(DOUBLE_EMIT, bash(command), env=env)
        assert proc.returncode == 2, command
        assert "4,000 events per second" in proc.stderr
        assert "drops spans silently" in proc.stderr


def test_double_emit_passes_when_nothing_matches(marker: str) -> None:
    env = {"RECEIPTS_EMIT_PATTERN": marker + "-nothing-else"}
    assert_pass(run_hook(DOUBLE_EMIT, bash(EMIT), env=env))


def test_double_emit_ignores_a_run_without_emit(marker: str) -> None:
    env = {"RECEIPTS_EMIT_PATTERN": marker}
    assert_pass(run_hook(DOUBLE_EMIT, bash(EMIT.replace(" --emit", "")), env=env))
    assert_pass(run_hook(DOUBLE_EMIT, bash("uv run python -m evals.report"), env=env))


# --------------------------------------------------------------------------
# block_parked_column
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "mv evals/results/full evals/results/r18-pass",
        "mv evals/results/full/ evals/results/r18-pass/",
        "cp -r /Users/e/receipts/evals/results/full /Users/e/receipts/evals/results/pass2",
        "mv evals/results/full/control-quiet evals/results/parked/control-quiet",
        "cd /x && mkdir -p evals/results/r18 && mv evals/results/full evals/results/r18-tmp",
    ],
)
def test_parked_three_levels_is_blocked(command: str) -> None:
    proc = run_hook(PARKED, bash(command))
    assert proc.returncode == 2
    assert "three levels" in proc.stderr
    assert "evals/results/<park>/<config>" in proc.stderr


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p evals/results/r18-pass && mv evals/results/full evals/results/r18-pass/full",
        "mv evals/results/full evals/results/full-nvidia",
        "mv evals/results/full evals/results/no-negation",
        "mv evals/results/r18-pass/full evals/results/full",
        "mv evals/results/full/control-quiet/1 evals/results/full/control-quiet/4",
        "mv evals/results/full /tmp/elsewhere",
        "mv /tmp/x evals/results/park/full",
        "mv a b",
    ],
)
def test_parked_four_levels_and_renames_pass(command: str) -> None:
    assert_pass(run_hook(PARKED, bash(command)))


# --------------------------------------------------------------------------
# lint_after_commit
# --------------------------------------------------------------------------


def make_project(tmp_path: Path, name: str, lint_exit: int) -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "Makefile").write_text(f"lint:\n\t@echo lint output line\n\t@exit {lint_exit}\n")
    return project


def test_lint_after_commit_warns_when_red(tmp_path: Path) -> None:
    project = make_project(tmp_path, "red", 1)
    payload = bash("git commit -m x", project, event="PostToolUse")
    proc = run_hook(LINT, payload, env={"CLAUDE_PROJECT_DIR": str(project)})
    assert proc.returncode == 2
    assert "lint is red after this commit" in proc.stderr
    assert "lint output line" in proc.stderr


def test_lint_after_commit_silent_when_green(tmp_path: Path) -> None:
    project = make_project(tmp_path, "green", 0)
    payload = bash('git -c user.name="E" commit -m x', project, event="PostToolUse")
    assert_pass(run_hook(LINT, payload, env={"CLAUDE_PROJECT_DIR": str(project)}))


def test_lint_after_commit_ignores_other_commands_and_no_makefile(tmp_path: Path) -> None:
    project = make_project(tmp_path, "red", 1)
    env = {"CLAUDE_PROJECT_DIR": str(project)}
    assert_pass(run_hook(LINT, bash("git status", project, event="PostToolUse"), env=env))
    bare = tmp_path / "bare"
    bare.mkdir()
    payload = bash("git commit -m x", bare, event="PostToolUse")
    assert_pass(run_hook(LINT, payload, env={"CLAUDE_PROJECT_DIR": str(bare)}))


def test_settings_json_names_every_hook_and_no_write_permission() -> None:
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    commands = [
        hook["command"]
        for event in settings["hooks"].values()
        for entry in event
        for hook in entry["hooks"]
    ]
    for script in (SECRETS, DOUBLE_EMIT, PARKED, LINT):
        assert any(script.name in c for c in commands), script.name
        assert script.exists()
    for rule in settings["permissions"]["allow"]:
        assert "rm" not in rule.split("(")[-1].split()[:1]
        assert not rule.startswith(("Bash(git push", "Bash(gh pr create", "Bash(gh pr merge"))
        assert not rule.startswith("mcp__linear__save")
    assert (
        "deny" not in settings["permissions"]
        or "Bash(rm:*)" not in settings["permissions"]["allow"]
    )
