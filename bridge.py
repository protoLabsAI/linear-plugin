"""Agent bridge — turns an inbound Linear event into an Ava turn + a reply.

This is the "Ava as a Linear agent" glue. On an actionable event it:
1. (agent sessions) posts a quick *thought* ack so Linear doesn't mark the session
   unresponsive while the turn runs,
2. pulls issue context and drives a turn on THIS agent over the local
   OpenAI-compatible endpoint (the full tool loop — she can read the issue, route,
   file a GitHub issue, etc.),
3. posts the result back: a session ``response`` (as Ava) for agent sessions, or an
   issue ``comment`` (as Ava when OAuth is set, else via the API key) for mentions.

The agent call + Linear posting are injectable so the orchestration is testable
offline; live use needs a public webhook fronting this instance.
"""

from __future__ import annotations

import logging
import os

import httpx

try:  # relative at runtime (loaded as the `linear` package), flat in tests
    from .events import Inbound
except ImportError:  # pragma: no cover
    from events import Inbound

log = logging.getLogger("protoagent.plugins.linear")

_AGENT_BASE = os.environ.get("LINEAR_AGENT_BASE_URL", "http://127.0.0.1:7870")
_MODEL = os.environ.get("LINEAR_AGENT_MODEL", "protolabs/reasoning")


def run_turn(prompt: str, *, base_url: str | None = None, token: str | None = None,
             client: httpx.Client | None = None, timeout: float = 180) -> str:
    """Drive one agent turn via /v1/chat/completions and return the reply text."""
    base = base_url or _AGENT_BASE
    token = token if token is not None else os.environ.get("A2A_AUTH_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    owns = client is None
    c = client or httpx.Client(timeout=timeout)
    try:
        r = c.post(f"{base}/v1/chat/completions", headers=headers,
                   json={"model": _MODEL, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    finally:
        if owns:
            c.close()


def build_prompt(ev: Inbound, issue: dict) -> str:
    """The instruction Ava receives for an inbound Linear event."""
    head = (f"Inbound Linear event ({ev.kind}/{ev.action}) on issue "
            f"{issue.get('identifier', ev.issue_id)} — \"{issue.get('title', '')}\".")
    body = issue.get("description", "") or ""
    ctx = f"\n\nIssue body:\n{body}" if body else ""
    if ev.respond_via == "session":
        ask = ("\n\nYou were assigned/prompted via a Linear agent session. Act on it as Ava "
               "(route engineering work by filing a GitHub issue on the owning project; reply "
               "concisely with what you did). Your reply becomes your session response.")
    else:
        ask = ("\n\nYou were @mentioned. Reply concisely as Ava — answer or route the work, and "
               "say where you sent it. Sign with — Ava (protoLabs).")
    return head + ctx + ask


def handle(ev: Inbound, client, identity, activity, *,
           run=run_turn, http: httpx.Client | None = None) -> dict:
    """Orchestrate one inbound event end-to-end. Returns a small result dict.

    ``client`` = LinearClient, ``activity`` = AvaActivityClient (post as Ava),
    ``run`` = the agent caller (injectable for tests)."""
    if not ev.actionable:
        return {"handled": False, "reason": f"not actionable ({ev.kind}/{ev.action})"}

    # Agent sessions: ack fast so Linear keeps the session live while we work.
    if ev.respond_via == "session" and ev.session_id:
        try:
            activity.thought(ev.session_id, "On it — reading the issue and routing.", http=http)
        except Exception:  # noqa: BLE001 — ack is best-effort
            log.warning("[linear] thought ack failed for session %s", ev.session_id)

    issue = client.get_issue(ev.issue_id, client=http) if ev.issue_id else {}
    reply = run(build_prompt(ev, issue))

    if ev.respond_via == "session" and ev.session_id:
        if activity.response(ev.session_id, reply, http=http):
            return {"handled": True, "via": "session", "as": "ava"}
        # No OAuth identity → fall back to a comment on the issue.
    if ev.issue_id:
        if identity.configured() and activity.create_comment(ev.issue_id, reply, http=http):
            return {"handled": True, "via": "comment", "as": "ava"}
        client.add_comment(ev.issue_id, reply, client=http)
        return {"handled": True, "via": "comment", "as": "api-key"}
    return {"handled": False, "reason": "no issue/session to reply to"}
