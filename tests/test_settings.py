import pytest
from pydantic import ValidationError

from receipts.settings import Settings

REQUIRED_VARS = (
    "HONEYCOMB_INGEST_KEY",
    "HONEYCOMB_MCP_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_WORKSPACE_ID",
)


def test_settings_loads_with_fake_keys(settings: Settings) -> None:
    assert settings.honeycomb_env == "receipts-demo"
    assert settings.honeycomb_dataset == "receipts-shop"
    assert settings.anthropic_model == "claude-sonnet-4-5"


def test_settings_raises_clear_error_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED_VARS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    message = str(exc_info.value)
    for name in REQUIRED_VARS:
        assert name in message
