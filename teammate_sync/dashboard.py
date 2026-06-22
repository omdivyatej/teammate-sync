"""
teammate-sync dashboard — localhost web view of connection + share state.

Launched by `teammate-sync dashboard`. Spins up a small Python http.server
on a random port, opens the user's browser. The HTML page polls a JSON
endpoint on the same server every 3s. That JSON endpoint, in turn, calls
the cloud backend's /v1/dashboard endpoint using the local auth.json.

Architecture:

    browser ─── http://127.0.0.1:<port>/index.html
                http://127.0.0.1:<port>/data.json   (poll every 3s)
                http://127.0.0.1:<port>/accept?peer=X
                http://127.0.0.1:<port>/decline?peer=X
                http://127.0.0.1:<port>/dump?teammate=X&session=Y

    local server ─── reads ~/.teammate-sync/auth.json
                      ─── calls cloud backend (Bearer auth)
                      ─── returns JSON or proxied dump bytes

No remote hosting needed. Auth never leaves this machine. The page can be
viewed only from this machine (binds to 127.0.0.1, not 0.0.0.0).
"""
from __future__ import annotations

import http.server
import json
import socket
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path


def _backend():
    """Construct an authenticated HTTPBackend for the caller. Raises if no auth."""
    from .auth import read_auth
    from .backend import HTTPBackend
    import httpx
    auth = read_auth()
    r = httpx.get(
        f"{auth['backend_url'].rstrip('/')}/v1/me",
        headers={"Authorization": f"Bearer {auth['token']}"},
        timeout=10.0,
    )
    if r.status_code != 200:
        raise ValueError(
            f"Cloud backend rejected token (/v1/me → {r.status_code}). "
            f"Re-run `teammate-sync init` to refresh."
        )
    return HTTPBackend(
        backend_url=auth["backend_url"],
        token=auth["token"],
        org=auth["org"],
        teammate=r.json()["github_handle"],
    )


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>teammate-sync — dashboard</title>
<style>
  :root {
    --bg: #0d0d0d;
    --bg-elev: #161616;
    --border: #2a2a2a;
    --text: #e8e8e8;
    --text-dim: #a0a0a0;
    --text-muted: #6f6f6f;
    --accent: #7dd3fc;
    --warn: #fbbf24;
    --good: #86efac;
    --bad: #ff7a7a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 1.5rem;
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
    color: var(--text);
    background: var(--bg);
  }
  h1 { font-size: 1.1rem; margin: 0; font-weight: 600; letter-spacing: -0.01em; }
  h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em;
       color: var(--text-muted); margin: 1.5rem 0 0.75rem 0; font-weight: 600; }
  .header { display: flex; align-items: baseline; gap: 0.75rem;
            padding-bottom: 1rem; border-bottom: 1px solid var(--border); }
  .header .me { color: var(--accent); font-family: monospace; }
  .header .org { color: var(--text-dim); }
  .header .freshness { margin-left: auto; font-size: 0.85rem; color: var(--text-muted); }
  .col-wrap { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem;
              margin-top: 1rem; }
  @media (max-width: 900px) { .col-wrap { grid-template-columns: 1fr; } }
  .col h2 { margin-top: 0; }
  .card {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.85rem 1rem;
    margin-bottom: 0.6rem;
  }
  .card .id { font-family: ui-monospace, SFMono-Regular, monospace;
              font-size: 0.85rem; color: var(--text-dim); }
  .card .id .live { color: var(--good); margin-left: 0.5rem; font-size: 0.75rem;
                    text-transform: uppercase; letter-spacing: 0.05em; }
  .card .meta { font-size: 0.85rem; color: var(--text-muted); margin-top: 0.35rem; }
  .card .meta strong { color: var(--text-dim); font-weight: 500; }
  .card .recipients { margin-top: 0.5rem; font-size: 0.85rem; }
  .pill { display: inline-block; padding: 0.1rem 0.5rem; margin: 0.1rem 0.2rem 0.1rem 0;
          background: #1f2937; border: 1px solid #374151; border-radius: 999px;
          color: var(--accent); font-family: monospace; font-size: 0.8rem; }
  .pill.warn { color: var(--warn); border-color: #92400e; background: #1c1917; }
  .pill.bad { color: var(--bad); border-color: #7f1d1d; background: #1c1010; }
  button {
    background: #1f2937; color: var(--accent); border: 1px solid #374151;
    border-radius: 4px; padding: 0.25rem 0.6rem; font-family: inherit;
    font-size: 0.8rem; cursor: pointer; margin-right: 0.3rem;
  }
  button:hover { background: #2a3645; }
  button.danger { color: var(--bad); border-color: #7f1d1d; }
  button.danger:hover { background: #2a1010; }
  details { margin-top: 0.5rem; }
  details summary { cursor: pointer; color: var(--text-dim); font-size: 0.85rem; }
  details pre {
    margin-top: 0.5rem; padding: 0.6rem; background: #0a0a0a;
    border: 1px solid var(--border); border-radius: 4px; overflow: auto;
    max-height: 400px; font-size: 0.8rem; line-height: 1.5;
    white-space: pre-wrap; color: var(--text-dim);
  }
  .empty { color: var(--text-muted); font-style: italic; padding: 0.6rem 0; }
  .err { color: var(--bad); padding: 1rem; border: 1px solid #7f1d1d; border-radius: 6px;
         background: #1c1010; margin-top: 1rem; }
  .invite-bar { background: #1c1917; border: 1px solid #92400e; border-radius: 6px;
                padding: 0.75rem 1rem; margin: 1rem 0; }
  .invite-bar h3 { font-size: 0.9rem; margin: 0 0 0.5rem 0; color: var(--warn); }
</style>
</head>
<body>

<div class="header">
  <h1>teammate-sync</h1>
  <span class="me" id="me-handle">...</span>
  <span class="org" id="org-name"></span>
  <span class="freshness" id="freshness">connecting…</span>
</div>

<div id="root">
  <div class="empty">Loading…</div>
</div>

<script>
const FRESH_MS = 3000;
const $ = (id) => document.getElementById(id);

let lastData = null;

async function poll() {
  try {
    const r = await fetch('/data.json', { cache: 'no-store' });
    if (!r.ok) throw new Error(await r.text() || `HTTP ${r.status}`);
    const data = await r.json();
    lastData = data;
    render(data);
    $('freshness').textContent = `synced ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    document.getElementById('root').innerHTML =
      `<div class="err">Error contacting backend: ${escapeHtml(e.message)}</div>`;
    $('freshness').textContent = `disconnected`;
  }
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

function renderSessionCard(s, isMine) {
  const recipients = s.recipients || [];
  const recipientsHtml = recipients.length
    ? recipients.map(r => `<span class="pill">${escapeHtml(r)}</span>`).join('')
    : `<span class="pill bad">no recipients — not actually shared</span>`;
  const sharedAt = s.shared_at
    ? new Date(s.shared_at * 1000).toLocaleString()
    : '';
  const dumpBtn = !isMine
    ? `<button onclick="dumpSession('${encodeURIComponent(s.owner_handle || '')}','${encodeURIComponent(s.session_id)}',this)">dump</button>`
    : '';
  return `
    <div class="card">
      <div class="id">${escapeHtml(s.session_id)}</div>
      <div class="meta">${isMine ? 'shared with: ' : 'from teammate · '}${recipientsHtml}</div>
      ${sharedAt ? `<div class="meta"><strong>at</strong> ${escapeHtml(sharedAt)}</div>` : ''}
      ${dumpBtn ? `<div style="margin-top:0.5rem;">${dumpBtn}</div>` : ''}
    </div>`;
}

async function dumpSession(handle, sid, btn) {
  btn.disabled = true;
  btn.textContent = 'loading…';
  try {
    const r = await fetch(`/dump?teammate=${handle}&session=${sid}`);
    const text = await r.text();
    const card = btn.closest('.card');
    let det = card.querySelector('details');
    if (!det) {
      det = document.createElement('details');
      det.open = true;
      det.innerHTML = `<summary>raw dump</summary><pre></pre>`;
      card.appendChild(det);
    }
    det.querySelector('pre').textContent = text;
    btn.textContent = 'dump';
    btn.disabled = false;
  } catch (e) {
    btn.textContent = 'error';
  }
}

async function postAction(path, peer) {
  try {
    const r = await fetch(path, { method: 'POST', headers: {'Content-Type':'application/json'},
                                   body: JSON.stringify({ peer }) });
    if (!r.ok) throw new Error(await r.text());
    await poll();
  } catch (e) {
    alert('Action failed: ' + e.message);
  }
}

function render(data) {
  $('me-handle').textContent = '@' + (data.me || '?');
  $('org-name').textContent = data.org ? `in ${data.org}` : '';

  const conn = data.connections || {};
  const pendingIn = conn.pending_incoming || [];
  const pendingOut = conn.pending_outgoing || [];

  let inviteBar = '';
  if (pendingIn.length) {
    inviteBar = `
      <div class="invite-bar">
        <h3>${pendingIn.length} pending connection request${pendingIn.length>1?'s':''}</h3>
        ${pendingIn.map(p => `
          <div style="margin: 0.4rem 0;">
            <strong>${escapeHtml(p.peer_handle)}</strong> wants to connect.
            <button onclick="postAction('/accept', '${encodeURIComponent(p.peer_handle)}')">accept</button>
            <button class="danger" onclick="postAction('/decline', '${encodeURIComponent(p.peer_handle)}')">decline</button>
          </div>
        `).join('')}
      </div>`;
  }

  let outBar = '';
  if (pendingOut.length) {
    outBar = `
      <div class="invite-bar" style="border-color: #3a3a3a; background: #161616;">
        <h3 style="color: var(--accent);">${pendingOut.length} request${pendingOut.length>1?'s':''} sent — awaiting their /connect</h3>
        ${pendingOut.map(p => `
          <div style="margin: 0.4rem 0;">
            → <strong>${escapeHtml(p.peer_handle)}</strong> · waiting for them to /connect you back.
            <button class="danger" onclick="postAction('/disconnect', '${encodeURIComponent(p.peer_handle)}')">cancel</button>
          </div>
        `).join('')}
      </div>`;
  }

  const mySessions = (data.my_sessions || []);
  const mySessionsHtml = mySessions.length
    ? mySessions.map(s => renderSessionCard(s, true)).join('')
    : `<div class="empty">No sessions you've shared yet. Run <span class="pill">/share &lt;handle&gt;</span> in any Claude Code session.</div>`;

  const teammates = (data.teammates || []);
  let teammatesHtml = '';
  if (!teammates.length) {
    teammatesHtml = `<div class="empty">No teammates have shared sessions with you yet.</div>`;
  } else {
    teammatesHtml = teammates.map(t => {
      const sessions = t.sessions || [];
      const sessionCards = sessions.map(s => renderSessionCard({...s, owner_handle: t.handle}, false)).join('');
      return `
        <div style="margin-bottom: 1.5rem;">
          <h2 style="margin: 0 0 0.5rem 0;">@${escapeHtml(t.handle)}
            <button class="danger" onclick="postAction('/disconnect', '${encodeURIComponent(t.handle)}')">disconnect</button>
          </h2>
          ${sessionCards}
        </div>`;
    }).join('');
  }

  let acceptedHtml = '';
  const accepted = conn.accepted || [];
  if (accepted.length) {
    acceptedHtml = `<div style="margin-top:0.5rem;">${accepted.map(c => `<span class="pill">${escapeHtml(c.peer_handle)}</span>`).join('')}</div>`;
  } else {
    acceptedHtml = `<div class="empty">No accepted connections yet.</div>`;
  }

  document.getElementById('root').innerHTML = `
    ${inviteBar}
    ${outBar}
    <h2>Accepted connections</h2>
    ${acceptedHtml}
    <div class="col-wrap">
      <div class="col">
        <h2>Your shared sessions</h2>
        ${mySessionsHtml}
      </div>
      <div class="col">
        <h2>From teammates</h2>
        ${teammatesHtml}
      </div>
    </div>`;
}

poll();
setInterval(poll, FRESH_MS);
</script>
</body>
</html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    # Re-bound per-request via the launcher's closure.
    backend = None  # type: ignore[assignment]
    org = None  # type: ignore[assignment]

    def log_message(self, *args, **kwargs):  # silence the default per-request log line
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict | list):
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _read_json_body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, _INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/data.json":
                snap = self.backend.dashboard()
                snap["org"] = self.org
                self._send_json(200, snap)
                return
            if path == "/dump":
                teammate = (query.get("teammate") or [""])[0]
                session = (query.get("session") or [""])[0]
                if not teammate or not session:
                    self._send_json(400, {"error": "teammate and session required"})
                    return
                raw = self.backend.dump(teammate, session)
                body = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode()
                self._send(200, body, "text/plain; charset=utf-8")
                return
            self._send_json(404, {"error": f"unknown path {path}"})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()
        peer = body.get("peer", "").strip()
        try:
            if path == "/accept":
                if not peer:
                    self._send_json(400, {"error": "peer required"})
                    return
                res = self.backend.accept_connection(peer)
                self._send_json(200, res)
                return
            if path == "/decline":
                if not peer:
                    self._send_json(400, {"error": "peer required"})
                    return
                res = self.backend.decline_connection(peer)
                self._send_json(200, res)
                return
            if path == "/disconnect":
                if not peer:
                    self._send_json(400, {"error": "peer required"})
                    return
                res = self.backend.disconnect_connection(peer)
                self._send_json(200, res)
                return
            self._send_json(404, {"error": f"unknown path {path}"})
        except Exception as e:
            self._send_json(500, {"error": str(e)})


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_dashboard(port: int | None = None, open_browser: bool = True) -> int:
    """
    Launch the localhost dashboard. Blocks until Ctrl+C.
    """
    try:
        backend = _backend()
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    handler_cls = _Handler
    handler_cls.backend = backend
    handler_cls.org = backend.org

    if port is None:
        port = _pick_free_port()
    url = f"http://127.0.0.1:{port}/"

    with socketserver.ThreadingTCPServer(("127.0.0.1", port), handler_cls) as server:
        print(f"[dashboard] serving at {url}")
        print(f"[dashboard] press Ctrl+C to stop.")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[dashboard] stopped.")
    return 0
