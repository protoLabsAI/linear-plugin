"""The 6 linear_* agent tools — thin wrappers over client.py.

Writes (create/comment) post as the API-key owner unless the OAuth agent identity
is configured, in which case comments go out AS Ava (set by register()).
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

try:  # relative at runtime (loaded as the `linear` package), flat in tests
    from .client import LinearClient, LinearError
except ImportError:  # pragma: no cover
    from client import LinearClient, LinearError

log = logging.getLogger("protoagent.plugins.linear")

# Bound by register(): the LinearClient and the AvaActivityClient (or None).
_CLIENT: LinearClient = LinearClient("")
_ACTIVITY = None


def bind(client: LinearClient, activity) -> None:
    global _CLIENT, _ACTIVITY
    _CLIENT, _ACTIVITY = client, activity


def _run(fn, *a, **k):
    if not _CLIENT.configured():
        return "Linear isn't configured. Set linear.api_key (or LINEAR_API_KEY)."
    try:
        return fn(*a, **k)
    except LinearError as exc:
        return f"Linear error: {exc}"
    except Exception as exc:  # noqa: BLE001
        log.warning("[linear] %s failed: %s", getattr(fn, "__name__", fn), exc)
        return f"Linear request failed: {type(exc).__name__}: {exc}"


@tool
def linear_list_teams() -> str:
    """List Linear teams (id, key, name)."""
    out = _run(_CLIENT.list_teams)
    return out if isinstance(out, str) else json.dumps({"count": len(out), "teams": out}, indent=2)


@tool
def linear_list_issues(team: str = "", state: str = "", label: str = "", assignee: str = "", max: int = 50) -> str:
    """List Linear issues with optional filters.

    Args:
        team: team key (e.g. "JOSH").
        state: workflow state name (e.g. "In Progress"), case-insensitive.
        label: label name, case-insensitive.
        assignee: "me" to filter to the API-key holder.
        max: max issues (default 50, cap 250).
    """
    out = _run(_CLIENT.list_issues, team, state, label, assignee, max)
    return out if isinstance(out, str) else json.dumps({"count": len(out), "issues": out}, indent=2)


@tool
def linear_search_issues(query: str, max: int = 25) -> str:
    """Full-text search Linear issues.

    Args:
        query: search text.
        max: max results (default 25, cap 100).
    """
    out = _run(_CLIENT.search_issues, query, max)
    return out if isinstance(out, str) else json.dumps({"query": query, "count": len(out), "issues": out}, indent=2)


@tool
def linear_get_issue(id_or_key: str) -> str:
    """Read one Linear issue with description, labels, and comments.

    Args:
        id_or_key: a UUID or an identifier like "JOSH-142".
    """
    out = _run(_CLIENT.get_issue, id_or_key)
    return out if isinstance(out, str) else json.dumps(out, indent=2)


@tool
def linear_create_issue(team_key: str, title: str, description: str = "", priority: str = "none") -> str:
    """File a new Linear issue (deduped against the team's recent open issues).

    Args:
        team_key: team key (e.g. "JOSH").
        title: issue title.
        description: optional body (markdown).
        priority: urgent | high | medium | low | none (default none).
    """
    out = _run(_CLIENT.create_issue, team_key, title, description, priority)
    if isinstance(out, str):
        return out
    if out.get("deduped"):
        return f"Existing issue {out['existingIdentifier']} ({out['existingState']}) already covers this — not duplicated."
    return f"Filed {out.get('identifier') or out['id']} on {team_key}: {title}  {out.get('url', '')}".strip()


@tool
def linear_add_comment(issue_id: str, body: str) -> str:
    """Comment on a Linear issue. Posts as Ava when the OAuth agent identity is
    configured, otherwise as the API-key owner.

    Args:
        issue_id: the issue UUID.
        body: comment text (markdown).
    """
    # Prefer the agent identity (post AS Ava) when available.
    if _ACTIVITY is not None and getattr(_ACTIVITY, "identity", None) and _ACTIVITY.identity.configured():
        try:
            if _ACTIVITY.create_comment(issue_id, body):
                return "Commented as Ava."
        except Exception as exc:  # noqa: BLE001 — fall back to the API key
            log.warning("[linear] comment-as-Ava failed, falling back to API key: %s", exc)
    out = _run(_CLIENT.add_comment, issue_id, body)
    return out if isinstance(out, str) else ("Commented." if out.get("success") else "Comment failed.")


TOOLS = [linear_list_teams, linear_list_issues, linear_search_issues,
         linear_get_issue, linear_create_issue, linear_add_comment]
