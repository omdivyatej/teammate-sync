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
    """Find the teammate-sync binary for subprocess calls.

    Honors $TEAMMATE_SYNC_BIN (set by the Electron desktop app to a shim
    that execs the bundled Python) before falling back to PATH lookup.
    """
    env_bin = os.environ.get("TEAMMATE_SYNC_BIN")
    if env_bin:
        return env_bin
    found = shutil.which("teammate-sync")
    if found:
        return found
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
<title>CodeBaton</title>
<style>
  :root {
    /* warm-tinted neutrals — tinted toward the ember brand hue, never pure gray */
    --bg:         #14110d;
    --bg-elev:    #1c1813;
    --bg-elev-2:  #241f18;
    --bg-sink:    #0d0b08;
    --border:     #2c261d;
    --border-2:   #3a3225;
    --text:       #f2ece2;
    --text-dim:   #b6ab9b;
    --text-muted: #7e7466;
    /* ember brand */
    --ember:      #ff8d3e;
    --ember-2:    #ffb15a;
    --ember-deep: #d8451c;
    --ember-soft: rgba(255,141,62,0.12);
    --ember-line: rgba(255,141,62,0.30);
    --danger:     #f0795f;
    --danger-deep:#7f2418;
    --radius:     11px;
    --radius-sm:  7px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; overflow: hidden; }
  body {
    font: 13.5px/1.55 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, "Segoe UI", sans-serif;
    color: var(--text);
    background: var(--bg);
    display: grid;
    grid-template-columns: 232px 1fr;
    grid-template-rows: 100vh;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }
  ::selection { background: var(--ember-soft); }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: #2c261d; border-radius: 6px; border: 2px solid var(--bg); }
  ::-webkit-scrollbar-thumb:hover { background: #3a3225; }

  /* ── SIDEBAR ───────────────────────────────────────────── */
  .sidebar {
    background: linear-gradient(180deg, #1a1610 0%, #14110d 100%);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    -webkit-app-region: drag;            /* draggable window chrome */
    padding-top: env(titlebar-area-height, 28px);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 18px 16px;
  }
  .brand .mark { width: 26px; height: 26px; flex: none; }
  .brand .name {
    font-size: 15px; font-weight: 650; letter-spacing: -0.015em; color: var(--text);
  }
  .brand .name b { color: var(--ember); font-weight: 650; }
  .nav { flex: 1; padding: 6px 10px; -webkit-app-region: no-drag; }
  .nav-item {
    display: flex; align-items: center; gap: 11px;
    padding: 8px 11px; margin: 2px 0;
    border-radius: var(--radius-sm);
    font-size: 13.5px; color: var(--text-dim);
    cursor: pointer; user-select: none;
    position: relative;
    transition: background .14s ease, color .14s ease;
  }
  .nav-item:hover { background: var(--bg-elev); color: var(--text); }
  .nav-item.active { background: var(--bg-elev-2); color: var(--text); }
  .nav-item.active::before {
    content: ""; position: absolute; left: -10px; top: 7px; bottom: 7px;
    width: 3px; border-radius: 3px;
    background: linear-gradient(180deg, var(--ember-2), var(--ember-deep));
  }
  .nav-item .ico { width: 16px; height: 16px; flex: none; opacity: .85; }
  .nav-item .count {
    margin-left: auto; font-size: 11px; font-variant-numeric: tabular-nums;
    color: var(--text-muted); background: var(--bg-sink);
    border: 1px solid var(--border); padding: 0 7px; border-radius: 999px; line-height: 18px;
  }
  .nav-item.active .count { color: var(--ember); border-color: var(--ember-line); }
  .sidefoot {
    -webkit-app-region: no-drag;
    padding: 11px 18px; border-top: 1px solid var(--border);
    display: flex; align-items: center; gap: 9px;
    font-size: 12px; color: var(--text-muted);
  }
  .live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); flex: none; }
  .live-dot.on  { background: var(--ember); box-shadow: 0 0 0 0 var(--ember-line); animation: pulse 2.4s infinite; }
  .live-dot.off { background: #4a4136; }
  .live-dot.err { background: var(--danger); }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(255,141,62,0.35); }
    70%  { box-shadow: 0 0 0 7px rgba(255,141,62,0); }
    100% { box-shadow: 0 0 0 0 rgba(255,141,62,0); }
  }

  /* ── MAIN ──────────────────────────────────────────────── */
  main { overflow: hidden; display: flex; flex-direction: column; }
  .topbar {
    -webkit-app-region: drag;
    display: flex; align-items: baseline; gap: 12px;
    padding: 0 26px; height: calc(48px + env(titlebar-area-height, 0px));
    padding-top: env(titlebar-area-height, 0px);
    border-bottom: 1px solid var(--border);
    background: var(--bg);
  }
  .topbar .handle { font-weight: 600; font-size: 14px; letter-spacing: -0.01em; }
  .topbar .ws { color: var(--text-muted); font-size: 12.5px; }
  .topbar .spacer { flex: 1; }
  .topbar .fresh { color: var(--text-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
  .panel { flex: 1; overflow-y: auto; padding: 26px; animation: rise .4s cubic-bezier(.2,.7,.2,1) both; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .ptitle { font-size: 19px; font-weight: 640; letter-spacing: -0.02em; margin: 0 0 2px; }
  .psub { color: var(--text-muted); font-size: 13px; margin: 0 0 22px; }
  .label { font-size: 11px; font-weight: 650; text-transform: uppercase; letter-spacing: 0.09em;
           color: var(--text-muted); margin: 26px 0 11px; }
  .label:first-of-type { margin-top: 4px; }

  /* ── STATUS hero stats ─────────────────────────────────── */
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 8px; }
  @media (max-width: 760px) { .stats { grid-template-columns: repeat(2, 1fr); } }
  .stat {
    background: linear-gradient(180deg, var(--bg-elev) 0%, #181410 100%);
    border: 1px solid var(--border); border-radius: var(--radius);
    padding: 15px 16px;
  }
  .stat .n { font-size: 28px; font-weight: 660; letter-spacing: -0.03em; line-height: 1;
             font-variant-numeric: tabular-nums; }
  .stat.accent .n { color: var(--ember); }
  .stat .l { font-size: 11.5px; color: var(--text-muted); margin-top: 9px;
             text-transform: uppercase; letter-spacing: 0.06em; }

  /* ── daemon control bar ────────────────────────────────── */
  .daemon {
    display: flex; align-items: center; gap: 14px;
    background: var(--bg-elev); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 15px 17px; margin-top: 14px;
  }
  .daemon .state { display: flex; align-items: center; gap: 10px; }
  .daemon .state .txt { font-weight: 560; }
  .daemon .meta { color: var(--text-muted); font-size: 12.5px; margin-top: 2px; }
  .daemon .grow { flex: 1; }

  /* ── cards ─────────────────────────────────────────────── */
  .card {
    background: var(--bg-elev); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 13px 15px; margin-bottom: 9px;
    transition: border-color .14s ease;
  }
  .card:hover { border-color: var(--border-2); }
  .crow { display: flex; align-items: center; gap: 12px; }
  .crow .grow { flex: 1; min-width: 0; }
  .avatar {
    width: 32px; height: 32px; border-radius: 9px; flex: none;
    display: grid; place-items: center; font-weight: 640; font-size: 13px;
    color: var(--ember-2);
    background: var(--ember-soft); border: 1px solid var(--ember-line);
  }
  .who { font-weight: 560; font-size: 13.5px; }
  .sub { color: var(--text-muted); font-size: 12.5px; margin-top: 2px; }
  .mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", monospace;
          font-size: 12px; color: var(--text-dim); word-break: break-all; }

  /* ── pills ─────────────────────────────────────────────── */
  .pill { display: inline-flex; align-items: center; gap: 5px;
          padding: 2px 9px; font-size: 12px; border-radius: 999px;
          background: var(--bg-elev-2); border: 1px solid var(--border);
          color: var(--ember-2); margin: 2px 4px 2px 0; }
  .pill.ember { color: var(--ember); border-color: var(--ember-line); background: var(--ember-soft); }
  .pill.muted { color: var(--text-muted); }
  .pill.bad { color: var(--danger); border-color: var(--danger-deep); background: rgba(240,121,95,0.08); }
  .pill .d { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }

  /* ── buttons ───────────────────────────────────────────── */
  button {
    font: inherit; font-size: 12.5px; font-weight: 520;
    padding: 6px 13px; border-radius: var(--radius-sm); cursor: pointer;
    background: var(--bg-elev-2); color: var(--text-dim);
    border: 1px solid var(--border-2);
    transition: background .13s, color .13s, border-color .13s, transform .06s;
  }
  button:hover { background: #2c261d; color: var(--text); }
  button:active { transform: translateY(1px); }
  button:disabled { opacity: .38; cursor: not-allowed; }
  button.primary {
    background: linear-gradient(180deg, var(--ember) 0%, var(--ember-deep) 100%);
    color: #1a0f07; border: none; font-weight: 600;
  }
  button.primary:hover { filter: brightness(1.07); background: linear-gradient(180deg, var(--ember-2), var(--ember)); }
  button.danger { color: var(--danger); border-color: var(--danger-deep); }
  button.danger:hover { background: rgba(240,121,95,0.1); color: #ff9883; }
  button.ghost { background: transparent; border-color: var(--border); }

  /* ── logs ──────────────────────────────────────────────── */
  pre.logs {
    background: var(--bg-sink); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 14px 16px; margin: 0;
    font-family: ui-monospace, SFMono-Regular, "SF Mono", monospace;
    font-size: 12px; line-height: 1.65; color: var(--text-dim);
    overflow: auto; white-space: pre-wrap; word-break: break-all;
  }
  .logs .ember { color: var(--ember); }

  /* ── details (raw dump) ────────────────────────────────── */
  details { margin-top: 10px; background: var(--bg-sink); border: 1px solid var(--border); border-radius: var(--radius-sm); }
  details summary { cursor: pointer; padding: 8px 13px; color: var(--text-muted); font-size: 12.5px; list-style: none; }
  details summary::-webkit-details-marker { display: none; }
  details summary::before { content: "▸ "; color: var(--ember); }
  details[open] summary::before { content: "▾ "; }
  details pre { margin: 0; padding: 11px 13px; border-top: 1px solid var(--border);
                font-family: ui-monospace, monospace; font-size: 11.5px; color: var(--text-dim);
                max-height: 420px; overflow: auto; white-space: pre-wrap; word-break: break-word; }

  /* ── settings rows ─────────────────────────────────────── */
  .srow { display: flex; align-items: center; gap: 16px; padding: 15px 0; border-bottom: 1px solid var(--border); }
  .srow:last-child { border-bottom: none; }
  .srow .grow { flex: 1; }
  .srow .nm { font-size: 13.5px; }
  .srow .ds { font-size: 12.5px; color: var(--text-muted); margin-top: 3px; max-width: 52ch; }

  /* iOS-style toggle */
  .toggle { position: relative; width: 42px; height: 25px; flex: none; }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle .track { position: absolute; inset: 0; border-radius: 999px;
                   background: #322b21; border: 1px solid var(--border-2); transition: .18s; }
  .toggle .track::after { content: ""; position: absolute; left: 3px; top: 3px;
                   width: 17px; height: 17px; border-radius: 50%; background: #b6ab9b; transition: .18s cubic-bezier(.3,.8,.3,1); }
  .toggle input:checked + .track { background: linear-gradient(180deg, var(--ember), var(--ember-deep)); border-color: transparent; }
  .toggle input:checked + .track::after { transform: translateX(17px); background: #1a0f07; }

  /* ── empty states ──────────────────────────────────────── */
  .empty { padding: 26px 20px; text-align: center; border: 1px dashed var(--border-2);
           border-radius: var(--radius); color: var(--text-muted); }
  .empty .em-mark { opacity: .5; margin-bottom: 12px; }
  .empty .em-t { color: var(--text-dim); font-size: 13.5px; }
  .empty .em-d { font-size: 12.5px; margin-top: 5px; }
  .empty code { background: var(--bg-sink); border: 1px solid var(--border);
                padding: 1px 6px; border-radius: 5px; color: var(--ember-2);
                font-family: ui-monospace, monospace; font-size: 12px; }

  .group-h { font-size: 13.5px; font-weight: 600; margin: 0 0 8px; display: flex; align-items: center; gap: 9px; }
  .err-box { color: var(--danger); padding: 16px; border: 1px solid var(--danger-deep);
             border-radius: var(--radius); background: rgba(240,121,95,0.06); }
  .hidden { display: none !important; }
</style>
</head>
<body>

<aside class="sidebar">
  <div class="brand">
    <svg class="mark" viewBox="0 0 44 44" fill="none" xmlns="http://www.w3.org/2000/svg">
      <line x1="13" y1="32" x2="23" y2="13" stroke="#d8451c" stroke-width="6.5" stroke-linecap="round"/>
      <line x1="23" y1="31" x2="33" y2="12" stroke="#ff9a4a" stroke-width="6.5" stroke-linecap="round"/>
      <circle cx="33" cy="12" r="3.4" fill="#ffe9d2"/>
    </svg>
    <div class="name">Code<b>Baton</b></div>
  </div>

  <nav class="nav" id="nav">
    <div class="nav-item active" data-panel="status">Overview</div>
    <div class="nav-item" data-panel="connections">Connections<span class="count" id="c-conn">0</span></div>
    <div class="nav-item" data-panel="sessions">Sessions</div>
    <div class="nav-item" data-panel="logs">Activity</div>
    <div class="nav-item" data-panel="settings">Settings</div>
  </nav>

  <div class="sidefoot">
    <span class="live-dot off" id="foot-dot"></span>
    <span id="foot-text">connecting…</span>
  </div>
</aside>

<main>
  <div class="topbar">
    <span class="handle" id="t-handle">@…</span>
    <span class="ws" id="t-ws"></span>
    <span class="spacer"></span>
    <span class="fresh" id="t-fresh"></span>
  </div>

  <!-- OVERVIEW -->
  <section class="panel" id="p-status">
    <h1 class="ptitle">Overview</h1>
    <p class="psub">Your live sharing state at a glance.</p>
    <div class="stats" id="stats"></div>
    <div class="daemon" id="daemon">
      <div class="state">
        <span class="live-dot off" id="d-dot"></span>
        <div>
          <div class="txt" id="d-txt">…</div>
          <div class="meta" id="d-meta"></div>
        </div>
      </div>
      <div class="grow"></div>
      <button class="primary" id="d-start" onclick="post('/daemon/start')">Start sync</button>
      <button class="danger" id="d-stop" onclick="post('/daemon/stop')">Stop</button>
    </div>
    <div class="label">Recent activity</div>
    <pre class="logs" id="s-logs" style="max-height: 240px;"></pre>
  </section>

  <!-- CONNECTIONS -->
  <section class="panel hidden" id="p-connections">
    <h1 class="ptitle">Connections</h1>
    <p class="psub">People whose context you can ask for — and who can ask for yours.</p>
    <div class="label">Connected</div>
    <div id="c-accepted"></div>
    <div class="label">Invites you sent</div>
    <div id="c-out"></div>
    <div class="label">Invites to you</div>
    <div id="c-in"></div>
  </section>

  <!-- SESSIONS -->
  <section class="panel hidden" id="p-sessions">
    <h1 class="ptitle">Sessions</h1>
    <p class="psub">Live Claude Code context flowing between you and your team.</p>
    <div class="label">You're sharing</div>
    <div id="s-mine"></div>
    <div class="label">Shared with you</div>
    <div id="s-theirs"></div>
  </section>

  <!-- ACTIVITY / LOGS -->
  <section class="panel hidden" id="p-logs">
    <h1 class="ptitle">Activity</h1>
    <p class="psub">The sync daemon's live log.</p>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:12px;">
      <label style="display:flex; align-items:center; gap:8px; color:var(--text-dim); font-size:12.5px; cursor:pointer;">
        <span class="toggle"><input type="checkbox" id="log-auto" checked><span class="track"></span></span>
        auto-refresh
      </label>
      <div style="flex:1"></div>
      <button class="ghost" onclick="refreshLogs()">refresh</button>
    </div>
    <pre class="logs" id="logs-out" style="max-height: calc(100vh - 230px);"></pre>
  </section>

  <!-- SETTINGS -->
  <section class="panel hidden" id="p-settings">
    <h1 class="ptitle">Settings</h1>
    <p class="psub">Account and app preferences.</p>
    <div class="label">Account</div>
    <div class="card">
      <div class="crow">
        <div class="avatar" id="set-av">?</div>
        <div class="grow">
          <div class="who" id="set-handle">@…</div>
          <div class="sub" id="set-ws"></div>
        </div>
      </div>
    </div>
    <div class="label">Preferences</div>
    <div class="srow">
      <div class="grow">
        <div class="nm">Notifications</div>
        <div class="ds">Desktop alert when a teammate wants to connect, or accepts your invite.</div>
      </div>
      <label class="toggle"><input type="checkbox" id="set-notif" onchange="toggleNotif(this.checked)"><span class="track"></span></label>
    </div>
    <div class="srow">
      <div class="grow">
        <div class="nm">Launch at login</div>
        <div class="ds">Start CodeBaton automatically when you log in, so sync is always on.</div>
      </div>
      <label class="toggle"><input type="checkbox" id="set-auto" onchange="toggleAuto(this.checked)"><span class="track"></span></label>
    </div>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);
const esc = s => (s==null?'':String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])));
const initial = h => (h||'?').replace(/[^a-z0-9]/ig,'').charAt(0).toUpperCase() || '?';
function ago(ep){ if(!ep) return ''; const s=(Date.now()/1000)-ep;
  if(s<60) return Math.max(0,Math.floor(s))+'s ago'; if(s<3600) return Math.floor(s/60)+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago'; return Math.floor(s/86400)+'d ago'; }
async function getJ(u,o){ const r=await fetch(u,{cache:'no-store',...o}); if(!r.ok) throw new Error(r.status+' '+await r.text()); return r.json(); }

const MARK_EMPTY = `<svg class="em-mark" width="40" height="40" viewBox="0 0 44 44" fill="none">
  <line x1="13" y1="32" x2="23" y2="13" stroke="#5a4a38" stroke-width="6.5" stroke-linecap="round"/>
  <line x1="23" y1="31" x2="33" y2="12" stroke="#6e5a40" stroke-width="6.5" stroke-linecap="round"/></svg>`;
const emptyBox = (t,d)=>`<div class="empty">${MARK_EMPTY}<div class="em-t">${t}</div><div class="em-d">${d}</div></div>`;

let active='status', logTimer=null, last=null;
function setPanel(n){
  active=n;
  ['status','connections','sessions','logs','settings'].forEach(p=>$('p-'+p).classList.toggle('hidden', p!==n));
  document.querySelectorAll('.nav-item').forEach(e=>e.classList.toggle('active', e.dataset.panel===n));
  if(n==='logs') refreshLogs();
  if(n==='settings') loadSettings();
}
document.querySelectorAll('.nav-item').forEach(e=>e.addEventListener('click',()=>setPanel(e.dataset.panel)));

async function poll(){
  try{
    const d = await getJ('/data.json'); last=d;
    top(d); status(d); connections(d); sessions(d);
    $('foot-dot').className='live-dot on'; $('foot-text').textContent='synced';
    $('t-fresh').textContent = 'updated '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  }catch(e){
    $('foot-dot').className='live-dot err'; $('foot-text').textContent='offline';
    $('p-status').innerHTML='<div class="err-box">Can\'t reach the backend: '+esc(e.message)+'</div>';
  }
}
function top(d){
  $('t-handle').textContent='@'+(d.me||'?'); $('t-ws').textContent=d.org?('· '+d.org):'';
  $('set-handle').textContent='@'+(d.me||'?'); $('set-ws').textContent=d.org?('workspace: '+d.org):'';
  $('set-av').textContent=initial(d.me);
}
function status(d){
  const c=d.connections||{}, acc=c.accepted||[], pin=c.pending_incoming||[], pout=c.pending_outgoing||[];
  const mine=d.my_sessions||[], theirs=(d.teammates||[]).flatMap(t=>t.sessions||[]);
  $('stats').innerHTML = `
    <div class="stat accent"><div class="n">${acc.length}</div><div class="l">Connected</div></div>
    <div class="stat"><div class="n">${pin.length+pout.length}</div><div class="l">Pending</div></div>
    <div class="stat"><div class="n">${mine.length}</div><div class="l">Sharing</div></div>
    <div class="stat"><div class="n">${theirs.length}</div><div class="l">Receiving</div></div>`;
  $('c-conn').textContent = acc.length+pin.length+pout.length;
  const dm=d.daemon||{};
  if(dm.alive){
    $('d-dot').className='live-dot on';
    $('d-txt').textContent='Sync is running';
    $('d-meta').textContent='pid '+dm.pid+' · context flows to your connections in real time';
    $('d-start').disabled=true; $('d-stop').disabled=false;
  } else {
    $('d-dot').className='live-dot off';
    $('d-txt').textContent='Sync is stopped';
    $('d-meta').textContent='Start it to share and receive teammate context.';
    $('d-start').disabled=false; $('d-stop').disabled=true;
  }
}
function connections(d){
  const c=d.connections||{};
  $('c-accepted').innerHTML=(c.accepted||[]).length ? (c.accepted||[]).map(x=>`
    <div class="card"><div class="crow">
      <div class="avatar">${initial(x.peer_handle)}</div>
      <div class="grow"><div class="who">@${esc(x.peer_handle)}</div>
        <div class="sub">${x.i_initiated?'you connected':'they connected'}${x.decided_at?' · '+ago(x.decided_at):''}</div></div>
      <button class="danger" onclick="post('/disconnect',{peer:'${esc(x.peer_handle)}'})">Disconnect</button>
    </div></div>`).join('') : emptyBox('No connections yet','Run <code>/connect &lt;handle&gt;</code> in a Claude Code session.');
  $('c-out').innerHTML=(c.pending_outgoing||[]).length ? (c.pending_outgoing||[]).map(x=>`
    <div class="card"><div class="crow">
      <div class="avatar">${initial(x.peer_handle)}</div>
      <div class="grow"><div class="who">@${esc(x.peer_handle)}</div>
        <div class="sub">waiting for them to <code style="font-family:ui-monospace">/connect</code> you back</div></div>
      <span class="pill muted"><span class="d"></span>pending</span>
      <button class="danger" onclick="post('/disconnect',{peer:'${esc(x.peer_handle)}'})">Cancel</button>
    </div></div>`).join('') : `<div class="sub" style="padding:4px 2px">None.</div>`;
  $('c-in').innerHTML=(c.pending_incoming||[]).length ? (c.pending_incoming||[]).map(x=>`
    <div class="card"><div class="crow">
      <div class="avatar">${initial(x.peer_handle)}</div>
      <div class="grow"><div class="who">@${esc(x.peer_handle)} wants to connect</div>
        <div class="sub">${ago(x.requested_at)}</div></div>
      <button class="primary" onclick="post('/accept',{peer:'${esc(x.peer_handle)}'})">Accept</button>
      <button class="danger" onclick="post('/decline',{peer:'${esc(x.peer_handle)}'})">Decline</button>
    </div></div>`).join('') : `<div class="sub" style="padding:4px 2px">None.</div>`;
}
function sessions(d){
  const mine=d.my_sessions||[];
  $('s-mine').innerHTML=mine.length ? mine.map(s=>`
    <div class="card"><div class="mono">${esc(s.session_id)}</div>
      <div class="sub" style="margin-top:7px">to ${(s.recipients||[]).length?(s.recipients||[]).map(r=>`<span class="pill ember">@${esc(r)}</span>`).join(''):'<span class="pill bad">no recipients</span>'}
        ${s.shared_at?'· '+ago(s.shared_at):''}</div></div>`).join('')
    : emptyBox("You're not sharing anything",'Type <code>/connect &lt;handle&gt;</code> in a Claude Code session to share it.');
  const tm=d.teammates||[];
  $('s-theirs').innerHTML=tm.length ? tm.map(t=>`
    <div style="margin-bottom:16px">
      <div class="group-h"><span class="avatar" style="width:24px;height:24px;border-radius:7px;font-size:11px">${initial(t.handle)}</span>@${esc(t.handle)}</div>
      ${(t.sessions||[]).map(s=>`<div class="card"><div class="mono">${esc(s.session_id)}</div>
        <div class="sub" style="margin-top:6px">shared ${ago(s.shared_at)}</div>
        <div style="margin-top:9px"><button onclick="dump('${esc(t.handle)}','${esc(s.session_id)}',this)">View raw context</button></div></div>`).join('')}
    </div>`).join('')
    : emptyBox('Nothing shared with you yet','A connected teammate needs to <code>/connect &lt;your-handle&gt;</code> in their session.');
}
async function dump(h,sid,btn){
  btn.disabled=true; btn.textContent='loading…';
  try{ const r=await fetch('/dump?teammate='+encodeURIComponent(h)+'&session='+encodeURIComponent(sid)); const t=await r.text();
    const card=btn.closest('.card'); let det=card.querySelector('details');
    if(!det){ det=document.createElement('details'); det.open=true; det.innerHTML='<summary>raw context</summary><pre></pre>'; card.appendChild(det); }
    det.querySelector('pre').textContent=t; btn.textContent='View raw context'; btn.disabled=false;
  }catch(e){ btn.textContent='error'; }
}
async function refreshLogs(){
  try{ const d=await getJ('/logs?lines=400'); const o=$('logs-out');
    o.innerHTML=(d.text||'(no activity yet — start sync)').split('\n').map(l=>
      l.replace(/^(\[sync\])/,'<span class="ember">$1</span>')).join('\n'); o.scrollTop=o.scrollHeight;
  }catch(e){ $('logs-out').textContent='Error: '+e.message; }
}
$('log-auto').addEventListener('change',()=>{ if(logTimer){clearInterval(logTimer);logTimer=null;}
  if($('log-auto').checked && active==='logs') logTimer=setInterval(refreshLogs,3000); });
async function loadSettings(){ try{ const s=await getJ('/settings');
  $('set-notif').checked=!!s.notifications_enabled; $('set-auto').checked=!!s.autostart_installed; }catch(e){} }
async function toggleNotif(v){ await fetch('/settings/notifications',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:v})}); }
async function toggleAuto(v){ const cb=$('set-auto'); cb.disabled=true;
  try{ await fetch('/settings/autostart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:v})}); await loadSettings(); }
  catch(e){ alert('Failed: '+e.message); } cb.disabled=false; }
async function post(path,body={}){ try{ const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok) throw new Error(await r.text()||r.status); await poll(); }catch(e){ alert('Failed: '+e.message); } }
async function pollSLogs(){ if(active!=='status') return;
  try{ const d=await getJ('/logs?lines=24'); $('s-logs').innerHTML=(d.text||'(sync not running)').split('\n').map(l=>
    l.replace(/^(\[sync\])/,'<span class="ember">$1</span>')).join('\n'); $('s-logs').scrollTop=$('s-logs').scrollHeight; }catch(e){} }

poll(); pollSLogs(); setPanel('status');
setInterval(poll, 3000); setInterval(pollSLogs, 5000);
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
    serve_only: bool = False,
) -> int:
    """
    Launch the dashboard.

    By default tries to open in a native window via pywebview (looks like a
    real desktop app). Falls back to the system browser if pywebview is
    unavailable or use_window=False is forced.

    serve_only=True: start ONLY the HTTP server (no window, no browser),
    print {"port": N} as a JSON line to stdout, and block forever. This is
    the mode the Electron desktop app drives — it reads the port from
    stdout and loads it in a BrowserWindow.
    """
    try:
        backend = _backend()
    except (FileNotFoundError, ValueError) as e:
        if serve_only:
            print(json.dumps({"error": str(e)}), flush=True)
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    if port is None:
        port = _pick_free_port()
    url = f"http://127.0.0.1:{port}/"
    server = _start_http_server_in_thread(backend, port)

    if serve_only:
        # Machine-readable handshake for the Electron host, then block.
        print(json.dumps({"port": port, "url": url}), flush=True)
        try:
            import time
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return 0

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
