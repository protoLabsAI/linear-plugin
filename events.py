"""Linear inbound webhook logic — signature verification + event classification.

Pure functions (no I/O) so the security-critical bits are easy to test. The webhook
router (webhook.py) verifies the signature, classifies the payload, and hands the
actionable ones to the agent bridge.

Actionability mirrors protoWorkstacean: ambient Issue/Comment/Project events are NOT
auto-answered; only @mentions (issueMention / issueCommentMention) and agent sessions
(created / prompted) drive Ava.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

_MENTION_ACTIONS = {"issueMention", "issueCommentMention"}
_SESSION_ACTIONS = {"created", "prompted"}


@dataclass
class Inbound:
    kind: str            # "notification" | "agent_session" | "standard"
    action: str
    actionable: bool
    issue_id: str = ""
    comment_id: str = ""
    session_id: str = ""
    respond_via: str = ""  # "comment" | "session" | ""


def verify_signature(secret: str, raw_body: bytes, signature: str) -> bool:
    """Constant-time HMAC-SHA256 check of the raw body against the
    ``linear-signature`` header (hex digest). Empty secret ⇒ reject."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def classify(payload: dict) -> Inbound:
    """Normalize a Linear webhook payload into an Inbound with an actionable flag."""
    t = str(payload.get("type", ""))
    action = str(payload.get("action", ""))

    if t == "AppUserNotification":
        notif = payload.get("notification") or {}
        naction = str(notif.get("type") or action)
        issue_id = notif.get("issueId") or (notif.get("issue") or {}).get("id", "")
        comment_id = notif.get("commentId") or ""
        actionable = naction in _MENTION_ACTIONS
        return Inbound("notification", naction, actionable, issue_id, comment_id, "",
                       "comment" if actionable else "")

    if t == "AgentSessionEvent":
        sess = payload.get("agentSession") or {}
        session_id = sess.get("id") or payload.get("sessionId") or ""
        issue_id = (sess.get("issue") or {}).get("id") or sess.get("issueId") or ""
        actionable = action in _SESSION_ACTIONS
        return Inbound("agent_session", action, actionable, issue_id, "", session_id,
                       "session" if actionable else "")

    # Standard Issue / Comment / Project event — ambient, not auto-answered.
    data = payload.get("data") or {}
    return Inbound("standard", action, False, data.get("id", ""), "", "", "")
