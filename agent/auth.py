"""OAuth login for the hosted Honeycomb MCP's write tools (R12, EDW-1334).

Canvas (`canvas_agent_invoke`) needs a user actor: a management key's call
fails with `poodle investigation bind returned 400: actor_user_hcid is
required`, verified live on 2026-09-07 (see CLAUDE.md's Honeycomb facts).
Every read-only path, including the whole eval matrix,
keeps working under `HONEYCOMB_MCP_KEY`; OAuth is opt-in, selected by
`Settings.honeycomb_auth = "oauth"`, and only ever needed for Canvas.

    uv run python -m agent.auth login
    uv run python -m agent.auth status

`login` is the one place in this project a browser opens: it runs the
authorization code + PKCE flow against `ui.honeycomb.io` (discovered from
`https://mcp.honeycomb.io/.well-known/oauth-protected-resource`, per RFC
9728) and stores the result. `status` reports what is on disk without any
network call. `agent/mcp_client.py`'s `_open_streams` builds a provider from
the same storage for `honeycomb_auth = "oauth"` and lets the SDK refresh
silently on later calls; `require_oauth_provider` is what it calls, and that
function raises `OAuthNotAuthorized` naming this module's `login` command
rather than opening a browser itself when there is no usable token. Only
`login`, run by a person at a terminal, ever does that.

Token storage. `FileTokenStorage` keeps the access token, refresh token, and
the client Honeycomb's dynamic registration returned in one JSON file
(default `~/.receipts/honeycomb_oauth.json`, see `Settings.honeycomb_oauth_token_path`),
created 0600 and chmod'd back to 0600 on every write in case the umask
loosened it. It also records `expires_at`, a field the SDK itself does not
persist: `mcp`'s `OAuthToken` model carries only the relative `expires_in`
the server sent, and the absolute `token_expiry_time` `OAuthContext` derives
from it (`mcp.client.auth.oauth2.OAuthContext.update_token_expiry`) lives in
memory for one process. `status` needs an absolute time to report even in a
fresh process, so `set_tokens` here computes one the same way
(`time.time() + expires_in`) and writes it alongside.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import BaseModel, ValidationError

from receipts.settings import Settings

CALLBACK_PATH = "/callback"
CALLBACK_PORT = 8765
SCOPE = "mcp:read mcp:write"

# How long `login` waits for the browser round trip before giving up.
LOGIN_TIMEOUT_S = 300.0


def _validate_or_none[Model: BaseModel](model: type[Model], raw: Any) -> Model | None:
    """`model.model_validate(raw)`, or `None` for a falsy `raw` or one that
    fails validation.

    A hand-edited or corrupted token file (a `tokens` block whose
    `access_token` is a number, say) must read the same as no token on
    file at all: the caller already has a clear, actionable message for
    "nothing stored" (`OAuthNotAuthorized`, naming `login`), and a
    `pydantic.ValidationError` escaping from here instead would reach
    `evals.run.main` as a bare stack trace with no such message.
    """
    if not raw:
        return None
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None


class OAuthNotAuthorized(RuntimeError):
    """No usable Honeycomb OAuth token is on file.

    Raised by `require_oauth_provider`, never by `mcp_client.py` opening a
    browser on its own: an eval run configured for `honeycomb_auth="oauth"`
    with a missing or unrefreshable token must fail with a clear message,
    not hang waiting on a login prompt nobody is watching. `python -m
    agent.auth login` is the one command that can clear this.
    """


@dataclass(frozen=True)
class StoredStatus:
    """What `FileTokenStorage.read_status` finds, without any network call."""

    tokens: OAuthToken | None
    expires_at: float | None
    client: OAuthClientInformationFull | None


class FileTokenStorage(TokenStorage):
    """One JSON file holding the token, its absolute expiry, and the client info.

    A token here is as sensitive as an API key, so CLAUDE.md's secrets rule
    applies to it the same way: never printed in full, logged, put in a
    fixture, or committed. Every write chmods the file back to 0600.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        """Write `data` as the token file, 0600 from the moment it exists.

        `Path.write_text` on a file that does not exist yet creates it at
        the mode `open()`'s own default gives it, subject to the process
        umask (0644 under a umask of 022), and a `chmod` right after leaves
        the access token world-readable for the gap between the two calls.
        `os.open` with an explicit mode closes that window: a mode of 0600
        has no group or other bits for umask to fail to mask off, so the
        file is owner-only from the instant it is created. The `chmod`
        still runs afterward, for a file that already exists at some other
        mode.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, indent=2)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)

    async def get_tokens(self) -> OAuthToken | None:
        return _validate_or_none(OAuthToken, self._read().get("tokens"))

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        data["expires_at"] = (
            time.time() + tokens.expires_in if tokens.expires_in is not None else None
        )
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return _validate_or_none(OAuthClientInformationFull, self._read().get("client"))

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client"] = info.model_dump(mode="json")
        self._write(data)

    def read_status(self) -> StoredStatus:
        """The stored token, its absolute expiry, and the client info, read-only."""
        raw = self._read()
        tokens = _validate_or_none(OAuthToken, raw.get("tokens"))
        client = _validate_or_none(OAuthClientInformationFull, raw.get("client"))
        return StoredStatus(tokens=tokens, expires_at=raw.get("expires_at"), client=client)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures exactly one authorization code from `CALLBACK_PATH`.

    Two bugs the throwaway spike this was built from had, fixed here: any
    request whose path is not `/callback` (the browser fetches `/favicon.ico`
    immediately after rendering the redirect page) gets a plain 404 instead
    of being read as a failed callback, and a code already captured is never
    overwritten by a second request to the same path.
    """

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's own name)
        box: dict[str, str] = self.server.code_box  # type: ignore[attr-defined]
        parts = urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            self._respond(404, b"")
            return
        query = parse_qs(parts.query)
        if "code" in query and "code" not in box:
            box["code"] = query["code"][0]
            box["state"] = query.get("state", [""])[0]
            self._respond(200, b"Authorized. You can close this tab.")
        elif "code" in box:
            self._respond(200, b"Already authorized. You can close this tab.")
        else:
            box["error"] = query.get("error", ["no code in callback"])[0]
            self._respond(200, b"Authorization failed. You can close this tab.")

    def _respond(self, status_code: int, body: bytes) -> None:
        self.send_response(status_code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence BaseHTTPRequestHandler's own logging
        pass


async def _redirect_handler(url: str) -> None:
    print(f"Open this URL to authorize Honeycomb (opening it now too):\n{url}\n", file=sys.stderr)
    webbrowser.open(url)


def _make_callback_handler(box: dict[str, str]):
    async def callback_handler() -> AuthorizationCodeResult:
        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        while time.monotonic() < deadline:
            if "code" in box or "error" in box:
                break
            await asyncio.sleep(0.25)
        if "error" in box:
            raise OAuthNotAuthorized(f"authorization failed: {box['error']}")
        if "code" not in box:
            raise OAuthNotAuthorized("timed out waiting for the browser authorization")
        return AuthorizationCodeResult(code=box["code"], state=box.get("state") or None)

    return callback_handler


def build_provider(
    settings: Settings,
    *,
    redirect_handler: Any = None,
    callback_handler: Any = None,
) -> OAuthClientProvider:
    """The `OAuthClientProvider` passed as `auth=` to the httpx2 client that
    `agent/mcp_client.py`'s `_open_streams` builds.

    With no handlers (the default `require_oauth_provider` uses), the
    provider can still reuse a stored token and let the SDK refresh it
    through a stored refresh token, but if it ever needed a fresh
    authorization it would raise instead of opening a browser: the SDK's own
    `_perform_authorization_code_grant` requires a `redirect_handler`. Only
    `login` below passes real ones.
    """
    storage = FileTokenStorage(settings.honeycomb_oauth_token_path)
    return OAuthClientProvider(
        server_url=settings.honeycomb_mcp_url,
        client_metadata=OAuthClientMetadata(
            client_name="receipts-investigator",
            redirect_uris=[f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=SCOPE,
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


async def require_oauth_provider(settings: Settings) -> OAuthClientProvider:
    """The provider for a run that must never open a browser.

    Raises `OAuthNotAuthorized`, naming the login command, when there is no
    stored token at all, or when the stored token has already expired and
    carries no refresh token to renew it with. The SDK's own refresh only
    fires on a 401 mid-call; checking here instead means a run configured
    for OAuth with a dead token fails before the session even opens, with a
    message that says what to run, rather than however an unauthenticated
    tool call happens to surface deep inside the first investigation step.
    """
    storage = FileTokenStorage(settings.honeycomb_oauth_token_path)
    status = storage.read_status()
    if status.tokens is None:
        raise OAuthNotAuthorized(
            "no Honeycomb OAuth token on file; run `uv run python -m agent.auth login` first"
        )
    expired = status.expires_at is not None and status.expires_at <= time.time()
    if expired and not status.tokens.refresh_token:
        raise OAuthNotAuthorized(
            "the stored Honeycomb OAuth token has expired and has no refresh token; "
            "run `uv run python -m agent.auth login` again"
        )
    return build_provider(settings)


async def login(settings: Settings) -> None:
    """Run the browser authorization flow once and store the result.

    Opens a local HTTP server on `CALLBACK_PORT` for the redirect, then a
    real MCP session (the same `streamable_http_client` + `ClientSession`
    path production uses) so the flow exercises exactly what a later run
    will do, not a simplified stand-in for it.
    """
    box: dict[str, str] = {}
    httpd = HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    httpd.code_box = box  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        provider = build_provider(
            settings,
            redirect_handler=_redirect_handler,
            callback_handler=_make_callback_handler(box),
        )
        async with httpx2.AsyncClient(
            auth=provider, timeout=httpx2.Timeout(30.0, read=300.0)
        ) as client:
            async with streamable_http_client(settings.honeycomb_mcp_url, http_client=client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
    print(f"Honeycomb OAuth: authorized and stored at {settings.honeycomb_oauth_token_path}.")


def status_line(settings: Settings) -> str:
    """One line reporting whether a usable token is on file."""
    storage = FileTokenStorage(settings.honeycomb_oauth_token_path)
    data = storage.read_status()
    if data.tokens is None:
        return "no Honeycomb OAuth token on file; run `uv run python -m agent.auth login`"
    if data.expires_at is None:
        expiry = "no expiry reported"
    else:
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(data.expires_at))
        expiry = f"expires {when}" if data.expires_at > time.time() else f"expired {when}"
    refreshable = ", refreshable" if data.tokens.refresh_token else ", not refreshable"
    return f"token on file, {expiry}{refreshable}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.auth")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="run the browser OAuth flow and store the token")
    sub.add_parser("status", help="report whether a valid token is on file, no network call")
    args = parser.parse_args(argv)

    settings = Settings()
    if args.command == "login":
        asyncio.run(login(settings))
        return 0
    print(status_line(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
