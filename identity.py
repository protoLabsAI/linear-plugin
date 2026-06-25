"""Linear OAuth *agent identity* — so comments and agent-session activities post
AS Ava (actor=app), not as the API-key owner.

Two pieces:
- ``AvaIdentity`` — the OAuth app config + token store (refresh token persisted to
  an instance-scoped 0600 JSON file; access tokens cached in-memory and refreshed
  ~5 min before expiry).
- ``AvaActivityClient`` — posts as Ava: issue comments and agent-session activities
  (``thought`` ack + final ``response``) via Linear's agentActivityCreate.

Unconfigured (no OAuth app) is fine: the plugin falls back to API-key comments and
skips agent-session acks. Token refresh + activity calls take an injectable httpx
client so they're unit-testable offline.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://linear.app/oauth/authorize"
TOKEN_URL = "https://api.linear.app/oauth/token"
DEFAULT_SCOPES = "read,write,app:assignable,app:mentionable"
_EARLY_REFRESH_S = 300


def _data_dir() -> Path:
    base = Path(os.environ.get("LINEAR_DATA_DIR") or (Path.home() / ".protoagent" / "linear"))
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        base = base / inst
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class AvaIdentity:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: str = DEFAULT_SCOPES
    # in-memory access-token cache: (token, expires_at)
    _access: tuple[str, float] | None = field(default=None, repr=False)

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    # ── token store (refresh token persisted) ────────────────────────────────
    def _token_path(self) -> Path:
        return _data_dir() / "ava-oauth.json"

    def _load(self) -> dict:
        try:
            return json.loads(self._token_path().read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        path = self._token_path()
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def authorized(self) -> bool:
        return bool(self._load().get("refresh_token"))

    # ── OAuth flow ───────────────────────────────────────────────────────────
    def authorize_url(self, state: str) -> str:
        return AUTHORIZE_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": self.scopes, "state": state,
            "actor": "app", "prompt": "consent",
        })

    def exchange_code(self, code: str, *, client: httpx.Client | None = None) -> None:
        tok = self._token_post({"grant_type": "authorization_code", "code": code,
                                "redirect_uri": self.redirect_uri}, client=client)
        store = self._load()
        if tok.get("refresh_token"):
            store["refresh_token"] = tok["refresh_token"]
        self._save(store)
        self._access = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)))

    def _token_post(self, extra: dict, *, client: httpx.Client | None = None) -> dict:
        owns = client is None
        c = client or httpx.Client(timeout=30)
        try:
            resp = c.post(TOKEN_URL, data={"client_id": self.client_id,
                          "client_secret": self.client_secret, **extra})
            resp.raise_for_status()
            return resp.json()
        finally:
            if owns:
                c.close()

    def access_token(self, *, client: httpx.Client | None = None) -> str | None:
        """A valid Bearer access token for actor=app calls, or None if unauthorized."""
        if not self.configured():
            return None
        if self._access and time.time() < self._access[1] - _EARLY_REFRESH_S:
            return self._access[0]
        refresh = self._load().get("refresh_token")
        if not refresh:
            return None
        tok = self._token_post({"grant_type": "refresh_token", "refresh_token": refresh}, client=client)
        store = self._load()
        if tok.get("refresh_token"):  # Linear may rotate it
            store["refresh_token"] = tok["refresh_token"]
            self._save(store)
        self._access = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)))
        return self._access[0]


@dataclass
class AvaActivityClient:
    identity: AvaIdentity
    client: "object"  # a LinearClient (for graphql())

    def _bearer(self, *, http: httpx.Client | None = None) -> str | None:
        tok = self.identity.access_token(client=http)
        return f"Bearer {tok}" if tok else None

    def create_comment(self, issue_id: str, body: str, *, http: httpx.Client | None = None) -> bool:
        bearer = self._bearer(http=http)
        if not bearer:
            return False
        self.client.add_comment(issue_id, body, client=http, auth=bearer)
        return True

    def _agent_activity(self, session_id: str, kind: str, body: str, *, http: httpx.Client | None = None) -> bool:
        bearer = self._bearer(http=http)
        if not bearer:
            return False
        q = ("mutation($input: AgentActivityCreateInput!) {"
             " agentActivityCreate(input: $input) { success } }")
        d = self.client.graphql(q, {"input": {"agentSessionId": session_id,
                                "content": {"type": kind, "body": body}}}, client=http, auth=bearer)
        return bool(d.get("agentActivityCreate", {}).get("success"))

    def thought(self, session_id: str, body: str, *, http: httpx.Client | None = None) -> bool:
        """A quick 'thought' ack so Linear doesn't mark the session unresponsive."""
        return self._agent_activity(session_id, "thought", body, http=http)

    def response(self, session_id: str, body: str, *, http: httpx.Client | None = None) -> bool:
        """The final agent response, posted into the session as Ava."""
        return self._agent_activity(session_id, "response", body, http=http)
