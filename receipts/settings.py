"""Project settings, loaded from the .env file at the repo root.

The .env path is anchored to this file, not the working directory, so the
generator, the agent, and the eval runner all find the same file no matter
where they are launched from.

Settings is a plain pydantic-settings model. It is not instantiated at import
time, so importing this module never fails on a missing .env; construction
does, with pydantic's own error naming each missing or empty required variable.
The three API keys are SecretStr so a stray repr, log line, or span attribute
does not carry them. Call .get_secret_value() at the point of use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    """All configuration the project reads from the environment."""

    # Field names match the .env names case-insensitively, so no aliases are
    # needed. extra is left at pydantic's default (forbid) so a misspelled name
    # in .env is an error instead of a silently applied default. env_ignore_empty
    # means an env var set to the empty string is treated as unset, which is
    # what lets .env.example ship `HONEYCOMB_INGEST_KEY=` and have that mean
    # "no key" rather than fail the `min_length=1` on the optional field.
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", env_ignore_empty=True
    )

    # Honeycomb: environment "receipts-demo", dataset "receipts-shop".
    # honeycomb_ingest_key is optional: R9 self-telemetry runs without it (no
    # spans, no warnings), and gen/emit.py, which needs a real key to send
    # anything, checks for one itself and fails with a clear message rather
    # than posting with an empty key.
    honeycomb_ingest_key: SecretStr | None = Field(default=None, min_length=1)
    honeycomb_mcp_key: SecretStr = Field(min_length=1)
    honeycomb_mcp_url: str = "https://mcp.honeycomb.io/mcp"
    honeycomb_otlp_endpoint: str = "https://api.honeycomb.io"
    honeycomb_dataset: str = "receipts-shop"
    honeycomb_env: str = "receipts-demo"

    # R12 (EDW-1334): canvas_agent_invoke needs a user actor. A management
    # key has none and fails with "actor_user_hcid is required" (verified
    # live 2026-09-07), so Canvas needs an OAuth session; every read tool and
    # the whole eval matrix keep working under the key, which is why key
    # stays the default and oauth is opt-in rather than a replacement.
    # agent/mcp_client.py's _open_streams reads this to pick which auth
    # agent/auth.py builds. honeycomb_oauth_token_path is where that
    # module's login command stores the access token, refresh token, and
    # registered client info; it must never be under the repo (the same
    # reasoning as honeycomb_mcp_key living only in .env), so the default is
    # outside it and the file is created 0600.
    honeycomb_auth: Literal["key", "oauth"] = "key"
    honeycomb_oauth_token_path: Path = Path.home() / ".receipts" / "honeycomb_oauth.json"

    # Anthropic. The key is identity-linked, so anthropic_workspace_id must be
    # sent as the anthropic-workspace-id header on every request.
    anthropic_api_key: SecretStr = Field(min_length=1)
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_workspace_id: str = Field(min_length=1)

    # AWS Bedrock, R11. Not required until then.
    aws_profile: str | None = None
    aws_region: str = "us-west-2"
    bedrock_model_id: str | None = None

    # Ollama, R15. Not required until then.
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen3.8:27b"

    # NVIDIA hosted NIM, R15. Not required until --provider nvidia is used;
    # agent/providers/nvidia.py raises a clear ValueError at construction
    # when the key is unset. nvidia/nemotron-3-super-120b-a12b is the
    # default: moonshotai/kimi-k3 was tried first and, on this key, returns
    # 429 after the first request and stays throttled for over five
    # minutes; nemotron answered eight back-to-back 22k-token requests with
    # 200 in about a second each.
    nvidia_api_key: SecretStr | None = Field(default=None, min_length=1)
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "nvidia/nemotron-3-super-120b-a12b"

    # R9 self-telemetry. Off by default: prompts and completions are only
    # written to the gen_ai.input.messages / gen_ai.output.messages span
    # events on the agent's own chat spans when this is set, because those
    # events carry the full conversation and are opt-in for a reason.
    receipts_capture_content: bool = False
