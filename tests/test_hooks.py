"""Tests for the Claude Code hooks under `.claude/hooks/`.

Each hook is run as a subprocess with a PreToolUse or PostToolUse JSON payload
on stdin, the way Claude Code runs it, and the test asserts the exit code and
the stderr. A hook that matches nothing must exit 0 with no output, and so
must a hook that hits anything unexpected: a bad cwd, a payload of the wrong
shape. The secrets hook gets a throwaway git repository in `tmp_path`; the emit
hook is pointed at a marker process the test starts itself; the lint hook gets
a Makefile whose `lint` target fails on purpose.

Every planted key is built at runtime (`"sk-ant-" + "a" * 20`) so that no line
of this file, or of any tracked file, matches the secrets hook's patterns. The
regression test for that stages the repository's own tracked files in a
scratch repository and asserts the hook lets the commit through.
"""

from __future__ import annotations

import json
import os
import shutil
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
ALL_HOOKS = [SECRETS, DOUBLE_EMIT, PARKED, LINT]

# Built at runtime so no source line matches the hook's patterns.
SK_ANT = "sk-ant-" + "a" * 20
HCAIK = "hcaik_" + "b" * 20
HCAMK = "hcamk_" + "c" * 20
NVAPI = "nvapi-" + "d" * 20
AKIA = "AKIA" + "A" * 16
HEADER = "x-honeycomb-team: " + "e" * 24
OAUTH_ACCESS_TOKEN = '"access_token": "' + "f" * 24 + '"'
OAUTH_REFRESH_TOKEN = '"refresh_token": "' + "g" * 24 + '"'


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


def bash(command: str, cwd: Path | str | None = None, *, event: str = "PreToolUse") -> dict:
    return {
        "hook_event_name": event,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd or REPO_ROOT),
    }


def assert_pass(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "" and proc.stderr == ""


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


# --------------------------------------------------------------------------
# Every hook: pass-through, bad input, bad cwd, the launcher
# --------------------------------------------------------------------------


@pytest.mark.parametrize("script", ALL_HOOKS)
def test_other_tools_and_bad_input_pass_through(script: Path, tmp_path: Path) -> None:
    assert_pass(run_hook(script, {"tool_name": "Read", "tool_input": {"file_path": "/x"}}))
    assert_pass(run_hook(script, "{not json"))
    assert_pass(run_hook(script, bash("ls -la", tmp_path)))


@pytest.mark.parametrize("script", ALL_HOOKS)
def test_bad_cwd_and_wrong_payload_shape_exit_zero(script: Path, tmp_path: Path) -> None:
    """A hook that cannot do its job steps aside; it never blocks by accident."""
    missing = tmp_path / "does-not-exist"
    env = {"CLAUDE_PROJECT_DIR": "", "RECEIPTS_EMIT_PATTERN": "receipts-nothing-runs-with-this"}
    for command in (
        "git commit -m x",
        "mkdir -p evals/results/p && mv evals/results/full evals/results/p/full",
        "python gen/emit.py",
    ):
        assert_pass(run_hook(script, bash(command, missing, event="PostToolUse"), env=env))
        assert_pass(run_hook(script, bash(command, missing), env=env))
    broken = {"tool_name": "Bash", "tool_input": "not a dict", "cwd": str(tmp_path)}
    assert_pass(run_hook(script, broken, env=env))
    broken = {"tool_name": "Bash", "tool_input": {"command": ["not", "a", "string"]}, "cwd": 7}
    assert_pass(run_hook(script, broken, env=env))


def hook_commands() -> list[str]:
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    return [
        hook["command"]
        for event in settings["hooks"].values()
        for entry in event
        for hook in entry["hooks"]
    ]


@pytest.mark.parametrize("command", hook_commands())
def test_launcher_survives_a_bad_cwd_and_an_empty_project_dir(command: str, tmp_path: Path) -> None:
    """The settings.json command line itself must exit 0 when the hook file cannot be
    found: an empty CLAUDE_PROJECT_DIR, or a session cwd outside the project. A non-zero
    exit from the launcher would block every Bash call."""
    assert "uv run" not in command, "hooks are stdlib only; uv resolving a project can fail"
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}})
    cases = [
        ({"CLAUDE_PROJECT_DIR": str(REPO_ROOT)}, REPO_ROOT),
        ({"CLAUDE_PROJECT_DIR": ""}, tmp_path),
        ({"CLAUDE_PROJECT_DIR": str(tmp_path)}, tmp_path),
    ]
    for env, cwd in cases:
        base = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        proc = subprocess.run(
            ["sh", "-c", command],
            input=payload,
            capture_output=True,
            text=True,
            cwd=cwd,
            env={**base, **env},
            timeout=60,
        )
        assert proc.returncode == 0, (command, env, proc.stderr)
        assert proc.stderr == ""


# --------------------------------------------------------------------------
# block_secrets
# --------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with one clean commit and a `.env.example`."""
    git("init", "-q", "-b", "main", cwd=tmp_path)
    git("config", "user.email", "t@example.com", cwd=tmp_path)
    git("config", "user.name", "T", cwd=tmp_path)
    (tmp_path / ".env.example").write_text("NVIDIA_API_KEY=\nHONEYCOMB_INGEST_KEY=\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", ".", cwd=tmp_path)
    git("commit", "-q", "-m", "init", cwd=tmp_path)
    return tmp_path


def stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    git("add", name, cwd=repo)


def test_secrets_clean_commit_passes(repo: Path) -> None:
    stage(repo, "b.py", "y = 2\nheader = 'x-honeycomb-team: <ingest key>'\n")
    assert_pass(run_hook(SECRETS, bash("git commit -m 'clean'", repo)))


def test_secrets_the_repository_itself_passes(tmp_path: Path) -> None:
    """Regression: the hook must not refuse the branch's own tracked files, which carry
    the hook's regex sources and the tests' planted fixtures."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for rel in filter(None, listed.stdout.split("\0")):
        src = REPO_ROOT / rel
        if src.is_file():
            dst = scratch / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
    git("init", "-q", "-b", "main", cwd=scratch)
    git("add", "-A", cwd=scratch)
    proc = run_hook(SECRETS, bash("git commit -m 'the whole tree'", scratch))
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""


@pytest.mark.parametrize(
    ("text", "label"),
    [
        (f"KEY = '{SK_ANT}'\n", "sk-ant- key"),
        (f"k = '{HCAIK}'\n", "hcaik_ ingest key"),
        (f"k = '{HCAMK}'\n", "hcamk_ management key"),
        (f"k = '{NVAPI}'\n", "nvapi- key"),
        (f"k = '{AKIA}'\n", "AWS access key id"),
        (f"headers = {{'{HEADER}'}}\n", "x-honeycomb-team header with a value"),
        ("NVIDIA_API_KEY=abc123\n", "NVIDIA_API_KEY= with a value not in .env.example"),
        ("{" + OAUTH_ACCESS_TOKEN + "}\n", "OAuth access_token in a committed file"),
        ("{" + OAUTH_REFRESH_TOKEN + "}\n", "OAuth refresh_token in a committed file"),
    ],
)
def test_secrets_commit_with_a_key_is_blocked(repo: Path, text: str, label: str) -> None:
    stage(repo, "leak.txt", text)
    proc = run_hook(SECRETS, bash("git commit -m 'oops'", repo))
    assert proc.returncode == 2
    assert "leak.txt: " + label in proc.stderr
    for value in (
        SK_ANT,
        HCAIK,
        HCAMK,
        NVAPI,
        AKIA,
        HEADER,
        "abc123",
        "f" * 24,
        "g" * 24,
    ):
        assert value not in proc.stderr


@pytest.mark.parametrize(
    "command",
    [
        "git add honeycomb_oauth.json",
        "git add .receipts/honeycomb_oauth.json",
        "git commit honeycomb_oauth.json -m x",
    ],
)
def test_secrets_oauth_token_file_is_blocked_by_name(repo: Path, command: str) -> None:
    """R12 (EDW-1334): agent/auth.py's OAuth token store defaults outside the
    repo, but this catches it if one is ever staged from inside it anyway."""
    proc = run_hook(SECRETS, bash(command, repo))
    assert proc.returncode == 2
    assert "secrets file" in proc.stderr


def test_secrets_commit_through_git_dash_c_is_still_checked(repo: Path) -> None:
    stage(repo, "leak.txt", f"k = '{NVAPI}'\n")
    cmd = 'git -c user.name="Edwin Knuth" -c user.email=e@x commit -m "m"'
    assert run_hook(SECRETS, bash(cmd, repo)).returncode == 2


def test_secrets_follows_cd_and_dash_capital_c(repo: Path, tmp_path: Path) -> None:
    """`cd <repo> && git commit` and `git -C <repo> commit` are the common shapes; the
    diff is read where the commit will run, not in the payload cwd."""
    stage(repo, "leak.txt", f"k = '{SK_ANT}'\n")
    elsewhere = tmp_path.parent / (tmp_path.name + "-elsewhere")
    elsewhere.mkdir(exist_ok=True)
    assert run_hook(SECRETS, bash(f"cd {repo} && git commit -m x", elsewhere)).returncode == 2
    assert run_hook(SECRETS, bash(f"git -C {repo} commit -m x", elsewhere)).returncode == 2
    assert (
        run_hook(SECRETS, bash(f"cd {repo}; git status; git commit -m x", elsewhere)).returncode
        == 2
    )
    assert_pass(run_hook(SECRETS, bash("git commit -m x", elsewhere)))


def test_secrets_env_example_line_passes(repo: Path) -> None:
    stage(repo, "notes.txt", "NVIDIA_API_KEY=\nNVIDIA_API_KEY=<your key>\n")
    assert_pass(run_hook(SECRETS, bash("git commit -m 'placeholders'", repo)))


def test_secrets_commit_all_reads_the_working_tree(repo: Path) -> None:
    (repo / "a.py").write_text(f"x = '{SK_ANT}'\n")
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


def test_secrets_heredoc_body_is_not_shell(repo: Path) -> None:
    command = "cat > notes.md <<'EOF'\nrun git add .env never\nEOF"
    assert_pass(run_hook(SECRETS, bash(command, repo)))


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
        "nohup zsh /tmp/scratch/r20pass.sh > /tmp/scratch/r20pass.log 2>&1 &",
        "/tmp/scratch/afterpass2.sh",
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
    assert_pass(run_hook(DOUBLE_EMIT, bash("ls scripts/*.sh"), env=env))


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "add afterpass.sh to the skill"',
        'grep -n "pass.sh" .claude/skills/pass-run/SKILL.md',
        "cat /private/tmp/x/scratchpad/r19pass.sh",
        "ls tests/test_pass.sh",
        'echo "uv run python -m evals.run --emit"',
        "python3 - <<EOF\nprint('uv run python -m evals.run --emit')\nEOF",
        "bash bypass.sh",
        "head -3 nvidiapass.sh | grep emit",
    ],
)
def test_double_emit_passes_ordinary_commands_while_a_marker_runs(
    command: str, marker: str
) -> None:
    """A pass runs for ninety minutes; the hook must not refuse every command that
    mentions a pass script or an emit in a string, a filename, or a heredoc body."""
    assert_pass(run_hook(DOUBLE_EMIT, bash(command), env={"RECEIPTS_EMIT_PATTERN": marker}))


@pytest.mark.parametrize(
    "command",
    [
        "zsh /tmp/scratch/r20pass.sh",
        "sh ./afterpass.sh > log 2>&1 &",
        "LOG=/tmp/x.log nohup time env zsh /tmp/scratch/pass1.sh &",
        "cd /repo && uv run python -m evals.run --scenarios all --repeats 1 --emit",
    ],
)
def test_double_emit_blocks_launch_shapes_while_a_marker_runs(command: str, marker: str) -> None:
    proc = run_hook(DOUBLE_EMIT, bash(command), env={"RECEIPTS_EMIT_PATTERN": marker})
    assert proc.returncode == 2, command


# --------------------------------------------------------------------------
# block_parked_column
# --------------------------------------------------------------------------

R = "evals/results"


@pytest.mark.parametrize(
    "command",
    [
        f"mv {R}/full {R}/r18-pass",
        f"cp -r /Users/e/receipts/{R}/full /Users/e/receipts/{R}/pass2",
        f"mv {R}/full/control-quiet {R}/parked/control-quiet",
        f"mkdir -p {R}/r18 && mv {R}/full {R}/r18-tmp",
        f"cd /x && mv {R}/full {R}/r18-tmp && mv {R}/r18-tmp {R}/r18-b",
        # A config-shaped name the runner never writes: the report merges these cells into
        # the live `full` column by the config field inside grade.json.
        f"mv {R}/full {R}/full-old",
        f"mv {R}/full {R}/full-2",
        # A trailing slash on a directory that does not exist is a rename, not a move into.
        f"mv {R}/full {R}/r20-pass/",
        f"mv {R}/full/ {R}/r18-pass/",
    ],
)
def test_parked_three_levels_is_blocked(command: str, tmp_path: Path) -> None:
    proc = run_hook(PARKED, bash(command, tmp_path))
    assert proc.returncode == 2
    assert "three levels" in proc.stderr
    assert "evals/results/<park>/<config>" in proc.stderr


@pytest.mark.parametrize(
    "command",
    [
        # The exact r19pass.sh sequence: through a temporary name to four levels.
        f"mv {R}/full {R}/r18-pass-tmp && mkdir -p {R}/r18-pass && "
        f"mv {R}/r18-pass-tmp {R}/r18-pass/full",
        f"mkdir -p {R}/r18-pass && mv {R}/full {R}/r18-pass/",
        f"mkdir -p {R}/r18-pass && mv {R}/full {R}/r18-pass",
        f"mkdir -p {R}/r18-pass && cp -r {R}/full {R}/r18-pass/",
        f"mkdir -p {R}/r18-pass && mv {R}/full/ {R}/r18-pass/",
        f"mkdir -p {R}/r18-pass/full && mv {R}/full/control-quiet {R}/r18-pass/full/",
        f"mv {R}/full/control-quiet {R}/r18-pass/full/control-quiet",
        f"mkdir -p {R}/r18-pass && mv {R}/full {R}/r18-pass/full",
        f"mv {R}/full {R}/full-nvidia",
        f"mv {R}/full {R}/no-negation",
        f"mv {R}/r18-pass/full {R}/full",
        f"mv {R}/full/control-quiet/1 {R}/full/control-quiet/4",
        f"mv {R}/full /tmp/elsewhere",
        f"mv /tmp/x {R}/park/full",
        "mv a b",
    ],
)
def test_parked_four_levels_and_renames_pass(command: str, tmp_path: Path) -> None:
    assert_pass(run_hook(PARKED, bash(command, tmp_path)))


def test_parked_destination_that_exists_on_disk_means_into(tmp_path: Path) -> None:
    (tmp_path / R / "r18-pass").mkdir(parents=True)
    assert_pass(run_hook(PARKED, bash(f"mv {R}/full {R}/r18-pass", tmp_path)))
    other = tmp_path / "other"
    other.mkdir()
    assert run_hook(PARKED, bash(f"mv {R}/full {R}/r18-pass", other)).returncode == 2
    assert_pass(run_hook(PARKED, bash(f"cd {tmp_path} && mv {R}/full {R}/r18-pass", other)))
    # A trailing slash means "into" only for a directory that exists or was just made.
    assert_pass(run_hook(PARKED, bash(f"mv {R}/full {R}/r18-pass/", tmp_path)))
    assert run_hook(PARKED, bash(f"mv {R}/full {R}/r20-pass/", tmp_path)).returncode == 2
    assert_pass(
        run_hook(PARKED, bash(f"mkdir {R}/r20-pass && mv {R}/full {R}/r20-pass/", tmp_path))
    )


def test_parked_hook_allows_exactly_the_names_the_runner_writes() -> None:
    """The hook is stdlib only and mirrors the runner's lists; this keeps them equal."""
    import importlib.util

    from evals.run import CONFIGS, PROVIDERS

    spec = importlib.util.spec_from_file_location("block_parked_column", PARKED)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.CONFIGS == tuple(CONFIGS)
    assert module.PROVIDERS == tuple(PROVIDERS)
    assert module.live_name("full") and module.live_name("no-negation-ollama")
    assert not module.live_name("full-old") and not module.live_name("r18-pass")


# --------------------------------------------------------------------------
# lint_after_commit
# --------------------------------------------------------------------------


def make_project(tmp_path: Path, name: str, lint_exit: int, *, git_repo: bool = False) -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "Makefile").write_text(f"lint:\n\t@echo lint output line\n\t@exit {lint_exit}\n")
    if git_repo:
        git("init", "-q", cwd=project)
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


def test_lint_after_commit_prefers_the_cwd_repository(tmp_path: Path) -> None:
    """In a worktree session the commit went to the cwd's repository, so that is what
    gets linted; CLAUDE_PROJECT_DIR (main's checkout) is the fallback outside a repo."""
    red = make_project(tmp_path, "red", 1, git_repo=True)
    green = make_project(tmp_path, "green", 0)
    sub = red / "sub"
    sub.mkdir()
    payload = bash("git commit -m x", sub, event="PostToolUse")
    proc = run_hook(LINT, payload, env={"CLAUDE_PROJECT_DIR": str(green)})
    assert proc.returncode == 2
    bare = tmp_path / "bare"
    bare.mkdir()
    payload = bash("git commit -m x", bare, event="PostToolUse")
    assert_pass(run_hook(LINT, payload, env={"CLAUDE_PROJECT_DIR": str(green)}))
    proc = run_hook(LINT, payload, env={"CLAUDE_PROJECT_DIR": str(red)})
    assert proc.returncode == 2


def test_lint_after_commit_ignores_other_commands_and_no_makefile(tmp_path: Path) -> None:
    project = make_project(tmp_path, "red", 1)
    env = {"CLAUDE_PROJECT_DIR": str(project)}
    assert_pass(run_hook(LINT, bash("git status", project, event="PostToolUse"), env=env))
    bare = tmp_path / "bare"
    bare.mkdir()
    payload = bash("git commit -m x", bare, event="PostToolUse")
    assert_pass(run_hook(LINT, payload, env={"CLAUDE_PROJECT_DIR": str(bare)}))


# --------------------------------------------------------------------------
# settings.json
# --------------------------------------------------------------------------


def test_settings_json_names_every_hook_and_only_read_only_permissions() -> None:
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    commands = hook_commands()
    for script in ALL_HOOKS:
        assert any(script.name in c for c in commands), script.name
        assert script.exists()
    allow = settings["permissions"]["allow"]
    for rule in allow:
        assert not rule.startswith(
            ("Bash(rm", "Bash(git push", "Bash(gh pr create", "Bash(gh pr merge")
        )
        assert not rule.startswith("mcp__linear__save")
    assert "Bash(git branch:*)" not in allow
    assert "Bash(uv run ruff:*)" not in allow
    assert "Bash(uv run ruff check:*)" in allow
    assert "Bash(uv run ruff format --check:*)" in allow
