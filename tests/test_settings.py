import pytest
from pydantic import ValidationError

from receipts.settings import ENV_FILE, REPO_ROOT, Settings
from tests.conftest import ENV_EXAMPLE, env_example_names

REQUIRED_VARS = (
    "HONEYCOMB_MCP_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_WORKSPACE_ID",
)
FAKE_REQUIRED = {name.lower(): "k" for name in REQUIRED_VARS}


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
    assert Settings(_env_file=None, **FAKE_REQUIRED).honeycomb_env == "prod-oops"


def test_settings_raises_clear_error_when_env_absent(clean_env: None) -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    message = str(exc_info.value)
    for name in REQUIRED_VARS:
        assert name.lower() in message


def test_unfilled_env_example_is_rejected(clean_env: None) -> None:
    """An unfilled template must fail, not load with empty or comment values."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=ENV_EXAMPLE)

    message = str(exc_info.value)
    for name in REQUIRED_VARS:
        assert name.lower() in message


@pytest.mark.parametrize("name", REQUIRED_VARS)
def test_empty_required_value_is_rejected(clean_env: None, name: str) -> None:
    values = dict(FAKE_REQUIRED)
    values[name.lower()] = ""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_unknown_variable_in_env_file_is_rejected(clean_env: None, tmp_path) -> None:
    """A typo in .env is an error, not a silently applied default."""
    env = tmp_path / ".env"
    env.write_text("".join(f"{k}=k\n" for k in REQUIRED_VARS) + "HONEYCOMB_DATSET=oops\n")
    with pytest.raises(ValidationError, match="honeycomb_datset"):
        Settings(_env_file=env)


def test_ingest_key_is_optional(clean_env: None) -> None:
    """R9 self-telemetry and the agent both run without it; only gen/emit.py needs one."""
    settings = Settings(_env_file=None, **FAKE_REQUIRED)
    assert settings.honeycomb_ingest_key is None


def test_an_empty_env_value_means_unset_not_a_validation_error(clean_env: None, tmp_path) -> None:
    """.env.example ships `HONEYCOMB_INGEST_KEY=` for "leave this unset". Without
    env_ignore_empty, the empty string would fail the field's own min_length=1."""
    env = tmp_path / ".env"
    env.write_text("".join(f"{k}=k\n" for k in REQUIRED_VARS) + "HONEYCOMB_INGEST_KEY=\n")
    settings = Settings(_env_file=env)
    assert settings.honeycomb_ingest_key is None


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
