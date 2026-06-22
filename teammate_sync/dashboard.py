"""
teammate-sync dashboard — cross-platform desktop UI for managing
connections, sessions, daemon, and settings (v0.6).

Architecture: a small localhost HTTP server renders the dashboard HTML
(single-page app with sidebar nav + 5 panels). The page polls JSON
endpoints on the same server, which proxy to the cloud backend (using
the user's local auth.json) or read local state (daemon.pid, log file,
notification prefs, etc.).

By default the dashboard opens in a native window via `pywebview` —
looks like a real desktop app (uses platform-native webview: WKWebView
on Mac, WebView2 on Windows, WebKitGTK on Linux). Falling back to the
default browser if pywebview is unavailable.

Endpoints (all 127.0.0.1, no remote exposure):

  GET  /                  the SPA HTML
  GET  /data.json         dashboard snapshot (proxy to backend)
  GET  /dump?teammate=X&session=Y     raw session bytes (proxy)
  GET  /logs?lines=N      tail of ~/.teammate-sync/state/daemon.log
  GET  /settings          local prefs + auth handle + autostart state
  POST /accept            body: {peer}
  POST /decline           body: {peer}
  POST /disconnect        body: {peer}
  POST /daemon/start      runs `teammate-sync up`
  POST /daemon/stop       runs `teammate-sync down`
  POST /settings/notifications   body: {enabled}
  POST /settings/autostart       body: {enabled}
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path


STATE_DIR = Path("~/.teammate-sync/state").expanduser()
LOG_FILE = STATE_DIR / "daemon.log"
NOTIFY_PREFS_FILE = STATE_DIR / ".notify-prefs.json"
LAUNCHAGENT_PATH = Path("~/Library/LaunchAgents/com.teammate-sync.app.plist").expanduser()


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


def _resolve_self_binary() -> str:
    """Find the teammate-sync binary for subprocess calls."""
    found = shutil.which("teammate-sync")
    if found:
        return found
    # Fallback to sys.argv[0] if launched from script context
    cand = Path(sys.argv[0]).resolve()
    if cand.exists() and cand.name == "teammate-sync":
        return str(cand)
    return "teammate-sync"  # last resort, hope it's on PATH


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _read_daemon_pid() -> int | None:
    p = STATE_DIR / "daemon.pid"
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def _daemon_status() -> dict:
    pid = _read_daemon_pid()
    alive = pid is not None and _pid_alive(pid)
    return {"alive": alive, "pid": pid if alive else None}


def _tail_log(n: int = 200) -> str:
    if not LOG_FILE.exists():
        return ""
    try:
        # Read last n lines efficiently for typical log sizes
        text = LOG_FILE.read_text(errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-n:])
    except OSError as e:
        return f"[error reading log: {e}]"


def _read_notify_prefs() -> dict:
    if not NOTIFY_PREFS_FILE.exists():
        return {"enabled": True}
    try:
        return json.loads(NOTIFY_PREFS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"enabled": True}


def _write_notify_prefs(prefs: dict) -> None:
    NOTIFY_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTIFY_PREFS_FILE.write_text(json.dumps(prefs, indent=2))


def _autostart_installed() -> bool:
    # Mac LaunchAgent; on Linux/Windows we'd return based on whatever
    # equivalent file lives in the standard autostart location. For now
    # only Mac is implemented.
    return sys.platform == "darwin" and LAUNCHAGENT_PATH.exists()


def _install_autostart() -> bool:
    """Install platform-appropriate auto-start. Returns True on success."""
    if sys.platform == "darwin":
        try:
            from . import macapp
            return macapp.install_launchagent_only() == 0
        except Exception:
            return False
    # TODO: Linux .desktop autostart, Windows registry Run entry
    return False


def _uninstall_autostart() -> bool:
    if sys.platform == "darwin":
        try:
            from . import macapp
            return macapp.uninstall_launchagent_only() == 0
        except Exception:
            return False
    return False


# ─── Embedded SPA ──────────────────────────────────────────────────────────

_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>teammate-sync</title>
<style>
  :root {
    --bg: #0e0e10;
    --bg-elev: #18181b;
    --bg-elev-2: #1f1f23;
    --border: #2a2a2e;
    --border-strong: #3a3a3f;
    --text: #ececee;
    --text-dim: #a6a6ac;
    --text-muted: #6e6e74;
    --accent: #7dd3fc;
    --accent-strong: #38bdf8;
    --good: #86efac;
    --warn: #fbbf24;
    --bad: #fca5a5;
    --bad-strong: #ef4444;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; overflow: hidden; }
  body {
    font: 13.5px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui,
          "Inter", "Segoe UI", Roboto, sans-serif;
    color: var(--text);
    background: var(--bg);
    display: grid;
    grid-template-columns: 220px 1fr;
    grid-template-rows: 100vh;
  }

  /* SIDEBAR */
  .sidebar {
    background: var(--bg-elev);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    padding: 1rem 0;
  }
  .brand {
    padding: 0 1.25rem 1.25rem 1.25rem;
    border-bottom: 1px solid var(--border);
    margin-bottom: 0.5rem;
  }
  .brand .logo {
    font-size: 1.05rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    color: var(--text);
  }
  .brand .sub {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-top: 0.25rem;
  }
  .nav {
    flex: 1;
    overflow-y: auto;
    padding: 0.25rem 0.5rem;
  }
  .nav-item {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.55rem 0.75rem;
    margin: 0.1rem 0;
    border-radius: 6px;
    font-size: 0.9rem;
    color: var(--text-dim);
    cursor: pointer;
    user-select: none;
  }
  .nav-item:hover { background: var(--bg-elev-2); color: var(--text); }
  .nav-item.active {
    background: var(--bg-elev-2);
    color: var(--text);
    box-shadow: inset 2px 0 0 var(--accent);
  }
  .nav-item .badge {
    margin-left: auto;
    font-size: 0.7rem;
    color: var(--text-muted);
    background: var(--bg);
    border: 1px solid var(--border);
    padding: 1px 6px;
    border-radius: 999px;
  }
  .sidebar-foot {
    padding: 0.75rem 1.25rem;
    border-top: 1px solid var(--border);
    font-size: 0.75rem;
    color: var(--text-muted);
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot.good { background: var(--good); }
  .dot.bad { background: var(--bad-strong); }
  .dot.idle { background: var(--text-muted); }

  /* MAIN AREA */
  main {
    overflow-y: auto;
    padding: 0;
    display: flex;
    flex-direction: column;
  }
  .topbar {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 0.9rem 1.75rem;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
    position: sticky;
    top: 0;
    z-index: 10;
  }
  .topbar .handle {
    font-family: ui-monospace, SFMono-Regular, monospace;
    color: var(--accent);
    font-size: 0.9rem;
  }
  .topbar .workspace {
    color: var(--text-muted);
    font-size: 0.85rem;
  }
  .topbar .freshness {
    margin-left: auto;
    color: var(--text-muted);
    font-size: 0.8rem;
  }
  .panel {
    padding: 1.5rem 1.75rem;
    flex: 1;
  }
  .panel-title {
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--text-muted);
    margin: 0 0 0.85rem 0;
  }
  .section-title {
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: var(--text-muted);
    margin: 1.75rem 0 0.6rem 0;
  }
  .section-title:first-child { margin-top: 0; }

  /* CARDS */
  .card {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem 1.15rem;
    margin-bottom: 0.6rem;
  }
  .card .row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }
  .card .grow { flex: 1; min-width: 0; }
  .card .id {
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 0.8rem;
    color: var(--text-dim);
    word-break: break-all;
  }
  .card .meta {
    font-size: 0.82rem;
    color: var(--text-muted);
    margin-top: 0.25rem;
  }
  .card .strong { color: var(--text); font-weight: 500; }

  /* STAT GRID */
  .stat-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 0.6rem;
    margin-bottom: 1.5rem;
  }
  .stat {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.85rem 1rem;
  }
  .stat-num {
    font-size: 1.4rem;
    font-weight: 600;
    color: var(--text);
    line-height: 1.2;
  }
  .stat-label {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
    margin-top: 0.3rem;
  }

  /* PILLS + BUTTONS */
  .pill {
    display: inline-block;
    padding: 1px 8px;
    font-size: 0.78rem;
    font-family: ui-monospace, monospace;
    background: var(--bg-elev-2);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--accent);
    margin: 1px 3px 1px 0;
  }
  .pill.warn { color: var(--warn); border-color: #92400e; background: #1c1917; }
  .pill.bad { color: var(--bad-strong); border-color: #7f1d1d; background: #1c1010; }
  .pill.good { color: var(--good); border-color: #14532d; background: #0a1f12; }
  button, .btn {
    background: var(--bg-elev-2);
    color: var(--accent);
    border: 1px solid var(--border-strong);
    border-radius: 5px;
    padding: 0.35rem 0.75rem;
    font: inherit;
    font-size: 0.85rem;
    cursor: pointer;
    margin-right: 0.3rem;
  }
  button:hover, .btn:hover { background: var(--border); color: var(--text); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  button.primary {
    background: var(--accent-strong);
    color: #0c0c0e;
    border-color: var(--accent-strong);
    font-weight: 500;
  }
  button.primary:hover { background: var(--accent); }
  button.danger {
    color: var(--bad);
    border-color: #7f1d1d;
  }
  button.danger:hover { background: #1c1010; color: var(--bad-strong); }
  button.ghost {
    background: transparent;
    color: var(--text-muted);
    border-color: var(--border);
  }

  /* LOGS */
  pre.logs {
    background: #08080a;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1rem 1.1rem;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 0.78rem;
    line-height: 1.55;
    color: var(--text-dim);
    overflow: auto;
    max-height: calc(100vh - 220px);
    white-space: pre-wrap;
    word-break: break-all;
    margin: 0;
  }

  /* DETAILS (dump previews) */
  details {
    margin-top: 0.6rem;
    background: #08080a;
    border-radius: 6px;
    border: 1px solid var(--border);
  }
  details summary {
    cursor: pointer;
    padding: 0.5rem 0.85rem;
    color: var(--text-muted);
    font-size: 0.82rem;
    user-select: none;
  }
  details pre {
    margin: 0;
    padding: 0.7rem 0.9rem;
    border-top: 1px solid var(--border);
    font-family: ui-monospace, monospace;
    font-size: 0.76rem;
    color: var(--text-dim);
    max-height: 400px;
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* SETTINGS */
  .setting-row {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 0.85rem 0;
    border-bottom: 1px solid var(--border);
  }
  .setting-row:last-child { border-bottom: none; }
  .setting-row .label {
    flex: 1;
  }
  .setting-row .label .name { font-size: 0.92rem; color: var(--text); }
  .setting-row .label .desc { font-size: 0.78rem; color: var(--text-muted); margin-top: 0.2rem; }

  /* EMPTY STATES */
  .empty {
    color: var(--text-muted);
    font-style: italic;
    padding: 0.85rem 1rem;
    background: var(--bg-elev);
    border: 1px dashed var(--border);
    border-radius: 6px;
    font-size: 0.85rem;
  }

  .err {
    color: var(--bad-strong);
    padding: 1rem;
    border: 1px solid #7f1d1d;
    border-radius: 6px;
    background: #1c1010;
  }
  .toolbar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.85rem;
  }
  .toolbar .grow { flex: 1; }
  input[type="checkbox"] {
    accent-color: var(--accent-strong);
    transform: scale(1.15);
    margin: 0;
  }
</style>
</head>
<body>

<div class="sidebar">
  <div class="brand">
    <div class="logo">teammate-sync</div>
    <div class="sub" id="brand-version">v0.6</div>
  </div>
  <div class="nav">
    <div class="nav-item active" data-panel="status">Status</div>
    <div class="nav-item" data-panel="connections">
      Connections
      <span class="badge" id="badge-connections">0</span>
    </div>
    <div class="nav-item" data-panel="sessions">Sessions</div>
    <div class="nav-item" data-panel="logs">Logs</div>
    <div class="nav-item" data-panel="settings">Settings</div>
  </div>
  <div class="sidebar-foot">
    <span class="dot idle" id="foot-dot"></span>
    <span id="foot-text">connecting…</span>
  </div>
</div>

<main>
  <div class="topbar">
    <span class="handle" id="top-handle">@…</span>
    <span class="workspace" id="top-workspace"></span>
    <span class="freshness" id="top-freshness"></span>
  </div>

  <!-- STATUS -->
  <div class="panel" id="panel-status">
    <h2 class="panel-title">Status</h2>
    <div class="stat-grid" id="stat-grid"></div>
    <div class="card" id="daemon-card">
      <div class="row">
        <div class="grow">
          <div class="strong" id="daemon-state">…</div>
          <div class="meta" id="daemon-meta"></div>
        </div>
        <button class="primary" id="btn-start" onclick="postAction('/daemon/start')">Start daemon</button>
        <button class="danger" id="btn-stop" onclick="postAction('/daemon/stop')">Stop daemon</button>
      </div>
    </div>
    <div class="section-title">Recent activity</div>
    <pre class="logs" id="status-logs" style="max-height: 280px;"></pre>
  </div>

  <!-- CONNECTIONS -->
  <div class="panel" id="panel-connections" style="display:none">
    <h2 class="panel-title">Connections</h2>
    <div class="section-title">Accepted</div>
    <div id="conn-accepted"></div>
    <div class="section-title">Requests you sent — awaiting their /connect</div>
    <div id="conn-out"></div>
    <div class="section-title">Requests to accept</div>
    <div id="conn-in"></div>
  </div>

  <!-- SESSIONS -->
  <div class="panel" id="panel-sessions" style="display:none">
    <h2 class="panel-title">Sessions</h2>
    <div class="section-title">Your shared sessions</div>
    <div id="my-sessions"></div>
    <div class="section-title">Sessions shared with you</div>
    <div id="their-sessions"></div>
  </div>

  <!-- LOGS -->
  <div class="panel" id="panel-logs" style="display:none">
    <h2 class="panel-title">Daemon log</h2>
    <div class="toolbar">
      <label style="display:flex; align-items:center; gap:0.4rem; color: var(--text-dim); font-size:0.85rem;">
        <input type="checkbox" id="logs-autorefresh" checked> auto-refresh
      </label>
      <span class="grow"></span>
      <button class="ghost" onclick="refreshLogs()">refresh now</button>
    </div>
    <pre class="logs" id="logs-output"></pre>
  </div>

  <!-- SETTINGS -->
  <div class="panel" id="panel-settings" style="display:none">
    <h2 class="panel-title">Settings</h2>

    <div class="section-title">Account</div>
    <div class="card">
      <div class="row">
        <div class="grow">
          <div class="strong" id="settings-handle">@…</div>
          <div class="meta" id="settings-workspace"></div>
        </div>
      </div>
    </div>

    <div class="section-title">Preferences</div>
    <div class="card" style="padding: 0 1.15rem;">
      <div class="setting-row">
        <div class="label">
          <div class="name">Notifications</div>
          <div class="desc">macOS / Linux / Windows toast when a teammate sends a /connect request or accepts your invite.</div>
        </div>
        <input type="checkbox" id="settings-notifications" onchange="toggleNotifications(this.checked)">
      </div>
      <div class="setting-row">
        <div class="label">
          <div class="name">Auto-start at login</div>
          <div class="desc">Launch the menu bar app automatically when you log in.</div>
        </div>
        <input type="checkbox" id="settings-autostart" onchange="toggleAutostart(this.checked)">
      </div>
    </div>
  </div>
</main>

<script>
//
// State
//
let activePanel = 'status';
let logsAutoRefreshTimer = null;

//
// Utilities
//
const $ = id => document.getElementById(id);
const esc = s => (s == null ? '' : String(s).replace(/[&<>"']/g, c => ({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
}[c])));

function timeAgo(epoch) {
  if (!epoch) return '';
  const sec = (Date.now() / 1000) - epoch;
  if (sec < 60) return `${Math.floor(sec)}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

async function fetchJson(url, opts = {}) {
  const r = await fetch(url, {cache: 'no-store', ...opts});
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

//
// Panel switching
//
function setPanel(name) {
  activePanel = name;
  ['status', 'connections', 'sessions', 'logs', 'settings'].forEach(p => {
    $('panel-' + p).style.display = (p === name) ? '' : 'none';
  });
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.panel === name);
  });
  if (name === 'logs') refreshLogs();
  if (name === 'settings') loadSettings();
}
document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', () => setPanel(el.dataset.panel));
});

//
// Top-level poll for /data.json (dashboard snapshot)
//
let lastData = null;
async function pollData() {
  try {
    const data = await fetchJson('/data.json');
    lastData = data;
    renderTopBar(data);
    renderStatus(data);
    renderConnections(data);
    renderSessions(data);
    $('foot-dot').className = 'dot good';
    $('foot-text').textContent = 'synced ' + new Date().toLocaleTimeString();
  } catch (e) {
    $('foot-dot').className = 'dot bad';
    $('foot-text').textContent = 'disconnected';
    $('panel-status').innerHTML =
      `<div class="err">Could not reach backend: ${esc(e.message)}</div>`;
  }
}

function renderTopBar(data) {
  $('top-handle').textContent = '@' + (data.me || '?');
  $('top-workspace').textContent = data.org ? `in ${data.org}` : '';
  $('top-freshness').textContent = 'synced ' + new Date().toLocaleTimeString();
  $('settings-handle').textContent = '@' + (data.me || '?');
  $('settings-workspace').textContent = data.org ? `workspace: ${data.org}` : '';
}

//
// STATUS panel
//
function renderStatus(data) {
  const conn = data.connections || {};
  const accepted = conn.accepted || [];
  const pendingIn = conn.pending_incoming || [];
  const pendingOut = conn.pending_outgoing || [];
  const mySessions = data.my_sessions || [];
  const theirSessions = (data.teammates || []).flatMap(t => t.sessions || []);

  const daemon = data.daemon || {};

  $('stat-grid').innerHTML = `
    <div class="stat">
      <div class="stat-num">${accepted.length}</div>
      <div class="stat-label">Accepted connections</div>
    </div>
    <div class="stat">
      <div class="stat-num">${pendingIn.length + pendingOut.length}</div>
      <div class="stat-label">Pending invites</div>
    </div>
    <div class="stat">
      <div class="stat-num">${mySessions.length}</div>
      <div class="stat-label">Your shared sessions</div>
    </div>
    <div class="stat">
      <div class="stat-num">${theirSessions.length}</div>
      <div class="stat-label">Visible from teammates</div>
    </div>
  `;

  // badge on Connections nav item
  $('badge-connections').textContent =
    accepted.length + pendingIn.length + pendingOut.length;

  // Daemon card
  if (daemon.alive) {
    $('daemon-state').innerHTML = `<span class="pill good">● running</span> &nbsp; pid ${esc(daemon.pid)}`;
    $('daemon-meta').textContent = '';
    $('btn-start').disabled = true;
    $('btn-stop').disabled = false;
  } else {
    $('daemon-state').innerHTML = `<span class="pill bad">● stopped</span>`;
    $('daemon-meta').textContent = 'Start the daemon to begin sharing and receiving teammate context.';
    $('btn-start').disabled = false;
    $('btn-stop').disabled = true;
  }
}

//
// CONNECTIONS panel
//
function renderConnections(data) {
  const conn = data.connections || {};
  $('conn-accepted').innerHTML = (conn.accepted || []).length
    ? (conn.accepted || []).map(c => `
        <div class="card">
          <div class="row">
            <div class="grow">
              <div class="strong">@${esc(c.peer_handle)}</div>
              <div class="meta">${c.i_initiated ? 'you invited them' : 'they invited you'}${c.decided_at ? ' · accepted ' + timeAgo(c.decided_at) : ''}</div>
            </div>
            <button class="danger" onclick="postAction('/disconnect', {peer: '${esc(c.peer_handle)}'})">Disconnect</button>
          </div>
        </div>`).join('')
    : `<div class="empty">No accepted connections yet. In a Claude Code session run <code>/connect &lt;github-handle&gt;</code> to start.</div>`;

  $('conn-out').innerHTML = (conn.pending_outgoing || []).length
    ? (conn.pending_outgoing || []).map(c => `
        <div class="card">
          <div class="row">
            <div class="grow">
              <div class="strong">→ @${esc(c.peer_handle)}</div>
              <div class="meta">awaiting their <code>/connect</code> back</div>
            </div>
            <button class="danger" onclick="postAction('/disconnect', {peer: '${esc(c.peer_handle)}'})">Cancel</button>
          </div>
        </div>`).join('')
    : `<div class="empty">No outgoing invites right now.</div>`;

  $('conn-in').innerHTML = (conn.pending_incoming || []).length
    ? (conn.pending_incoming || []).map(c => `
        <div class="card">
          <div class="row">
            <div class="grow">
              <div class="strong">← @${esc(c.peer_handle)} wants to connect</div>
              <div class="meta">requested ${timeAgo(c.requested_at)}</div>
            </div>
            <button class="primary" onclick="postAction('/accept', {peer: '${esc(c.peer_handle)}'})">Accept</button>
            <button class="danger" onclick="postAction('/decline', {peer: '${esc(c.peer_handle)}'})">Decline</button>
          </div>
        </div>`).join('')
    : `<div class="empty">No incoming invites.</div>`;
}

//
// SESSIONS panel
//
function renderSessions(data) {
  const mine = data.my_sessions || [];
  $('my-sessions').innerHTML = mine.length
    ? mine.map(s => `
        <div class="card">
          <div class="id">${esc(s.session_id)}</div>
          <div class="meta">
            shared with: ${(s.recipients || []).length
              ? (s.recipients || []).map(r => `<span class="pill">@${esc(r)}</span>`).join('')
              : '<span class="pill bad">no recipients — not actually shared</span>'}
          </div>
          ${s.shared_at ? `<div class="meta">shared ${timeAgo(s.shared_at)}</div>` : ''}
        </div>`).join('')
    : `<div class="empty">No sessions you've shared. Type <code>/connect &lt;handle&gt;</code> inside a Claude Code session.</div>`;

  const teammates = data.teammates || [];
  $('their-sessions').innerHTML = teammates.length
    ? teammates.map(t => `
        <div style="margin-bottom: 1rem;">
          <div class="strong" style="font-size: 0.95rem; margin-bottom: 0.35rem;">@${esc(t.handle)}</div>
          ${(t.sessions || []).map(s => `
            <div class="card" id="session-${esc(s.session_id)}">
              <div class="id">${esc(s.session_id)}</div>
              <div class="meta">shared ${timeAgo(s.shared_at)}</div>
              <div style="margin-top: 0.55rem;">
                <button onclick="dumpSession('${esc(t.handle)}', '${esc(s.session_id)}', this)">View raw content</button>
              </div>
            </div>
          `).join('')}
        </div>`).join('')
    : `<div class="empty">No sessions shared with you yet. Connected teammates need to <code>/connect &lt;your-handle&gt;</code> in their Claude Code session.</div>`;
}

async function dumpSession(handle, sid, btn) {
  btn.disabled = true;
  btn.textContent = 'loading…';
  try {
    const r = await fetch(`/dump?teammate=${encodeURIComponent(handle)}&session=${encodeURIComponent(sid)}`);
    const text = await r.text();
    const card = btn.closest('.card');
    let det = card.querySelector('details');
    if (!det) {
      det = document.createElement('details');
      det.open = true;
      det.innerHTML = `<summary>raw content</summary><pre></pre>`;
      card.appendChild(det);
    }
    det.querySelector('pre').textContent = text;
    btn.textContent = 'View raw content';
    btn.disabled = false;
  } catch (e) {
    btn.textContent = 'error: ' + e.message;
  }
}

//
// LOGS panel
//
async function refreshLogs() {
  try {
    const r = await fetch('/logs?lines=300');
    const data = await r.json();
    $('logs-output').textContent = data.text || '(empty)';
    const out = $('logs-output');
    out.scrollTop = out.scrollHeight;
  } catch (e) {
    $('logs-output').textContent = 'Error: ' + e.message;
  }
}

function ensureLogsTimer() {
  const enabled = $('logs-autorefresh').checked;
  if (logsAutoRefreshTimer) {
    clearInterval(logsAutoRefreshTimer);
    logsAutoRefreshTimer = null;
  }
  if (enabled && activePanel === 'logs') {
    logsAutoRefreshTimer = setInterval(refreshLogs, 3000);
  }
}
$('logs-autorefresh').addEventListener('change', ensureLogsTimer);

//
// SETTINGS panel
//
async function loadSettings() {
  try {
    const s = await fetchJson('/settings');
    $('settings-notifications').checked = !!s.notifications_enabled;
    $('settings-autostart').checked = !!s.autostart_installed;
  } catch (e) {
    // ignore — surface elsewhere if needed
  }
}

async function toggleNotifications(enabled) {
  await fetch('/settings/notifications', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled}),
  });
}

async function toggleAutostart(enabled) {
  const cb = $('settings-autostart');
  cb.disabled = true;
  try {
    await fetch('/settings/autostart', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    await loadSettings();
  } catch (e) {
    alert('Failed: ' + e.message);
  }
  cb.disabled = false;
}

//
// POST actions (accept / decline / disconnect / daemon start / daemon stop)
//
async function postAction(path, body = {}) {
  try {
    const r = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text() || `HTTP ${r.status}`);
    await pollData();
  } catch (e) {
    alert('Failed: ' + e.message);
  }
}

//
// Periodically refresh status logs in the Status panel
//
async function pollStatusLogs() {
  if (activePanel !== 'status') return;
  try {
    const r = await fetch('/logs?lines=30');
    const data = await r.json();
    $('status-logs').textContent = data.text || '(daemon not running)';
    $('status-logs').scrollTop = $('status-logs').scrollHeight;
  } catch (e) {}
}

// Boot
pollData();
pollStatusLogs();
setInterval(pollData, 3000);
setInterval(pollStatusLogs, 5000);
setPanel('status');
</script>
</body>
</html>
"""


# ─── HTTP server ───────────────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    backend = None  # bound in run_dashboard before serve_forever
    org = None

    def log_message(self, *args, **kwargs):
        return  # silence

    def _send(self, status: int, body: bytes, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload):
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
                snap["daemon"] = _daemon_status()
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
            if path == "/logs":
                n = int((query.get("lines") or ["200"])[0])
                self._send_json(200, {"text": _tail_log(n)})
                return
            if path == "/settings":
                prefs = _read_notify_prefs()
                self._send_json(200, {
                    "notifications_enabled": bool(prefs.get("enabled", True)),
                    "autostart_installed": _autostart_installed(),
                    "autostart_supported": sys.platform == "darwin",
                })
                return
            self._send_json(404, {"error": f"unknown path {path}"})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()
        try:
            if path == "/accept":
                peer = (body.get("peer") or "").strip()
                if not peer:
                    self._send_json(400, {"error": "peer required"})
                    return
                self._send_json(200, self.backend.accept_connection(peer))
                return
            if path == "/decline":
                peer = (body.get("peer") or "").strip()
                if not peer:
                    self._send_json(400, {"error": "peer required"})
                    return
                self._send_json(200, self.backend.decline_connection(peer))
                return
            if path == "/disconnect":
                peer = (body.get("peer") or "").strip()
                if not peer:
                    self._send_json(400, {"error": "peer required"})
                    return
                self._send_json(200, self.backend.disconnect_connection(peer))
                return
            if path == "/daemon/start":
                rc = subprocess.run(
                    [_resolve_self_binary(), "up"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                ).returncode
                self._send_json(200, {"ok": rc == 0, "rc": rc})
                return
            if path == "/daemon/stop":
                rc = subprocess.run(
                    [_resolve_self_binary(), "down"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).returncode
                self._send_json(200, {"ok": rc == 0, "rc": rc})
                return
            if path == "/settings/notifications":
                enabled = bool(body.get("enabled", True))
                _write_notify_prefs({"enabled": enabled})
                self._send_json(200, {"ok": True})
                return
            if path == "/settings/autostart":
                enabled = bool(body.get("enabled", False))
                ok = _install_autostart() if enabled else _uninstall_autostart()
                self._send_json(200, {"ok": ok})
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


def _start_http_server_in_thread(backend, port: int) -> "socketserver.ThreadingTCPServer":
    handler_cls = _Handler
    handler_cls.backend = backend
    handler_cls.org = backend.org
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_dashboard(
    port: int | None = None,
    open_browser: bool = True,
    use_window: bool | None = None,
) -> int:
    """
    Launch the dashboard.

    By default tries to open in a native window via pywebview (looks like a
    real desktop app). Falls back to the system browser if pywebview is
    unavailable or use_window=False is forced.

    Set use_window=False to force browser mode.
    """
    try:
        backend = _backend()
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if port is None:
        port = _pick_free_port()
    url = f"http://127.0.0.1:{port}/"
    server = _start_http_server_in_thread(backend, port)
    print(f"[dashboard] serving at {url}")

    # Try the native window first; gracefully degrade to browser
    if use_window is None:
        try:
            import webview  # noqa
            use_window = True
        except ImportError:
            use_window = False

    if use_window:
        try:
            import webview
            window = webview.create_window(
                "teammate-sync",
                url,
                width=1100,
                height=720,
                min_size=(880, 560),
                background_color="#0e0e10",
            )
            # webview.start() blocks until window closed
            webview.start()
        except Exception as e:
            print(f"[dashboard] native window failed ({e}); opening browser instead")
            if open_browser:
                webbrowser.open(url)
            try:
                # Browser mode: keep serving until Ctrl-C
                while True:
                    import time
                    time.sleep(60)
            except KeyboardInterrupt:
                pass
    else:
        if open_browser:
            webbrowser.open(url)
        print(f"[dashboard] press Ctrl-C to stop.")
        try:
            while True:
                import time
                time.sleep(60)
        except KeyboardInterrupt:
            pass

    server.shutdown()
    print("[dashboard] stopped.")
    return 0
