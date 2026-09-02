import pytest

from receipts.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """A Settings instance populated with fake, non-secret values.

    Ignores any real .env so tests do not depend on local secrets.
    """
    return Settings(
        _env_file=None,
        HONEYCOMB_INGEST_KEY="fake-ingest-key",
        HONEYCOMB_MCP_KEY="fake-key-id:fake-secret",
        ANTHROPIC_API_KEY="fake-anthropic-key",
        ANTHROPIC_WORKSPACE_ID="fake-workspace-id",
    )
