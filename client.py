"""Linear GraphQL client — the 6 agent operations over httpx.

API-key auth (``Authorization: <key>``); a personal API key authors writes as the
key's owner. The OAuth *agent identity* (post AS Ava) lives in identity.py and
reuses ``graphql()`` with a Bearer token. Logic is here so the @tool wrappers in
tools.py stay thin and this stays unit-testable with a mocked transport.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

GRAPHQL_URL = "https://api.linear.app/graphql"

_PRIORITY = {"urgent": 1, "high": 2, "medium": 3, "low": 4, "none": 0}
_IDENTIFIER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-(\d+)$")

_ISSUE_FIELDS = """
  id identifier title state { name } priority
  assignee { displayName name } team { key } url updatedAt
"""


class LinearError(RuntimeError):
    """Surfaced to the agent as a readable string."""


@dataclass
class LinearClient:
    api_key: str

    def configured(self) -> bool:
        return bool(self.api_key)

    def graphql(self, query: str, variables: dict | None = None, *,
                client: httpx.Client | None = None, auth: str | None = None) -> dict:
        """Run a GraphQL op. ``auth`` overrides the header (Bearer token for the
        OAuth agent identity); default is the raw API key."""
        header = auth or self.api_key
        if not header:
            raise LinearError("Linear is not configured — set api_key (LINEAR_API_KEY).")
        owns = client is None
        c = client or httpx.Client(timeout=30)
        try:
            resp = c.post(GRAPHQL_URL, json={"query": query, "variables": variables or {}},
                          headers={"Authorization": header, "Content-Type": "application/json"})
            resp.raise_for_status()
            data = resp.json()
        finally:
            if owns:
                c.close()
        if data.get("errors"):
            raise LinearError("; ".join(e.get("message", "?") for e in data["errors"]))
        return data.get("data") or {}

    # ── reads ────────────────────────────────────────────────────────────────

    def list_teams(self, *, client=None) -> list[dict]:
        d = self.graphql("{ teams(first: 50) { nodes { id key name } } }", client=client)
        return d.get("teams", {}).get("nodes", [])

    def list_issues(self, team: str = "", state: str = "", label: str = "",
                    assignee: str = "", max: int = 50, *, client=None) -> list[dict]:
        filt: dict = {}
        if team:
            filt["team"] = {"key": {"eq": team}}
        if state:
            filt["state"] = {"name": {"eqIgnoreCase": state}}
        if label:
            filt["labels"] = {"name": {"eqIgnoreCase": label}}
        if assignee == "me":
            filt["assignee"] = {"isMe": {"eq": True}}
        q = ("query($filter: IssueFilter, $first: Int) {"
             f" issues(filter: $filter, first: $first, orderBy: updatedAt) {{ nodes {{ {_ISSUE_FIELDS} }} }} }}")
        d = self.graphql(q, {"filter": filt, "first": min(int(max), 250)}, client=client)
        return [_issue(n) for n in d.get("issues", {}).get("nodes", [])]

    def search_issues(self, query: str, max: int = 25, *, client=None) -> list[dict]:
        q = ("query($q: String!, $first: Int) {"
             f" searchIssues(term: $q, first: $first) {{ nodes {{ {_ISSUE_FIELDS} }} }} }}")
        d = self.graphql(q, {"q": query, "first": min(int(max), 100)}, client=client)
        return [_issue(n) for n in d.get("searchIssues", {}).get("nodes", [])]

    def get_issue(self, id_or_key: str, *, client=None) -> dict:
        m = _IDENTIFIER_RE.match(id_or_key.strip())
        if m:  # TEAM-123 → filter by team key + number
            q = ("query($key: String!, $num: Float!) { issues(filter: {"
                 " team: { key: { eq: $key } }, number: { eq: $num } }, first: 1) { nodes {"
                 f" {_ISSUE_FIELDS} description labels {{ nodes {{ name }} }}"
                 " comments(first: 50) { nodes { body user { displayName name } createdAt } } } } }")
            d = self.graphql(q, {"key": m.group(1).upper(), "num": float(m.group(2))}, client=client)
            nodes = d.get("issues", {}).get("nodes", [])
            if not nodes:
                raise LinearError(f"No issue {id_or_key!r}.")
            return _issue_detail(nodes[0])
        q = ("query($id: String!) { issue(id: $id) {"
             f" {_ISSUE_FIELDS} description labels {{ nodes {{ name }} }}"
             " comments(first: 50) { nodes { body user { displayName name } createdAt } } } }")
        d = self.graphql(q, {"id": id_or_key}, client=client)
        node = d.get("issue")
        if not node:
            raise LinearError(f"No issue {id_or_key!r}.")
        return _issue_detail(node)

    # ── writes ───────────────────────────────────────────────────────────────

    def _team_id(self, team_key: str, *, client=None) -> str:
        for t in self.list_teams(client=client):
            if t.get("key", "").upper() == team_key.upper():
                return t["id"]
        raise LinearError(f"Unknown team key {team_key!r}.")

    def create_issue(self, team_key: str, title: str, description: str = "",
                     priority: str = "none", *, client=None) -> dict:
        # Dedup: a normalized-title match among the team's recent open issues.
        norm = " ".join(title.lower().split())
        for it in self.list_issues(team=team_key, max=50, client=client):
            if " ".join(it["title"].lower().split()) == norm and it["state"].lower() not in ("done", "canceled"):
                return {"id": it["id"], "teamKey": team_key, "title": title,
                        "deduped": True, "existingIdentifier": it["identifier"], "existingState": it["state"]}
        team_id = self._team_id(team_key, client=client)
        q = ("mutation($input: IssueCreateInput!) { issueCreate(input: $input) {"
             " success issue { id identifier url } } }")
        d = self.graphql(q, {"input": {"teamId": team_id, "title": title,
                          "description": description or None, "priority": _PRIORITY.get(priority, 0)}}, client=client)
        res = d.get("issueCreate", {})
        if not res.get("success"):
            raise LinearError("issueCreate failed.")
        issue = res.get("issue", {})
        return {"id": issue.get("id", ""), "identifier": issue.get("identifier", ""),
                "url": issue.get("url", ""), "teamKey": team_key, "title": title}

    def add_comment(self, issue_id: str, body: str, *, client=None, auth: str | None = None) -> dict:
        """Comment on an issue. ``auth`` (a Bearer token) posts as the OAuth agent
        identity (Ava); default posts as the API-key owner."""
        q = "mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }"
        d = self.graphql(q, {"input": {"issueId": issue_id, "body": body}}, client=client, auth=auth)
        return {"success": bool(d.get("commentCreate", {}).get("success")),
                "as": "ava" if auth else "api-key"}


def _issue(n: dict) -> dict:
    return {
        "id": n.get("id", ""),
        "identifier": n.get("identifier", ""),
        "title": n.get("title", ""),
        "state": (n.get("state") or {}).get("name", ""),
        "priority": n.get("priority", 0),
        "assignee": (n.get("assignee") or {}).get("displayName") or (n.get("assignee") or {}).get("name", ""),
        "team": (n.get("team") or {}).get("key", ""),
        "url": n.get("url", ""),
        "updatedAt": n.get("updatedAt", ""),
    }


def _issue_detail(n: dict) -> dict:
    d = _issue(n)
    d["description"] = n.get("description", "")
    d["labels"] = [x.get("name", "") for x in (n.get("labels") or {}).get("nodes", [])]
    d["comments"] = [
        {"author": (c.get("user") or {}).get("displayName") or (c.get("user") or {}).get("name", ""),
         "body": c.get("body", ""), "createdAt": c.get("createdAt", "")}
        for c in (n.get("comments") or {}).get("nodes", [])
    ]
    return d
