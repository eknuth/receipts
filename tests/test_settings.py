"""Settings has no required variable. No one command uses every key: the
generator needs the ingest key alone, the agent and the evals need the MCP key
and the key of whichever provider is in play. Each key is optional on the model
and the code that uses it raises a ValueError naming the variable at the point
of use (gen/emit.py, agent/mcp_client.py, agent/providers/anthropic.py,
agent/providers/nvidia.py). What the model still enforces is that a value, once
given, is not empty, and that an unknown name in .env is an error.
"""

import pytest
from pydantic import ValidationError

from receipts.settings import ENV_FILE, REPO_ROOT, Settings
from tests.conftest import ENV_EXAMPLE, env_example_names

KEYS = (
    "HONEYCOMB_INGEST_KEY",
    "HONEYCOMB_MCP_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_WORKSPACE_ID",
    "NVIDIA_API_KEY",
)


def test_settings_loads_with_fake_keys(settings: Settings) -> None:
    assert settings.honeycomb_env == "receipts-demo"
    assert settings.honeycomb_dataset == "receipts-shop"
    assert settings.anthropic_model == "claude-sonnet-4-5"


def test_env_file_is_anchored_to_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ENV_FILE == REPO_ROOT / ".env"
    assert (REPO_ROOT / "pyproject.toml").exists()


def test_secrets_do_not_appear_in_repr(settings: Settings) -> None:
    text = repr(settings) + str(settings) + settings.model_dump_json()
    assert "fake-ingest-key" not in text
    assert "fake-secret" not in text
    assert "fake-anthropic-key" not in text
    assert settings.honeycomb_ingest_key.get_secret_value() == "fake-ingest-key"


def test_shell_variables_do_not_leak_into_fixture(
    monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    monkeypatch.setenv("HONEYCOMB_ENV", "prod-oops")
    assert Settings(_env_file=None).honeycomb_env == "prod-oops"


def test_settings_loads_with_nothing_set(clean_env: None) -> None:
    """No .env, no shell variables: every key is None and every default holds.
    A stranger who runs gen.emit with only the ingest key set, or the evals on
    another provider with no Anthropic key, must not be stopped here."""
    settings = Settings(_env_file=None)
    for name in KEYS:
        assert getattr(settings, name.lower()) is None, name
    assert settings.honeycomb_env == "receipts-demo"
    assert settings.anthropic_model == "claude-sonnet-4-5"


def test_unfilled_env_example_loads_with_every_key_unset(clean_env: None) -> None:
    """The template ships `NAME=` for each key. With env_ignore_empty that
    reads as unset, not as an empty key, and no comment line leaks in as a value."""
    settings = Settings(_env_file=ENV_EXAMPLE)
    for name in KEYS:
        assert getattr(settings, name.lower()) is None, name
    assert settings.honeycomb_dataset == "receipts-shop"
    assert settings.honeycomb_auth == "key"


@pytest.mark.parametrize("name", KEYS)
def test_empty_key_value_is_rejected(clean_env: None, name: str) -> None:
    """Unset means None; an explicit empty string is still not a key."""
    with pytest.raises(ValidationError, match=name.lower()):
        Settings(_env_file=None, **{name.lower(): ""})


def test_unknown_variable_in_env_file_is_rejected(clean_env: None, tmp_path) -> None:
    """A typo in .env is an error, not a silently applied default."""
    env = tmp_path / ".env"
    env.write_text("HONEYCOMB_DATSET=oops\n")
    with pytest.raises(ValidationError, match="honeycomb_datset"):
        Settings(_env_file=env)


def test_an_empty_env_value_means_unset_not_a_validation_error(clean_env: None, tmp_path) -> None:
    """.env.example ships `HONEYCOMB_INGEST_KEY=` for "leave this unset". Without
    env_ignore_empty, the empty string would fail the field's own min_length=1."""
    env = tmp_path / ".env"
    env.write_text("HONEYCOMB_MCP_KEY=k\nHONEYCOMB_INGEST_KEY=\n")
    settings = Settings(_env_file=env)
    assert settings.honeycomb_ingest_key is None
    assert settings.honeycomb_mcp_key.get_secret_value() == "k"


def test_env_example_matches_settings_fields() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    assert set(env_example_names()) == fields


def test_ollama_defaults(settings: Settings) -> None:
    """R15: not required to be set, and blank .env.example values still resolve to these."""
    assert settings.ollama_host == "http://localhost:11434"
    assert settings.ollama_model == "qwen3.8:27b"


def test_nvidia_defaults(settings: Settings) -> None:
    """R15: not required to be set, and blank .env.example values still resolve to these."""
    assert settings.nvidia_api_key is None
    assert settings.nvidia_base_url == "https://integrate.api.nvidia.com/v1"
    assert settings.nvidia_model == "nvidia/nemotron-3-super-120b-a12b"
