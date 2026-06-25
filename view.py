"""Console view for the Linear plugin — a status + actions dashboard.

Served as a sandboxed iframe page at /plugins/linear/view (public, so the browser
can load it under a token gate — declared in the manifest's public_paths). It pulls
its data from the GATED /api/plugins/linear/* routes using the operator bearer the
console hands it via the postMessage bridge.
"""

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Linear</title>
<style>
  :root{--bg:#0a0f14;--fg:#e6e6e6;--muted:#9aa0aa;--card:#11161c;--line:#1f2630;--accent:#9b87f2}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
    font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;font-size:14px}
  .wrap{max-width:720px;margin:0 auto;padding:24px}
  h1{font-size:18px;margin:0 0 2px} .sub{color:var(--muted);margin:0 0 20px;font-size:13px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
  .row{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--line)}
  .row:last-child{border-bottom:none}
  .k{color:var(--muted)} .badge{font-weight:600}
  .ok{color:#46c46a} .no{color:#e5687a} .warn{color:#e0b34a}
  a.btn{display:inline-block;background:var(--accent);color:#0a0f14;text-decoration:none;font-weight:600;
    padding:9px 14px;border-radius:8px;margin-top:4px}
  a.btn.disabled{background:#2a2f3a;color:#6b7280;pointer-events:none}
  .issue{padding:8px 0;border-bottom:1px solid var(--line)}
  .issue:last-child{border-bottom:none} .id{color:var(--accent);font-weight:600;margin-right:8px}
  .st{color:var(--muted);font-size:12px} .empty{color:var(--muted);padding:8px 0}
  .err{color:#e5687a;font-size:13px}
</style></head><body><div class="wrap">
  <h1>Linear</h1>
  <p class="sub">Ava's Linear surface — tools, agent identity, and inbound handling.</p>

  <div class="card" id="status"><div class="empty">Loading status…</div></div>

  <div class="card">
    <div class="k" style="margin-bottom:8px">Agent identity (post as Ava)</div>
    <a id="authBtn" class="btn disabled" href="#" target="_blank" rel="noopener">Authorize Ava on Linear</a>
    <p class="st" id="authHint" style="margin:8px 0 0">Set the OAuth app config first (linear.ava_client_id / secret / redirect).</p>
  </div>

  <div class="card">
    <div class="k" style="margin-bottom:8px">Recent issues assigned to the API-key user</div>
    <div id="issues"><div class="empty">—</div></div>
  </div>
</div>
<script>
  var BASE = location.pathname.replace(/\\/plugins\\/linear\\/view.*$/, "");  // fleet-proxy-safe base
  var TOKEN = "";
  function authed(){ return TOKEN ? {Authorization:"Bearer "+TOKEN} : {}; }
  function badge(ok, yes, no){ return '<span class="badge '+(ok?'ok':'no')+'">'+(ok?yes:no)+'</span>'; }

  async function load(){
    try{
      var r = await fetch(BASE+"/api/plugins/linear/status", {headers: authed()});
      if(!r.ok){ document.getElementById("status").innerHTML='<div class="err">Status '+r.status+' — open the console authed.</div>'; return; }
      var s = await r.json();
      document.getElementById("status").innerHTML =
        row("API key (tools)", badge(s.api_key, "configured", "missing")) +
        row("OAuth app", badge(s.oauth_configured, "configured", "not set")) +
        row("Ava authorized", s.oauth_configured ? badge(s.oauth_authorized, "yes", "no — authorize below") : '<span class="st">n/a</span>') +
        row("Session poller", '<span class="badge '+(s.poller_active?"ok":"warn")+'">'+(s.poller_active?"running":"idle")+'</span>');
      var ab = document.getElementById("authBtn"), ah = document.getElementById("authHint");
      if(s.oauth_configured){ ab.classList.remove("disabled"); ab.href = BASE+"/plugins/linear/oauth/start";
        ah.textContent = s.oauth_authorized ? "Authorized — re-run to refresh consent." : "Click to grant Ava actor=app access."; }
      loadIssues(s.api_key);
    }catch(e){ document.getElementById("status").innerHTML='<div class="err">'+e+'</div>'; }
  }
  function row(k,v){ return '<div class="row"><span class="k">'+k+'</span>'+v+'</div>'; }

  async function loadIssues(haveKey){
    var box = document.getElementById("issues");
    if(!haveKey){ box.innerHTML='<div class="empty">Set the API key to list issues.</div>'; return; }
    try{
      var r = await fetch(BASE+"/api/plugins/linear/issues", {headers: authed()});
      var d = await r.json();
      if(!d.issues || !d.issues.length){ box.innerHTML='<div class="empty">No issues.</div>'; return; }
      box.innerHTML = d.issues.map(function(i){
        return '<div class="issue"><span class="id">'+i.identifier+'</span>'+esc(i.title)+
               ' <span class="st">· '+esc(i.state)+'</span></div>'; }).join("");
    }catch(e){ box.innerHTML='<div class="err">'+e+'</div>'; }
  }
  function esc(s){ return (s||"").replace(/[&<>]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c];}); }

  function applyTheme(t){ if(!t)return; if(t.bg)document.body.style.background=t.bg; if(t.fg)document.body.style.color=t.fg; }
  window.addEventListener("message", function(e){
    var m = e.data||{};
    if(m.type==="protoagent:init"){ TOKEN=m.token||""; applyTheme(m.theme); load(); }
    else if(m.type==="protoagent:theme"){ applyTheme(m.theme); }
  });
  // If the bridge never fires (standalone open), still try unauthenticated.
  setTimeout(function(){ if(!TOKEN) load(); }, 800);
</script></body></html>"""


def build_data_router(client, identity):
    """Gated data routes (mounted under /api/plugins/linear) feeding the view."""
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/status")
    async def status() -> dict:
        api_key = client.configured()
        return {
            "api_key": api_key,
            "oauth_configured": identity.configured(),
            "oauth_authorized": identity.authorized(),
            # The poller runs exactly when it can respond as the agent.
            "poller_active": bool(api_key and identity.authorized()),
        }

    @router.get("/issues")
    async def issues() -> dict:
        if not client.configured():
            return {"issues": []}
        try:
            return {"issues": client.list_issues(assignee="me", max=10)}
        except Exception as exc:  # noqa: BLE001 — surface as empty + error, never 500 the view
            return {"issues": [], "error": str(exc)}

    return router
