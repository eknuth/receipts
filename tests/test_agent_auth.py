"""Tests for agent/auth.py: token storage, and the no-browser guard that
`agent/mcp_client.py`'s OAuth path relies on.

No network calls and no browser: `login`'s own flow is exercised only up to
`build_provider`, which is a pure constructor. File permissions are checked
with `os.stat`, not by trusting `FileTokenStorage`'s own claim.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from agent.auth import (
    FileTokenStorage,
    OAuthNotAuthorized,
    build_provider,
    require_oauth_provider,
    status_line,
)
from receipts.settings import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        honeycomb_ingest_key="fake-ingest-key",
        honeycomb_mcp_key="fake-key-id:fake-secret",
        anthropic_api_key="fake-anthropic-key",
        anthropic_workspace_id="fake-workspace-id",
        honeycomb_auth="oauth",
        honeycomb_oauth_token_path=tmp_path / "honeycomb_oauth.json",
    )


# --------------------------------------------------------------------------
# FileTokenStorage
# --------------------------------------------------------------------------


async def test_tokens_and_client_info_round_trip_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "honeycomb_oauth.json"
    storage = FileTokenStorage(path)
    token = OAuthToken(access_token="at-1", refresh_token="rt-1", expires_in=3600)
    client = OAuthClientInformationFull(client_id="cid-1")

    await storage.set_tokens(token)
    await storage.set_client_info(client)

    # A fresh instance over the same path, not the one that wrote it: the
    # round trip goes through the file, not through in-memory state.
    fresh = FileTokenStorage(path)
    got_token = await fresh.get_tokens()
    got_client = await fresh.get_client_info()
    assert got_token is not None
    assert got_token.access_token == "at-1"
    assert got_token.refresh_token == "rt-1"
    assert got_client is not None
    assert got_client.client_id == "cid-1"


async def test_get_tokens_and_get_client_info_are_none_before_anything_is_stored(
    tmp_path: Path,
) -> None:
    storage = FileTokenStorage(tmp_path / "honeycomb_oauth.json")
    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None


async def test_the_token_file_is_created_0600(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "honeycomb_oauth.json"
    storage = FileTokenStorage(path)
    await storage.set_tokens(OAuthToken(access_token="at-1", expires_in=60))

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


async def test_the_token_file_is_never_briefly_wider_than_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier version wrote with `Path.write_text` (the mode `open()`
    gives a new file by default, subject to the process umask) and chmod'd
    to 0600 only afterward, leaving a window where the file held an access
    token at 0644 under a umask of 022. Spies on `os.chmod` to catch the
    file's mode at the instant chmod runs: it must already be 0600, meaning
    the file was created at that mode and never widened at all, not
    widened and then narrowed back."""
    path = tmp_path / "honeycomb_oauth.json"
    storage = FileTokenStorage(path)
    modes_when_chmod_ran: list[int] = []
    real_chmod = os.chmod

    def spying_chmod(target: Any, mode: int) -> None:
        modes_when_chmod_ran.append(stat.S_IMODE(os.stat(target).st_mode))
        real_chmod(target, mode)

    monkeypatch.setattr(os, "chmod", spying_chmod)
    old_umask = os.umask(0o022)
    try:
        await storage.set_tokens(OAuthToken(access_token="at-1", expires_in=60))
    finally:
        os.umask(old_umask)

    assert modes_when_chmod_ran == [0o600]


async def test_set_tokens_records_an_absolute_expires_at(tmp_path: Path) -> None:
    storage = FileTokenStorage(tmp_path / "honeycomb_oauth.json")
    before = time.time()
    await storage.set_tokens(OAuthToken(access_token="at-1", expires_in=100))
    after = time.time()

    status = storage.read_status()
    assert status.expires_at is not None
    assert before + 100 <= status.expires_at <= after + 100


def test_read_status_with_nothing_stored_has_no_tokens() -> None:
    status = FileTokenStorage(Path("/nonexistent/does-not-exist.json")).read_status()
    assert status.tokens is None
    assert status.expires_at is None
    assert status.client is None


def test_a_malformed_tokens_block_reads_as_no_token_on_file(tmp_path: Path) -> None:
    """A hand-edited or corrupted token file whose `tokens` block fails
    pydantic validation (an `access_token` that is a number, a `token_type`
    that is a list) used to raise `ValidationError` straight out of
    `read_status`, and from there out of `require_oauth_provider` and
    `evals.run.main`: exit 1, a stack trace, no login command named. It must
    read the same as no token on file at all."""
    path = tmp_path / "honeycomb_oauth.json"
    path.write_text(json.dumps({"tokens": {"access_token": 5, "token_type": []}}))

    status = FileTokenStorage(path).read_status()

    assert status.tokens is None


async def test_get_tokens_on_a_malformed_tokens_block_returns_none_not_an_exception(
    tmp_path: Path,
) -> None:
    path = tmp_path / "honeycomb_oauth.json"
    path.write_text(json.dumps({"tokens": {"access_token": 5, "token_type": []}}))

    assert await FileTokenStorage(path).get_tokens() is None


async def test_require_oauth_provider_names_login_on_a_malformed_tokens_block(
    tmp_path: Path,
) -> None:
    """The same clear, actionable message a missing token file gets, not a
    `pydantic.ValidationError` escaping to the caller."""
    settings = make_settings(tmp_path)
    settings.honeycomb_oauth_token_path.write_text(
        json.dumps({"tokens": {"access_token": 5, "token_type": []}})
    )

    with pytest.raises(OAuthNotAuthorized) as excinfo:
        await require_oauth_provider(settings)
    assert "agent.auth login" in str(excinfo.value)


# --------------------------------------------------------------------------
# require_oauth_provider: the no-browser guard
# --------------------------------------------------------------------------


async def test_require_oauth_provider_raises_and_names_login_when_no_token_is_on_file(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(OAuthNotAuthorized) as excinfo:
        await require_oauth_provider(settings)
    assert "agent.auth login" in str(excinfo.value)


async def test_require_oauth_provider_raises_when_the_token_expired_with_no_refresh_token(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    storage = FileTokenStorage(settings.honeycomb_oauth_token_path)
    await storage.set_tokens(OAuthToken(access_token="at-1", expires_in=-100))  # already expired

    with pytest.raises(OAuthNotAuthorized) as excinfo:
        await require_oauth_provider(settings)
    assert "agent.auth login" in str(excinfo.value)


async def test_require_oauth_provider_reuses_a_valid_stored_token_without_a_redirect_handler(
    tmp_path: Path,
) -> None:
    """A missing or unrefreshable token should ever need the browser again;
    a good token must be reused silently. The default provider passes no
    redirect_handler at all, so it is structurally unable to open one."""
    settings = make_settings(tmp_path)
    storage = FileTokenStorage(settings.honeycomb_oauth_token_path)
    await storage.set_tokens(OAuthToken(access_token="at-1", refresh_token="rt-1", expires_in=3600))

    provider = await require_oauth_provider(settings)

    assert isinstance(provider, OAuthClientProvider)
    assert provider.context.redirect_handler is None
    assert provider.context.callback_handler is None


async def test_require_oauth_provider_reuses_an_expired_but_refreshable_token(
    tmp_path: Path,
) -> None:
    """An expired token with a refresh token is left to the SDK's own
    refresh on the next call, not treated as unauthorized up front."""
    settings = make_settings(tmp_path)
    storage = FileTokenStorage(settings.honeycomb_oauth_token_path)
    await storage.set_tokens(OAuthToken(access_token="at-1", refresh_token="rt-1", expires_in=-100))

    provider = await require_oauth_provider(settings)
    assert isinstance(provider, OAuthClientProvider)


def test_build_provider_targets_the_configured_mcp_url_and_scope(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    provider = build_provider(settings)
    assert provider.context.server_url == settings.honeycomb_mcp_url
    assert provider.context.client_metadata.scope == "mcp:read mcp:write"
    assert provider.context.client_metadata.redirect_uris is not None
    assert str(provider.context.client_metadata.redirect_uris[0]).endswith("/callback")


# --------------------------------------------------------------------------
# status_line
# --------------------------------------------------------------------------


def test_status_line_with_no_token_names_the_login_command(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    line = status_line(settings)
    assert "agent.auth login" in line


async def test_status_line_with_a_valid_token_reports_expiry_and_refreshable(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    storage = FileTokenStorage(settings.honeycomb_oauth_token_path)
    await storage.set_tokens(OAuthToken(access_token="at-1", refresh_token="rt-1", expires_in=3600))

    line = status_line(settings)
    assert "token on file" in line
    assert "expires" in line
    assert "refreshable" in line


async def test_status_line_never_prints_the_token_itself(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    storage = FileTokenStorage(settings.honeycomb_oauth_token_path)
    await storage.set_tokens(OAuthToken(access_token="super-secret-value", expires_in=3600))

    line = status_line(settings)
    assert "super-secret-value" not in line
