"""Linear full-bundle plugin — Ava as a Linear agent.

ALL Linear functionality in one external plugin (per the port directive):
- the 6 ``linear_*`` tools (tools.py / client.py) — read + create + comment;
- the OAuth **agent identity** (identity.py) so comments + session activities post AS Ava;
- inbound **agent-session** handling via a poller (poller.py — works behind the LAN,
  no public URL) and an optional **webhook** fast-path + OAuth routes (webhook.py);
- the **bridge** (bridge.py) that drives an Ava turn on an inbound event and replies.

Outbound tools need only ``LINEAR_API_KEY``. The agent (post-as-Ava + sessions) needs
a Linear OAuth app (``LINEAR_AVA_CLIENT_ID`` / ``_CLIENT_SECRET`` / ``_REDIRECT_URI``);
authorize once at ``/plugins/linear/oauth/start``. Everything degrades gracefully when
unconfigured.
"""

from __future__ import annotations

import logging
import os

from . import bridge, tools
from .client import LinearClient
from .identity import DEFAULT_SCOPES, AvaActivityClient, AvaIdentity
from .poller import SessionPoller

log = logging.getLogger("protoagent.plugins.linear")


def _cfg(cfg: dict, key: str, env: str) -> str:
    return cfg.get(key) or os.environ.get(env, "") or ""


def register(registry) -> None:
    cfg = registry.config or {}
    client = LinearClient(_cfg(cfg, "api_key", "LINEAR_API_KEY"))
    identity = AvaIdentity(
        client_id=_cfg(cfg, "ava_client_id", "LINEAR_AVA_CLIENT_ID"),
        client_secret=_cfg(cfg, "ava_client_secret", "LINEAR_AVA_CLIENT_SECRET"),
        redirect_uri=_cfg(cfg, "ava_redirect_uri", "LINEAR_AVA_REDIRECT_URI"),
        scopes=_cfg(cfg, "ava_scopes", "LINEAR_AVA_SCOPES") or DEFAULT_SCOPES,
    )
    activity = AvaActivityClient(identity=identity, client=client)
    tools.bind(client, activity)

    for t in tools.TOOLS:
        registry.register_tool(t)

    # OAuth + webhook router (the webhook is an optional fast-path; OAuth /start is
    # how the operator authorizes Ava's identity).
    try:
        from .webhook import build_router
        secret = _cfg(cfg, "webhook_secret", "LINEAR_WEBHOOK_SECRET")
        registry.register_router(build_router(client, identity, activity, bridge, webhook_secret=secret))
    except Exception:  # noqa: BLE001 — router is best-effort
        log.exception("[linear] mounting OAuth/webhook router failed")

    # Console view: public page (/plugins/linear/view) + gated data (/api/plugins/linear/*).
    try:
        from .view import build_data_router
        registry.register_router(build_data_router(client, identity), prefix="/api/plugins/linear")
    except Exception:  # noqa: BLE001 — view is best-effort
        log.exception("[linear] mounting view data router failed")

    # Inbound poller surface — primary inbound path; self-idles until api_key +
    # an authorized OAuth identity are present.
    poller = SessionPoller(client, identity, activity, bridge)
    registry.register_surface(poller.start, stop=poller.stop, name="linear-session-poller")

    state = "configured" if client.configured() else "no api_key (tools return a setup hint)"
    ident = "OAuth identity set" if identity.configured() else "no OAuth identity (comments via API key)"
    log.info("[linear] registered 6 tool(s) + OAuth/webhook router + session poller — %s; %s", state, ident)
