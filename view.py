"""Console view for the Linear plugin — a status + actions dashboard.

Served as a sandboxed iframe page at /plugins/linear/view (public, declared in the
manifest's public_paths so it loads under a token gate). It links the host's design-
system plugin-kit so it's themed from the operator's live `--pl-*` tokens, and uses
the kit's `apiFetch` (bearer + slug-aware base) to read the GATED
/api/plugins/linear/* routes. Vanilla JS, no host build (ADR 0038).
"""

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Linear</title>
<script>
  window.__base = location.pathname.split("/plugins/")[0];
  document.write('<link rel="stylesheet" href="' + window.__base + '/_ds/plugin-kit.css">');
</script>
<style>
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--pl-color-bg-raised);color:var(--pl-color-fg);
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif);font-size:13px}
  .wrap{max-width:760px;margin:0 auto;padding:20px}
  h1{font-size:17px;margin:0 0 2px} .sub{color:var(--pl-color-fg-muted);margin:0 0 18px;font-size:12px}
  .pl-card{margin-bottom:14px}
  h2{font-size:11px;color:var(--pl-color-fg-muted);margin:0 0 10px;text-transform:uppercase;letter-spacing:.05em}
  .row{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--pl-color-border)}
  .row:last-child{border-bottom:none} .k{color:var(--pl-color-fg-muted)}
  .issue{padding:8px 0;border-bottom:1px solid var(--pl-color-border)} .issue:last-child{border-bottom:none}
  .id{color:var(--pl-color-accent);font-weight:var(--pl-font-weight-semibold,600);margin-right:8px}
  .st{color:var(--pl-color-fg-muted);font-size:12px} .empty{color:var(--pl-color-fg-muted);padding:8px 0}
  .err{color:var(--pl-color-status-error);font-size:12px}
  a.pl-btn{text-decoration:none} a.pl-btn[aria-disabled="true"]{opacity:.5;pointer-events:none}
</style></head><body>
<div class="wrap">
  <h1>Linear</h1>
  <p class="sub">Ava's Linear surface — tools, agent identity, inbound handling.</p>
  <div class="pl-card" id="status"><div class="empty">Loading status…</div></div>
  <div class="pl-card">
    <h2>Agent identity (post as Ava)</h2>
    <a id="authBtn" class="pl-btn pl-btn--primary pl-btn--sm" href="#" target="_blank" rel="noopener" aria-disabled="true">Authorize Ava on Linear</a>
    <p class="st" id="authHint" style="margin:8px 0 0">Set the OAuth app config first (linear.ava_client_id / secret / redirect).</p>
  </div>
  <div class="pl-card"><h2>Recent issues assigned to the API-key user</h2><div id="issues"><div class="empty">—</div></div></div>
</div>
<script type="module">
  "use strict";
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) { kit = { initPluginView(cb){ cb && cb(); }, apiFetch:(p,i)=>fetch(window.__base+p,i) }; }
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s==null?"":s).replace(/[&<>"]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c]));
  const badge = (ok, yes, no) => '<span class="pl-badge pl-badge--'+(ok?'success':'error')+'">'+esc(ok?yes:no)+'</span>';
  const row = (k, v) => '<div class="row"><span class="k">'+esc(k)+'</span>'+v+'</div>';

  async function load(){
    try{
      const r = await kit.apiFetch("/api/plugins/linear/status");
      if(!r.ok){ $("status").innerHTML='<div class="err">Status '+r.status+'</div>'; return; }
      const s = await r.json();
      $("status").innerHTML =
        row("API key (tools)", badge(s.api_key,"configured","missing")) +
        row("OAuth app", badge(s.oauth_configured,"configured","not set")) +
        row("Ava authorized", s.oauth_configured ? badge(s.oauth_authorized,"yes","no") : '<span class="st">n/a</span>') +
        row("Session poller", '<span class="pl-badge pl-badge--'+(s.poller_active?'success':'warning')+'">'+(s.poller_active?'running':'idle')+'</span>');
      const ab=$("authBtn"), ah=$("authHint");
      if(s.oauth_configured){ ab.removeAttribute("aria-disabled"); ab.href=window.__base+"/plugins/linear/oauth/start";
        ah.textContent = s.oauth_authorized ? "Authorized — re-run to refresh consent." : "Click to grant Ava actor=app access."; }
      loadIssues(s.api_key);
    }catch(e){ $("status").innerHTML='<div class="err">'+esc(e)+'</div>'; }
  }
  async function loadIssues(haveKey){
    const box=$("issues");
    if(!haveKey){ box.innerHTML='<div class="empty">Set the API key to list issues.</div>'; return; }
    try{
      const d = await kit.apiFetch("/api/plugins/linear/issues").then(r=>r.json());
      if(!d.issues || !d.issues.length){ box.innerHTML='<div class="empty">No issues.</div>'; return; }
      box.innerHTML = d.issues.map(i => '<div class="issue"><span class="id">'+esc(i.identifier)+'</span>'+esc(i.title)+' <span class="st">· '+esc(i.state)+'</span></div>').join("");
    }catch(e){ box.innerHTML='<div class="err">'+esc(e)+'</div>'; }
  }
  kit.initPluginView(load);
  load();
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
