import re

import pytest

from receipts.settings import REPO_ROOT, Settings

ENV_EXAMPLE = REPO_ROOT / ".env.example"


def env_example_names() -> list[str]:
    """Every variable name declared in .env.example."""
    names = []
    for line in ENV_EXAMPLE.read_text().splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            names.append(m.group(1))
    return names


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every project variable from the process environment.

    _env_file=None alone only blocks the dotenv file; an exported shell
    variable would still leak into a test-built Settings.
    """
    for name in env_example_names():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(clean_env: None) -> Settings:
    """A Settings instance populated with fake, non-secret values.

    Ignores the real .env and the shell so tests never depend on local secrets.
    """
    return Settings(
        _env_file=None,
        honeycomb_ingest_key="fake-ingest-key",
        honeycomb_mcp_key="fake-key-id:fake-secret",
        anthropic_api_key="fake-anthropic-key",
        anthropic_workspace_id="fake-workspace-id",
    )


@pytest.fixture(scope="module")
def settings_module() -> Settings:
    """The same fake settings as `settings`, at module scope.

    The generator tests build a few thousand spans per module and want the
    settings once rather than once per test.
    """
    return Settings(
        _env_file=None,
        honeycomb_ingest_key="fake-ingest-key",
        honeycomb_mcp_key="fake-key-id:fake-secret",
        anthropic_api_key="fake-anthropic-key",
        anthropic_workspace_id="fake-workspace-id",
    )
