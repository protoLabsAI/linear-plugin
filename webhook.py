"""Linear OAuth + webhook router (optional fast-path).

Mounts under ``/plugins/linear``:
- ``GET /oauth/start``    — operator begins the actor=app OAuth grant (redirect to Linear).
- ``GET /oauth/callback`` — Linear redirects back with ?code&state; exchanges + stores the refresh token.
- ``POST /webhook``       — Linear webhook: HMAC-verified, classified, actionable events dispatched to the bridge.

NOTE on reachability: under a configured bearer, the host's default-deny auth
middleware gates ``/plugins/*`` too — so the webhook 401s for Linear unless this
path is made auth-exempt AND fronted by a public tunnel. The poller (poller.py) is
the inbound path that works without either. This router is here for deployments
that do have a public, auth-exempt ingress.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .events import classify, verify_signature
from .view import PAGE

log = logging.getLogger("protoagent.plugins.linear")

# NOTE: imports live at module level (not inside build_router) on purpose — with
# `from __future__ import annotations` the route handlers' `request: Request`
# annotation is a STRING that FastAPI resolves against THIS module's globals. If
# Request were imported only inside build_router, that resolution fails and FastAPI
# mis-reads `request` as a required query param (422). FastAPI is always present
# when this module is imported (register() builds the router at plugin load).


def build_router(client, identity, activity, bridge, *, webhook_secret: str):
    router = APIRouter()
    # CSRF state for the OAuth round-trip (process-local; fine for a single operator).
    _states: set[str] = set()

    @router.get("/view")
    async def view():
        # Public page (declared in manifest public_paths); fetches its data from the
        # GATED /api/plugins/linear/* routes with the console-supplied bearer.
        return HTMLResponse(PAGE)

    @router.get("/oauth/start")
    async def oauth_start():
        if not identity.configured():
            return JSONResponse({"detail": "OAuth not configured (set linear.ava_client_id/secret/redirect)."}, 400)
        import uuid
        state = uuid.uuid4().hex
        _states.add(state)
        return RedirectResponse(identity.authorize_url(state))

    @router.get("/oauth/callback")
    async def oauth_callback(code: str = "", state: str = ""):
        if not state or state not in _states:
            return HTMLResponse("<p>Invalid OAuth state.</p>", status_code=400)
        _states.discard(state)
        try:
            identity.exchange_code(code)
        except Exception as exc:  # noqa: BLE001
            log.exception("[linear] OAuth code exchange failed")
            return HTMLResponse(f"<p>OAuth failed: {exc}</p>", status_code=400)
        return HTMLResponse("<p>Ava is now authorized on Linear. You can close this tab.</p>")

    @router.post("/webhook")
    async def webhook(request: Request):
        raw = await request.body()
        sig = request.headers.get("linear-signature", "")
        if not verify_signature(webhook_secret, raw, sig):
            return JSONResponse({"detail": "bad signature"}, status_code=401)
        import json as _json
        try:
            payload = _json.loads(raw)
        except ValueError:
            return JSONResponse({"detail": "bad json"}, status_code=400)
        ev = classify(payload)
        if not ev.actionable:
            return {"ok": True, "handled": False}
        # Dispatch off the request path so Linear gets a fast 200.
        import asyncio
        asyncio.create_task(asyncio.to_thread(bridge.handle, ev, client, identity, activity))
        return {"ok": True, "handled": True, "kind": ev.kind, "action": ev.action}

    return router
