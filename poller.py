"""Stale/pending agent-session poller — the inbound path that needs NO public URL.

Linear's agent-session webhooks are both unreliable AND (here) unreachable: this
instance is LAN/mDNS-only and, under a bearer, plugin webhook paths are gated. So
the poller is the PRIMARY inbound mechanism — it polls Linear (outbound) for agent
sessions awaiting Ava and drives each through the bridge. The webhook (webhook.py)
is an optional fast-path for deployments that DO have a public tunnel.

Idle + safe by default: it only runs when the OAuth agent identity is authorized
(agent sessions require actor=app to respond), polls on an interval, and dedups by
(session_id, updatedAt) so a session is handled once per update.
"""

from __future__ import annotations

import asyncio
import logging

try:  # relative at runtime (loaded as the `linear` package), flat in tests
    from .events import Inbound
except ImportError:  # pragma: no cover
    from events import Inbound

log = logging.getLogger("protoagent.plugins.linear")

_AWAITING = {"pending", "active", "stale"}  # session states that may need a response
_SESSIONS_Q = ("{ agentSessions(first: 25) { nodes { id status updatedAt"
               " issue { id identifier } } } }")


class SessionPoller:
    def __init__(self, client, identity, activity, bridge, *, interval_s: float = 30.0):
        self.client, self.identity, self.activity, self.bridge = client, identity, activity, bridge
        self.interval_s = interval_s
        self._task: asyncio.Task | None = None
        self._seen: set[str] = set()
        self._stop = asyncio.Event()

    def _poll_once(self) -> list[Inbound]:
        """One synchronous poll → the actionable agent sessions not yet handled."""
        data = self.client.graphql(_SESSIONS_Q)
        out: list[Inbound] = []
        for n in data.get("agentSessions", {}).get("nodes", []):
            if str(n.get("status", "")).lower() not in _AWAITING:
                continue
            key = f"{n.get('id')}:{n.get('updatedAt')}"
            if key in self._seen:
                continue
            self._seen.add(key)
            out.append(Inbound("agent_session", "prompted", True,
                               (n.get("issue") or {}).get("id", ""), "", n.get("id", ""), "session"))
        return out

    async def _loop(self) -> None:
        # Only poll when we can actually respond as the agent.
        if not (self.client.configured() and self.identity.authorized()):
            log.info("[linear] session poller idle — needs api_key + an authorized OAuth identity")
            return
        log.info("[linear] session poller started (every %.0fs)", self.interval_s)
        while not self._stop.is_set():
            try:
                for ev in await asyncio.to_thread(self._poll_once):
                    try:
                        await asyncio.to_thread(self.bridge.handle, ev, self.client, self.identity, self.activity)
                    except Exception:  # noqa: BLE001 — one bad session shouldn't kill the loop
                        log.exception("[linear] session %s handling failed", ev.session_id)
            except Exception:  # noqa: BLE001 — transient Linear/API error; keep polling
                log.warning("[linear] session poll failed; retrying next interval")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
