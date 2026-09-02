"""Project settings, loaded from the .env file at the repo root.

Settings is a plain pydantic-settings model. It is not instantiated at import
time, so importing this module never fails on a missing .env; construction
does, with pydantic's own error naming each missing required variable.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration the project reads from the environment."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Honeycomb: environment "receipts-demo", dataset "receipts-shop".
    honeycomb_ingest_key: str = Field(alias="HONEYCOMB_INGEST_KEY")
    honeycomb_mcp_key: str = Field(alias="HONEYCOMB_MCP_KEY")
    honeycomb_mcp_url: str = Field("https://mcp.honeycomb.io/mcp", alias="HONEYCOMB_MCP_URL")
    honeycomb_otlp_endpoint: str = Field(
        "https://api.honeycomb.io", alias="HONEYCOMB_OTLP_ENDPOINT"
    )
    honeycomb_dataset: str = Field("receipts-shop", alias="HONEYCOMB_DATASET")
    honeycomb_env: str = Field("receipts-demo", alias="HONEYCOMB_ENV")

    # Anthropic. The key is identity-linked, so anthropic_workspace_id must be
    # sent as the anthropic-workspace-id header on every request.
    anthropic_api_key: str = Field(alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field("claude-sonnet-4-5", alias="ANTHROPIC_MODEL")
    anthropic_workspace_id: str = Field(alias="ANTHROPIC_WORKSPACE_ID")

    # AWS Bedrock, R11. Not required until then.
    aws_profile: str | None = Field(None, alias="AWS_PROFILE")
    aws_region: str = Field("us-west-2", alias="AWS_REGION")
    bedrock_model_id: str | None = Field(None, alias="BEDROCK_MODEL_ID")

    # Ollama, R15. Not required until then.
    ollama_host: str = Field("http://localhost:11434", alias="OLLAMA_HOST")
    ollama_model: str = Field("qwen3.8:27b", alias="OLLAMA_MODEL")
