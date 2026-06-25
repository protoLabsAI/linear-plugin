"""Unit tests for the Linear plugin — mocked httpx, no network.

    cd ~/dev/protoAgent && uv run --frozen python -m pytest ~/dev/linear-plugin/tests -q

Loads the plugin modules by file path (no package machinery). Covers the GraphQL
client, webhook signature + event classification, the OAuth token refresh, and the
agent-bridge orchestration. The FastAPI router + the live poller loop are exercised
end-to-end once credentials + a tunnel exist.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


client_mod = _load("client", "client.py")
events = _load("events", "events.py")
identity_mod = _load("identity", "identity.py")
bridge = _load("bridge", "bridge.py")
LinearClient, LinearError = client_mod.LinearClient, client_mod.LinearError


# ── GraphQL client ────────────────────────────────────────────────────────────

def _gql_handler(by_query):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        q = body["query"]
        for needle, resp in by_query.items():
            if needle in q:
                return httpx.Response(200, json={"data": resp})
        return httpx.Response(200, json={"data": {}})
    return handler


def _client_with(by_query):
    return httpx.Client(transport=httpx.MockTransport(_gql_handler(by_query)))


def test_list_teams_and_issues_parse():
    lc = LinearClient("k")
    c = _client_with({
        "teams(first: 50)": {"teams": {"nodes": [{"id": "t1", "key": "JOSH", "name": "Josh"}]}},
        "issues(filter": {"issues": {"nodes": [{"id": "i1", "identifier": "JOSH-1", "title": "Bug",
                  "state": {"name": "Todo"}, "priority": 2, "assignee": {"displayName": "Ava"},
                  "team": {"key": "JOSH"}, "url": "u", "updatedAt": "t"}]}},
    })
    assert lc.list_teams(client=c)[0]["key"] == "JOSH"
    issues = lc.list_issues(team="JOSH", client=c)
    assert issues[0]["identifier"] == "JOSH-1" and issues[0]["state"] == "Todo" and issues[0]["assignee"] == "Ava"


def test_create_issue_dedup_short_circuits():
    lc = LinearClient("k")
    # An open issue with the same normalized title already exists → dedup, no create.
    c = _client_with({"issues(filter": {"issues": {"nodes": [{"id": "i9", "identifier": "JOSH-9",
                  "title": "Fix  the   thing", "state": {"name": "In Progress"}, "team": {"key": "JOSH"}}]}}})
    out = lc.create_issue("JOSH", "fix the thing", client=c)
    assert out["deduped"] and out["existingIdentifier"] == "JOSH-9"


def test_create_issue_creates_when_no_dup():
    lc = LinearClient("k")
    c = _client_with({
        "issues(filter": {"issues": {"nodes": []}},
        "teams(first: 50)": {"teams": {"nodes": [{"id": "T", "key": "JOSH", "name": "J"}]}},
        "issueCreate": {"issueCreate": {"success": True, "issue": {"id": "n1", "identifier": "JOSH-10", "url": "U"}}},
    })
    out = lc.create_issue("JOSH", "brand new", priority="high", client=c)
    assert out["identifier"] == "JOSH-10" and out["url"] == "U" and not out.get("deduped")


def test_graphql_errors_raise():
    lc = LinearClient("k")
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"errors": [{"message": "nope"}]})))
    with pytest.raises(LinearError):
        lc.list_teams(client=c)


def test_unconfigured_tool_path():
    assert not LinearClient("").configured()


# ── webhook signature + classification ────────────────────────────────────────

def test_verify_signature_roundtrip():
    secret, body = "s3cr3t", b'{"a":1}'
    good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert events.verify_signature(secret, body, good)
    assert not events.verify_signature(secret, body, "deadbeef")
    assert not events.verify_signature("", body, good)
    assert not events.verify_signature(secret, body, "")


def test_classify_mention_actionable():
    ev = events.classify({"type": "AppUserNotification", "action": "issueMention",
                          "notification": {"type": "issueMention", "issueId": "I1"}})
    assert ev.kind == "notification" and ev.actionable and ev.respond_via == "comment" and ev.issue_id == "I1"


def test_classify_assignment_not_actionable():
    ev = events.classify({"type": "AppUserNotification", "action": "issueAssignedToYou",
                          "notification": {"type": "issueAssignedToYou", "issueId": "I2"}})
    assert not ev.actionable


def test_classify_agent_session_actionable():
    ev = events.classify({"type": "AgentSessionEvent", "action": "created",
                          "agentSession": {"id": "S1", "issue": {"id": "I3"}}})
    assert ev.kind == "agent_session" and ev.actionable and ev.session_id == "S1" and ev.respond_via == "session"


def test_classify_standard_event_ambient():
    assert not events.classify({"type": "Issue", "action": "update", "data": {"id": "X"}}).actionable


# ── OAuth identity token refresh ──────────────────────────────────────────────

def test_identity_refreshes_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    ident = identity_mod.AvaIdentity("cid", "csec", "https://h/cb")
    (tmp_path / "ava-oauth.json").write_text(json.dumps({"refresh_token": "R"}))
    assert ident.authorized()
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})

    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert ident.access_token(client=c) == "AT"
    assert ident.access_token(client=c) == "AT"  # cached
    assert calls["n"] == 1


def test_identity_unconfigured_returns_none():
    assert identity_mod.AvaIdentity("", "", "").access_token() is None


# ── bridge orchestration (fakes) ──────────────────────────────────────────────

class _FakeClient:
    def get_issue(self, issue_id, client=None):
        return {"identifier": "JOSH-5", "title": "T", "description": "body"}

    def add_comment(self, issue_id, body, client=None):
        self.commented = (issue_id, body)
        return {"success": True, "as": "api-key"}


class _FakeActivity:
    def __init__(self, identity, can_respond=True):
        self.identity, self.can_respond = identity, can_respond
        self.thoughts, self.responses, self.comments = [], [], []

    def thought(self, sid, body, http=None):
        self.thoughts.append((sid, body)); return True

    def response(self, sid, body, http=None):
        self.responses.append((sid, body)); return self.can_respond

    def create_comment(self, issue_id, body, http=None):
        self.comments.append((issue_id, body)); return self.can_respond


class _Ident:
    def __init__(self, configured): self._c = configured
    def configured(self): return self._c


def test_bridge_session_acks_then_responds():
    ident = _Ident(True)
    act = _FakeActivity(ident, can_respond=True)
    ev = events.Inbound("agent_session", "prompted", True, "I3", "", "S1", "session")
    res = bridge.handle(ev, _FakeClient(), ident, act, run=lambda p: "done routing")
    assert res == {"handled": True, "via": "session", "as": "ava"}
    assert act.thoughts and act.responses[0] == ("S1", "done routing")


def test_bridge_session_falls_back_to_comment_without_oauth():
    ident = _Ident(False)
    act = _FakeActivity(ident, can_respond=False)  # response() fails (no OAuth)
    fc = _FakeClient()
    ev = events.Inbound("agent_session", "prompted", True, "I3", "", "S1", "session")
    res = bridge.handle(ev, fc, ident, act, run=lambda p: "reply")
    assert res["via"] == "comment" and res["as"] == "api-key" and fc.commented[1] == "reply"


def test_bridge_mention_comments_as_ava():
    ident = _Ident(True)
    act = _FakeActivity(ident, can_respond=True)
    ev = events.Inbound("notification", "issueMention", True, "I1", "", "", "comment")
    res = bridge.handle(ev, _FakeClient(), ident, act, run=lambda p: "answer")
    assert res == {"handled": True, "via": "comment", "as": "ava"} and act.comments[0] == ("I1", "answer")


def test_bridge_skips_non_actionable():
    ev = events.Inbound("standard", "update", False)
    res = bridge.handle(ev, _FakeClient(), _Ident(True), _FakeActivity(_Ident(True)), run=lambda p: "x")
    assert res["handled"] is False
