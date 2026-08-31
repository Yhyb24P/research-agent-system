"""Static assets for the loopback Browser Control Tower.

The document is deliberately unauthenticated and contains no state or secret.
``research browser`` passes the existing local credential in a URL fragment;
fragments are not sent in HTTP requests and the script immediately removes it.
All reads and typed mutations continue to use the normal Bearer boundary.
"""

BROWSER_INDEX = """<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Research Control Tower</title><link rel=\"stylesheet\" href=\"/ui/app.css\"></head>
<body><header><h1>Research Control Tower</h1><p id=\"status\">Connecting to the local control plane…</p><button id=\"refresh\">Refresh</button></header>
<main><section><h2>Runs</h2><pre id=\"runs\"></pre></section><section><h2>Agents</h2><pre id=\"agents\"></pre></section><section><h2>Approvals</h2><pre id=\"approvals\"></pre></section><section><h2>Handoffs</h2><pre id=\"handoffs\"></pre></section>
<section class=\"wide\"><h2>Typed controls</h2><p>These controls submit the same authenticated command DTOs as the daily client. They never write browser state to the controller database.</p><label>Run ID <input id=\"run-id\"></label><button data-command=\"cancel-run\">Cancel run</button><label>Remote runtime ID <input id=\"runtime-id\"></label><button data-command=\"attach\">Attach</button><button data-command=\"renew\">Renew</button><button data-command=\"detach\">Detach</button><pre id=\"result\"></pre></section></main><script src=\"/ui/app.js\"></script></body></html>"""

BROWSER_CSS = """*{box-sizing:border-box}body{margin:0;background:#10151c;color:#e8edf2;font:15px system-ui,sans-serif}header{padding:1.2rem 2rem;border-bottom:1px solid #34414f}h1{margin:0}p{color:#b8c5d1}button,input{margin:.25rem;padding:.45rem;border-radius:4px;border:1px solid #526273}button{background:#246b9b;color:white;cursor:pointer}input{background:#18212a;color:#e8edf2}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:1rem;padding:1rem 2rem}.wide{grid-column:1/-1}section{background:#17202a;padding:1rem;border:1px solid #34414f;border-radius:6px}h2{margin-top:0}pre{white-space:pre-wrap;overflow-wrap:anywhere;min-height:2rem;color:#c8d9e8}"""

BROWSER_JS = """(() => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get('token');
  history.replaceState(null, '', location.pathname);
  const status = document.querySelector('#status');
  const result = document.querySelector('#result');
  if (!token) { status.textContent = 'No local credential was supplied. Start this page with research browser.'; return; }
  const commandId = () => `cmd_${crypto.randomUUID()}`;
  async function request(path, options = {}) {
    const response = await fetch(path, { ...options, credentials: 'omit', headers: { Authorization: `Bearer ${token}`, ...(options.headers || {}) } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }
  function show(id, value) { document.querySelector(id).textContent = JSON.stringify(value, null, 2); }
  async function refresh() {
    status.textContent = 'Refreshing…';
    try {
      const [runs, agents, approvals, handoffs] = await Promise.all(['/api/runs','/api/agents','/api/approvals','/api/handoffs'].map(path => request(path)));
      show('#runs', runs); show('#agents', agents); show('#approvals', approvals); show('#handoffs', handoffs);
      status.textContent = 'Connected to the authenticated local control plane.';
    } catch (error) { status.textContent = `Control plane unavailable: ${error.message}`; }
  }
  document.querySelector('#refresh').addEventListener('click', refresh);
  document.querySelectorAll('[data-command]').forEach(button => button.addEventListener('click', async () => {
    const action = button.dataset.command;
    const runId = document.querySelector('#run-id').value.trim();
    const runtimeId = document.querySelector('#runtime-id').value.trim();
    let path, payload;
    if (action === 'cancel-run') { if (!runId) { result.textContent = 'Run ID is required.'; return; } path = `/api/runs/${encodeURIComponent(runId)}/cancel`; payload = { command_id: commandId() }; }
    else { if (!runtimeId) { result.textContent = 'Remote runtime ID is required.'; return; } path = `/api/remote-agents/${action}`; payload = { command_id: commandId(), runtime_id: runtimeId }; }
    try { show('#result', await request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })); await refresh(); }
    catch (error) { result.textContent = `Command rejected: ${error.message}`; }
  }));
  refresh();
})();"""

BROWSER_ASSETS = {
    "/ui": ("text/html; charset=utf-8", BROWSER_INDEX),
    "/ui/": ("text/html; charset=utf-8", BROWSER_INDEX),
    "/ui/app.css": ("text/css; charset=utf-8", BROWSER_CSS),
    "/ui/app.js": ("application/javascript; charset=utf-8", BROWSER_JS),
}

__all__ = ["BROWSER_ASSETS"]
