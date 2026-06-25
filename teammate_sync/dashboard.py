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
<html lang="en" class="dark"><head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CodeBaton</title>
    <script src="https://cdn.tailwindcss.com/3.4.17"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: { extend: {
                fontFamily: { sans: ['Inter','sans-serif'], mono: ['JetBrains Mono','monospace'] },
                colors: { brand: {
                    bg: '#0A0A0B', surface: '#121214', surfaceHover: '#1A1A1D',
                    border: '#27272A', borderSubtle: 'rgba(255,255,255,0.06)',
                    text: '#EDEDEF', textMuted: '#A0A0AB',
                    lime: '#00E57A', limeMuted: 'rgba(0,229,122,0.1)',
                    cobalt: '#3b6dff', cobaltMuted: 'rgba(59,109,255,0.12)',
                }},
                animation: { 'pulse-slow': 'pulse 3s cubic-bezier(0.4,0,0.6,1) infinite' },
            }}
        }
    </script>
    <style>
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #27272A; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #3F3F46; }
        /* subtly tinted dark base — depth without glare */
        body {
            background:
              radial-gradient(1000px 520px at 8% -14%, rgba(0,229,122,0.06), transparent 55%),
              radial-gradient(1000px 520px at 100% -6%, rgba(59,109,255,0.06), transparent 54%),
              #0a0b0e !important;
        }
        /* SOLID, defined surfaces with crisp borders + depth (not washed-out glass) */
        [class*="bg-[#0D0D0F]"] {
            background: #131419 !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 2px 8px -2px rgba(0,0,0,0.5);
        }
        .bg-brand-surface { background: #181920 !important; }
        aside { background: #0c0d11 !important; border-right: 1px solid rgba(255,255,255,0.09) !important; }
        main { background: #0a0b0e !important; }
        /* make every "subtle" border actually visible so cards read as distinct */
        .border-brand-borderSubtle { border-color: rgba(255,255,255,0.11) !important; }
        .divide-brand-borderSubtle > * + * { border-color: rgba(255,255,255,0.09) !important; }
        /* terminal-style log surface */
        .term-surface { background: #0b0c10 !important; box-shadow: inset 0 0 60px rgba(0,0,0,0.5); }
        .scanline-container { position: relative; overflow: hidden; }
        .scanline-container::after { content:''; position:absolute; inset:0; height:100%;
            background: linear-gradient(to bottom, transparent, rgba(0,229,122,0.03) 50%, transparent);
            animation: scan 8s linear infinite; pointer-events:none; }
        @keyframes scan { 0%{transform:translateY(-100%)} 100%{transform:translateY(100%)} }
        /* force-hide inactive views (beats Tailwind display utilities that load after this) */
        .view-section:not(.active) { display: none !important; }
        .view-section.active { animation: fadeSlideUp 0.3s cubic-bezier(0.16,1,0.3,1) forwards; }
        @keyframes fadeSlideUp { 0%{opacity:0;transform:translateY(8px)} 100%{opacity:1;transform:translateY(0)} }
        .toggle-checkbox:checked + .toggle-label { background-color: rgba(0,229,122,0.15); border-color: rgba(0,229,122,0.3); }
        .toggle-checkbox:checked + .toggle-label .toggle-dot { transform: translateX(100%); background-color: #00E57A; }
        @keyframes glowPulse { 0%,100%{box-shadow:0 0 20px rgba(0,229,122,0.05), inset 0 0 20px rgba(0,229,122,0.02)} 50%{box-shadow:0 0 50px rgba(0,229,122,0.15), inset 0 0 30px rgba(0,229,122,0.06)} }
        @keyframes float { 0%,100%{transform:translate(25%,25%) rotate(0)} 50%{transform:translate(25%,20%) rotate(3deg)} }
        @keyframes popIn { 0%{opacity:0;transform:scale(0.5)} 80%{transform:scale(1.15)} 100%{opacity:1;transform:scale(1)} }
        @keyframes blinkCursor { 0%,100%{opacity:1} 50%{opacity:0} }
        @keyframes updateBar { 0%{transform:translateX(-120%)} 100%{transform:translateX(320%)} }
        .animate-glow-pulse { animation: glowPulse 4s ease-in-out infinite; }
        .animate-float { animation: float 8s ease-in-out infinite; }
        .animate-pop-in { animation: popIn 0.5s cubic-bezier(0.34,1.56,0.64,1) forwards; }
        .animate-blink { animation: blinkCursor 1s step-end infinite; }
        .animate-update-bar { animation: updateBar 1.1s ease-in-out infinite; }
    </style>
</head>
<body class="bg-brand-bg text-brand-text font-sans h-screen w-screen overflow-hidden flex flex-col selection:bg-brand-lime selection:text-black antialiased">

    <div class="flex flex-1 overflow-hidden relative">
        <!-- transparent top drag region: drag the window from anywhere along the
             top edge. macOS floats its traffic-light controls over the left here,
             and the sidebar + content run full-bleed underneath (WhatsApp-style). -->
        <div class="absolute top-0 left-0 right-0 h-9 z-40 select-none" style="-webkit-app-region: drag;"></div>
        <!-- sidebar -->
        <aside class="w-64 border-r border-brand-borderSubtle bg-[#0D0D0F] flex flex-col justify-between flex-shrink-0 z-20 relative">
            <div>
                <div class="px-6 pt-12 pb-5 border-b border-brand-borderSubtle">
                    <div class="flex items-center gap-3">
                        <div class="flex-shrink-0">
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
                                <path d="M10 4L6 20" stroke="#2563EB" stroke-width="3" stroke-linecap="round"/>
                                <path d="M18 4L14 20" stroke="#00E57A" stroke-width="3" stroke-linecap="round"/>
                            </svg>
                        </div>
                        <div>
                            <h1 class="font-medium text-sm tracking-tight text-white">CodeBaton</h1>
                            <div class="text-xs text-brand-textMuted font-mono mt-0.5 flex items-center gap-1.5">
                                <span id="conn-dot" class="w-1.5 h-1.5 rounded-full bg-brand-textMuted"></span>
                                <span id="side-org">…</span>
                            </div>
                        </div>
                    </div>
                </div>

                <nav class="p-3 space-y-1 mt-2" id="sidebar-nav">
                    <a href="#" data-tab="view-overview" class="nav-item flex items-center justify-between px-3 py-2 rounded-md bg-brand-surface border border-white/5 text-white group transition-colors">
                        <div class="flex items-center gap-3">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon-wrapper text-brand-lime opacity-100"><rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/></svg>
                            <span class="text-sm font-medium">Overview</span>
                        </div>
                    </a>
                    <a href="#" data-tab="view-connections" class="nav-item flex items-center justify-between px-3 py-2 rounded-md text-brand-textMuted border border-transparent hover:bg-brand-surface hover:text-white transition-colors group">
                        <div class="flex items-center gap-3">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon-wrapper opacity-70 group-hover:opacity-100"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
                            <span class="text-sm font-medium">Connections</span>
                        </div>
                        <span id="nav-conn-count" class="text-xs font-mono bg-brand-bg border border-brand-borderSubtle px-1.5 py-0.5 rounded text-brand-textMuted">0</span>
                    </a>
                    <a href="#" data-tab="view-sessions" class="nav-item flex items-center justify-between px-3 py-2 rounded-md text-brand-textMuted border border-transparent hover:bg-brand-surface hover:text-white transition-colors group">
                        <div class="flex items-center gap-3">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon-wrapper opacity-70 group-hover:opacity-100"><polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/></svg>
                            <span class="text-sm font-medium">Sessions</span>
                        </div>
                    </a>
                    <a href="#" data-tab="view-activity" class="nav-item flex items-center justify-between px-3 py-2 rounded-md text-brand-textMuted border border-transparent hover:bg-brand-surface hover:text-white transition-colors group">
                        <div class="flex items-center gap-3">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon-wrapper opacity-70 group-hover:opacity-100"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>
                            <span class="text-sm font-medium">Activity</span>
                        </div>
                    </a>
                    <div class="pt-4 mt-2 border-t border-brand-borderSubtle">
                        <a href="#" data-tab="view-settings" class="nav-item flex items-center justify-between px-3 py-2 rounded-md text-brand-textMuted border border-transparent hover:bg-brand-surface hover:text-white transition-colors group">
                            <div class="flex items-center gap-3">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon-wrapper opacity-70 group-hover:opacity-100"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
                                <span class="text-sm font-medium">Settings</span>
                            </div>
                        </a>
                    </div>
                </nav>
            </div>
            <div class="p-4 border-t border-brand-borderSubtle">
                <div class="flex items-center gap-3 px-2 py-2 rounded-md">
                    <div id="side-avatar" class="w-8 h-8 rounded-full border border-white/10 grid place-items-center text-sm font-semibold text-brand-lime bg-brand-limeMuted">?</div>
                    <div class="flex-1 min-w-0">
                        <p id="side-handle" class="text-sm font-medium text-white truncate">…</p>
                        <p class="text-xs text-brand-textMuted truncate font-mono">signed in</p>
                    </div>
                </div>
            </div>
        </aside>

        <!-- main -->
        <main class="flex-1 flex flex-col min-w-0 overflow-y-auto relative z-10 bg-brand-bg">
            <div class="max-w-5xl w-full mx-auto px-10 pt-12 pb-10 relative">

                <!-- UPDATE BANNER (shown only while checking / downloading / ready) -->
                <div id="update-banner" class="hidden mb-6 rounded-lg border px-4 py-3 flex items-center gap-3 text-sm">
                    <span id="update-icon" class="flex-shrink-0"></span>
                    <div class="flex-1 min-w-0">
                        <p id="update-text" class="text-white font-medium"></p>
                        <p id="update-sub" class="hidden text-brand-textMuted text-xs mt-0.5"></p>
                        <div id="update-bar-wrap" class="hidden mt-2 h-1 w-full rounded-full bg-white/10 overflow-hidden">
                            <div class="h-full w-1/3 rounded-full bg-brand-lime animate-update-bar"></div>
                        </div>
                    </div>
                </div>

                <!-- OVERVIEW -->
                <div id="view-overview" class="view-section active">
                    <header class="mb-8">
                        <h2 class="text-2xl font-medium text-white tracking-tight">Overview</h2>
                        <p class="text-sm text-brand-textMuted mt-1">Your live context-sharing state.</p>
                    </header>

                    <section id="daemon-card" class="mb-10 scanline-container rounded-xl border border-brand-borderSubtle bg-brand-surface shadow-lg relative animate-glow-pulse">
                        <div class="p-8 flex flex-col sm:flex-row sm:items-center justify-between gap-6 relative z-10">
                            <div class="flex items-start gap-5">
                                <div class="mt-1 relative flex h-4 w-4 items-center justify-center flex-shrink-0">
                                    <span id="daemon-pulse" class="absolute inline-flex h-full w-full animate-pulse-slow rounded-full bg-brand-lime opacity-30"></span>
                                    <span id="daemon-led" class="relative inline-flex h-2.5 w-2.5 rounded-full bg-brand-lime shadow-[0_0_10px_rgba(0,229,122,0.5)]"></span>
                                </div>
                                <div>
                                    <h3 id="daemon-title" class="text-lg font-medium text-white tracking-tight">Sync engine…</h3>
                                    <div class="mt-2 flex items-center gap-3 text-sm">
                                        <span id="daemon-meta" class="text-brand-textMuted">checking…</span>
                                    </div>
                                </div>
                            </div>
                            <button id="daemon-btn" data-action="daemon" class="group relative flex h-10 items-center justify-center gap-2.5 rounded-md border border-white/10 bg-brand-bg px-6 font-mono text-sm font-medium text-white transition-all hover:border-white/20 hover:bg-white/5 hover:-translate-y-px">
                                <span id="daemon-btn-led" class="h-1.5 w-1.5 rounded-full bg-red-500"></span>
                                <span id="daemon-btn-label">Stop Engine</span>
                            </button>
                        </div>
                        <div class="absolute right-0 bottom-0 opacity-[0.07] pointer-events-none animate-float">
                            <svg width="200" height="200" viewBox="0 0 24 24" fill="none"><path d="M10 4L6 20M18 4L14 20" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
                        </div>
                    </section>

                    <section class="mb-12 grid grid-cols-1 md:grid-cols-3 gap-5">
                        <div class="rounded-lg border border-brand-borderSubtle bg-[#0D0D0F] p-5 hover:border-white/10 transition-all hover:-translate-y-1">
                            <div class="flex items-center justify-between mb-4">
                                <span class="font-mono text-[11px] font-medium text-brand-textMuted uppercase tracking-widest">Connections</span>
                            </div>
                            <div class="flex items-baseline gap-2"><span id="stat-conn" class="text-3xl font-semibold text-white">0</span><span class="text-sm font-medium text-[#61C554]">teammates</span></div>
                            <p class="text-[11px] text-brand-textMuted mt-2 leading-snug">Teammates you're connected to this session. Cleared when you quit CodeBaton.</p>
                        </div>
                        <div class="rounded-lg border border-brand-lime/25 bg-[#0D0D0F] p-5 hover:border-brand-lime/45 transition-all hover:-translate-y-1 shadow-[0_0_36px_-16px_rgba(0,229,122,0.6)]">
                            <div class="flex items-center justify-between mb-4">
                                <span class="font-mono text-[11px] font-medium text-brand-lime uppercase tracking-widest">Sharing</span>
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-brand-lime"><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg>
                            </div>
                            <div class="flex items-baseline gap-2"><span id="stat-sharing" class="text-3xl font-semibold text-white">0</span><span class="text-sm font-medium text-brand-textMuted">sessions</span></div>
                        </div>
                        <div class="rounded-lg border border-brand-cobalt/30 bg-[#0D0D0F] p-5 hover:border-brand-cobalt/55 transition-all hover:-translate-y-1 cursor-pointer shadow-[0_0_36px_-16px_rgba(59,109,255,0.65)]" data-go="view-sessions">
                            <div class="flex items-center justify-between mb-4">
                                <span class="font-mono text-[11px] font-medium text-brand-cobalt uppercase tracking-widest">Receiving</span>
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-brand-cobalt"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>
                            </div>
                            <div class="flex items-baseline gap-2"><span id="stat-receiving" class="text-3xl font-semibold text-white">0</span><span class="text-sm font-medium text-brand-textMuted">sessions</span></div>
                        </div>
                    </section>

                    <section>
                        <div class="flex items-center justify-between mb-4">
                            <h3 class="text-sm font-medium text-white tracking-tight">You're sharing</h3>
                            <button class="text-xs font-mono text-brand-textMuted hover:text-white" data-go="view-sessions">View sessions →</button>
                        </div>
                        <div id="ov-sessions"></div>
                    </section>
                </div>

                <!-- CONNECTIONS -->
                <div id="view-connections" class="view-section">
                    <header class="mb-8">
                        <h2 class="text-2xl font-medium text-white tracking-tight">Connections</h2>
                        <p class="text-sm text-brand-textMuted mt-1">Teammates you can query — and who can query you.</p>
                    </header>
                    <div class="space-y-8">
                        <section>
                            <h3 class="text-xs font-mono font-medium text-brand-textMuted uppercase tracking-widest mb-3 flex items-center gap-2">Invites to you <span id="conn-in-count" class="bg-brand-limeMuted text-brand-lime px-1.5 py-0.5 rounded-full text-[10px]">0</span></h3>
                            <div id="conn-incoming"></div>
                        </section>
                        <section>
                            <h3 class="text-xs font-mono font-medium text-brand-textMuted uppercase tracking-widest mb-3">Connected (<span id="conn-active-count">0</span>)</h3>
                            <div id="conn-active"></div>
                        </section>
                        <section>
                            <h3 class="text-xs font-mono font-medium text-brand-textMuted uppercase tracking-widest mb-3">Invites you sent (<span id="conn-out-count">0</span>)</h3>
                            <div id="conn-pending"></div>
                        </section>
                    </div>
                </div>

                <!-- SESSIONS -->
                <div id="view-sessions" class="view-section">
                    <header class="mb-8">
                        <h2 class="text-2xl font-medium text-white tracking-tight">Sessions</h2>
                        <p class="text-sm text-brand-textMuted mt-1">Live Claude Code context flowing between you and your team.</p>
                    </header>
                    <div class="space-y-8">
                        <section>
                            <div class="flex items-center justify-between mb-3">
                                <h3 class="text-xs font-mono font-medium text-brand-textMuted uppercase tracking-widest flex items-center gap-2">
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg>
                                    Sharing
                                </h3>
                            </div>
                            <div id="sess-sharing"></div>
                        </section>
                        <section>
                            <h3 class="text-xs font-mono font-medium text-brand-textMuted uppercase tracking-widest mb-3 flex items-center gap-2">
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-brand-cobalt"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>
                                Shared with you
                            </h3>
                            <div id="sess-receiving"></div>
                        </section>
                    </div>
                </div>

                <!-- ACTIVITY -->
                <div id="view-activity" class="view-section">
                    <header class="mb-6 flex items-end justify-between">
                        <div>
                            <h2 class="text-2xl font-medium text-white tracking-tight">Activity</h2>
                            <p class="text-sm text-brand-textMuted mt-1">Live events from the sync engine.</p>
                        </div>
                        <div class="flex gap-2">
                            <button id="btn-pause-log" class="px-3 py-1.5 text-xs font-mono font-medium text-brand-textMuted bg-brand-surface border border-brand-borderSubtle hover:border-white/20 rounded transition-colors w-20 text-center">Pause</button>
                        </div>
                    </header>
                    <div class="rounded-xl border border-white/15 overflow-hidden shadow-2xl shadow-black/50">
                        <div class="flex items-center gap-2.5 px-4 py-2.5 border-b border-white/10 bg-white/[0.03]">
                            <span class="w-2 h-2 rounded-full bg-brand-lime shadow-[0_0_8px_rgba(0,229,122,0.7)]"></span>
                            <span class="text-[11px] font-mono uppercase tracking-widest text-brand-textMuted">Live sync log</span>
                            <span class="ml-auto text-[11px] font-mono text-brand-textMuted/60">daemon.log</span>
                        </div>
                        <div id="activity-log" class="term-surface overflow-y-auto p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all text-brand-textMuted" style="height: calc(100vh - 280px);"></div>
                    </div>
                </div>

                <!-- SETTINGS -->
                <div id="view-settings" class="view-section">
                    <header class="mb-8">
                        <h2 class="text-2xl font-medium text-white tracking-tight">Settings</h2>
                        <p class="text-sm text-brand-textMuted mt-1">Account and app preferences.</p>
                    </header>
                    <div class="max-w-2xl space-y-8">
                        <section class="bg-[#0D0D0F] border border-brand-borderSubtle rounded-lg p-6">
                            <h3 class="text-sm font-medium text-white mb-4">Account</h3>
                            <div class="space-y-4">
                                <div>
                                    <label class="block text-xs font-mono text-brand-textMuted mb-1.5 ml-1">GitHub handle</label>
                                    <input id="set-handle" type="text" value="…" readonly class="w-full bg-brand-surface/50 border border-brand-borderSubtle rounded-md px-3 py-2 text-sm text-brand-textMuted cursor-not-allowed font-mono focus:outline-none">
                                </div>
                                <div>
                                    <label class="block text-xs font-mono text-brand-textMuted mb-1.5 ml-1">Workspace (GitHub org)</label>
                                    <input id="set-org" type="text" value="…" readonly class="w-full bg-brand-surface/50 border border-brand-borderSubtle rounded-md px-3 py-2 text-sm text-brand-textMuted cursor-not-allowed font-mono focus:outline-none">
                                </div>
                            </div>
                        </section>
                        <section class="bg-[#0D0D0F] border border-brand-borderSubtle rounded-lg p-6 flex items-center justify-between gap-4">
                            <div class="min-w-0">
                                <h4 class="text-sm font-medium text-white">Software update</h4>
                                <p class="text-xs text-brand-textMuted mt-0.5">
                                    <span id="upd-version" class="font-mono">CodeBaton</span>
                                    <span id="upd-status" class="ml-1"></span>
                                </p>
                            </div>
                            <button id="btn-check-update" class="px-4 py-2 text-xs font-medium text-white bg-brand-surface hover:bg-white/5 border border-brand-borderSubtle rounded transition-colors whitespace-nowrap">Check for updates</button>
                        </section>
                        <section class="bg-[#0D0D0F] border border-brand-borderSubtle rounded-lg p-6 space-y-6">
                            <div class="flex items-center justify-between">
                                <div>
                                    <h4 class="text-sm font-medium text-white">Desktop notifications</h4>
                                    <p class="text-xs text-brand-textMuted mt-0.5">Alert when a teammate connects or accepts your invite.</p>
                                </div>
                                <label class="relative flex items-center cursor-pointer">
                                    <input id="set-notif" type="checkbox" class="sr-only peer toggle-checkbox" data-setting="notifications">
                                    <div class="toggle-label w-11 h-6 bg-brand-surface border border-brand-borderSubtle rounded-full transition-colors relative">
                                        <div class="toggle-dot absolute top-[2px] left-[2px] bg-brand-textMuted rounded-full h-5 w-5 transition-transform border border-black/10"></div>
                                    </div>
                                </label>
                            </div>
                            <div class="w-full h-px bg-brand-borderSubtle"></div>
                            <div class="flex items-center justify-between">
                                <div>
                                    <h4 class="text-sm font-medium text-white">Capture decisions</h4>
                                    <p class="text-xs text-brand-textMuted mt-0.5">Silently distill your sessions into a shared decision log (knowledge.md) for /ask-all.</p>
                                </div>
                                <label class="relative flex items-center cursor-pointer">
                                    <input id="set-distill" type="checkbox" class="sr-only peer toggle-checkbox" data-setting="distill">
                                    <div class="toggle-label w-11 h-6 bg-brand-surface border border-brand-borderSubtle rounded-full transition-colors relative">
                                        <div class="toggle-dot absolute top-[2px] left-[2px] bg-brand-textMuted rounded-full h-5 w-5 transition-transform border border-black/10"></div>
                                    </div>
                                </label>
                            </div>
                            <div class="w-full h-px bg-brand-borderSubtle"></div>
                            <div class="flex items-center justify-between">
                                <div>
                                    <h4 class="text-sm font-medium text-white">Launch at login</h4>
                                    <p class="text-xs text-brand-textMuted mt-0.5">Start CodeBaton automatically on boot.</p>
                                </div>
                                <label class="relative flex items-center cursor-pointer">
                                    <input id="set-autostart" type="checkbox" class="sr-only peer toggle-checkbox" data-setting="autostart">
                                    <div class="toggle-label w-11 h-6 bg-brand-surface border border-brand-borderSubtle rounded-full transition-colors relative">
                                        <div class="toggle-dot absolute top-[2px] left-[2px] bg-brand-textMuted rounded-full h-5 w-5 transition-transform border border-black/10"></div>
                                    </div>
                                </label>
                            </div>
                        </section>

                        <section class="bg-[#0D0D0F] border border-brand-borderSubtle rounded-lg p-6 flex items-center justify-between gap-4">
                            <div>
                                <h4 class="text-sm font-medium text-white">Sign out</h4>
                                <p class="text-xs text-brand-textMuted mt-0.5">Stop the daemon and clear your saved sign-in. You can sign back in right after.</p>
                            </div>
                            <button id="btn-signout" class="px-4 py-2 text-xs font-medium text-red-400 bg-red-500/10 hover:bg-red-500/20 border border-red-500/25 rounded transition-colors whitespace-nowrap">Sign out</button>
                        </section>
                    </div>
                </div>

                <div class="h-10"></div>
            </div>
        </main>
    </div>

    <!-- in-app sign-in screen (shown whenever not signed in) -->
    <div id="signin-screen" class="fixed inset-0 z-[100] hidden items-center justify-center" style="background:#0a0b0e;">
      <div class="absolute top-0 left-0 right-0 h-9" style="-webkit-app-region: drag;"></div>
      <div class="max-w-sm w-full mx-6 text-center">
        <svg width="48" height="48" viewBox="0 0 44 44" fill="none" class="mx-auto mb-6"><line x1="13" y1="32" x2="23" y2="13" stroke="#3b6dff" stroke-width="6.5" stroke-linecap="round"/><line x1="23" y1="31" x2="33" y2="12" stroke="#00E57A" stroke-width="6.5" stroke-linecap="round"/><circle cx="33" cy="12" r="3.4" fill="#d6ffe9"/></svg>
        <h2 class="text-2xl font-semibold text-white tracking-tight">Welcome to CodeBaton</h2>
        <p class="text-sm text-brand-textMuted mt-2">Sign in with GitHub to connect your live Claude Code sessions with your team.</p>

        <!-- step 1: sign in -->
        <div id="si-step1" class="mt-8">
          <button id="si-github" class="w-full py-3 rounded-lg bg-white text-black font-semibold text-sm hover:bg-gray-200 transition flex items-center justify-center gap-2">
            <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>
            Sign in with GitHub
          </button>
        </div>

        <!-- waiting for browser auth -->
        <div id="si-waiting" class="mt-8 hidden">
          <div class="inline-flex items-center gap-2 text-sm text-brand-textMuted">
            <span class="w-2 h-2 rounded-full bg-brand-lime animate-pulse"></span>
            Waiting for GitHub authorization in your browser…
          </div>
          <button id="si-retry" class="block mx-auto mt-4 text-xs text-brand-textMuted hover:text-white underline">Reopen browser</button>
        </div>

        <!-- step 2: pick org -->
        <div id="si-step2" class="mt-8 hidden text-left">
          <p class="text-xs font-mono text-brand-textMuted uppercase tracking-widest mb-3">Choose your workspace</p>
          <div id="si-orgs" class="space-y-2"></div>
        </div>

        <p id="si-error" class="mt-4 text-sm text-red-400 hidden"></p>
      </div>
    </div>

<script>
const $ = id => document.getElementById(id);
const esc = s => (s==null?'':String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])));
const initial = h => (h||'?').replace(/[^a-z0-9]/ig,'').charAt(0).toUpperCase() || '?';
function ago(ep){ if(!ep) return ''; const s=(Date.now()/1000)-ep;
  if(s<60) return Math.max(0,Math.floor(s))+'s ago'; if(s<3600) return Math.floor(s/60)+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago'; return Math.floor(s/86400)+'d ago'; }
async function getJ(u,o){ const r=await fetch(u,{cache:'no-store',...o}); if(!r.ok) throw new Error(r.status+' '+await r.text()); return r.json(); }
function avatar(h, accent){ const c = accent ? 'text-brand-lime bg-brand-limeMuted border-brand-lime/30' : 'text-brand-text bg-brand-surface border-white/10';
  return `<div class="w-9 h-9 rounded-full border grid place-items-center text-sm font-semibold ${c}">${initial(h)}</div>`; }

// ── tab switching ──
const tabs = document.querySelectorAll('[data-tab]');
const views = document.querySelectorAll('.view-section');
let active = 'view-overview';
function go(id){
  active = id;
  tabs.forEach(t=>{
    const on = t.getAttribute('data-tab')===id;
    t.classList.toggle('bg-brand-surface', on); t.classList.toggle('border-white/5', on); t.classList.toggle('text-white', on);
    t.classList.toggle('text-brand-textMuted', !on); t.classList.toggle('border-transparent', !on);
    const ic = t.querySelector('.icon-wrapper');
    ic.classList.toggle('opacity-100', on); ic.classList.toggle('text-brand-lime', on); ic.classList.toggle('opacity-70', !on);
  });
  views.forEach(v=>{ v.classList.remove('active'); if(v.id===id){ void v.offsetWidth; v.classList.add('active'); } });
  if(id==='view-activity') refreshLogs();
  if(id==='view-settings') loadSettings();
}
tabs.forEach(t=>t.addEventListener('click',e=>{e.preventDefault(); go(t.getAttribute('data-tab'));}));
document.addEventListener('click', e=>{ const g=e.target.closest('[data-go]'); if(g) go(g.getAttribute('data-go')); });

// ── actions ──
document.addEventListener('click', async e=>{
  const b = e.target.closest('[data-action]'); if(!b) return;
  const act = b.getAttribute('data-action');
  try{
    if(act==='accept')     await post('/accept',{peer:b.dataset.peer});
    else if(act==='decline')await post('/decline',{peer:b.dataset.peer});
    else if(act==='disconnect')await post('/disconnect',{peer:b.dataset.peer});
    else if(act==='unshare') await post('/unshare',{session_id:b.dataset.session});
    else if(act==='daemon'){ const alive=b.dataset.alive==='1'; await post(alive?'/daemon/stop':'/daemon/start'); }
    await poll();
  }catch(err){ alert('Failed: '+err.message); }
});
async function post(path, body={}){ const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); if(!r.ok) throw new Error(await r.text()||r.status); return r.json().catch(()=>({})); }

// ── render ──
let last=null;
async function poll(){
  try{
    const d = await getJ('/data.json'); last=d;
    if(d.signed_in===false){ showSignin(); return; }
    hideSignin();
    $('conn-dot').className='w-1.5 h-1.5 rounded-full bg-brand-lime';
    $('side-org').textContent = d.org||'…';
    $('side-handle').textContent = '@'+(d.me||'?');
    $('side-avatar').textContent = initial(d.me);
    $('set-handle').value = '@'+(d.me||'?');
    $('set-org').value = d.org||'';
    renderOverview(d); renderConnections(d); renderSessions(d);
  }catch(e){
    $('conn-dot').className='w-1.5 h-1.5 rounded-full bg-red-500';
  }
}
function renderOverview(d){
  const c=d.connections||{}, acc=c.accepted||[], pin=c.pending_incoming||[], pout=c.pending_outgoing||[];
  const mine=d.my_sessions||[], theirs=(d.teammates||[]).flatMap(t=>t.sessions||[]);
  $('stat-conn').textContent=acc.length; $('stat-sharing').textContent=mine.length; $('stat-receiving').textContent=theirs.length;
  $('nav-conn-count').textContent = acc.length+pin.length+pout.length;
  const dm=d.daemon||{}; const alive=!!dm.alive;
  $('daemon-led').className='relative inline-flex h-2.5 w-2.5 rounded-full '+(alive?'bg-brand-lime shadow-[0_0_10px_rgba(0,229,122,0.5)]':'bg-red-500');
  $('daemon-pulse').style.display = alive?'inline-flex':'none';
  $('daemon-title').textContent = alive?'Sync engine running':'Sync engine stopped';
  $('daemon-meta').textContent = alive?('pid '+dm.pid+' · context flows to your connections in real time'):'Start it to share and receive teammate context.';
  $('daemon-btn').dataset.alive = alive?'1':'0';
  $('daemon-btn-led').className='h-1.5 w-1.5 rounded-full '+(alive?'bg-red-500':'bg-brand-lime');
  $('daemon-btn-label').textContent = alive?'Stop Engine':'Start Engine';
  $('ov-sessions').innerHTML = mine.length ? mine.map(sessionRow).join('') : emptyShare();
}
function sessionRow(s){
  const recips=(s.recipients||[]).map(r=>`<span class="text-xs font-mono text-brand-lime">@${esc(r)}</span>`).join(', ')||'<span class="text-xs text-red-400">no recipients</span>';
  return `<div class="rounded-lg border border-brand-borderSubtle bg-[#0D0D0F] p-5 mb-3">
    <div class="flex items-center justify-between gap-4 flex-wrap">
      <div>
        <div class="flex items-center gap-2 mb-1"><h4 class="text-sm font-medium text-white font-mono">${esc(s.session_id.slice(0,18))}…</h4></div>
        <div class="flex items-center gap-2 mt-1"><span class="text-xs text-brand-textMuted">shared with</span> ${recips}
          ${s.shared_at?`<span class="text-xs text-brand-textMuted ml-1">· ${ago(s.shared_at)}</span>`:''}</div>
      </div>
      <button data-action="unshare" data-session="${esc(s.session_id)}" class="px-4 py-2 text-xs font-medium text-red-400 bg-red-500/10 hover:bg-red-500/20 border border-red-500/20 rounded transition-colors">Stop sharing</button>
    </div></div>`;
}
function emptyShare(){
  return `<div class="w-full rounded-xl border border-dashed border-white/10 bg-[#0A0A0B] py-14 px-6 flex flex-col items-center text-center">
    <div class="mb-5 rounded-full bg-brand-surface border border-white/5 p-4"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="text-brand-textMuted"><polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/></svg></div>
    <h4 class="text-base font-medium text-white">You're not sharing anything</h4>
    <p class="mt-2 text-sm text-brand-textMuted max-w-sm">In a Claude Code session, run <span class="font-mono text-brand-lime">/connect &lt;teammate&gt;</span> to share it.</p></div>`;
}
function connRow(handle, accent, btns){
  return `<div class="flex items-center justify-between p-4 hover:bg-white/[0.02] transition-colors group">
    <div class="flex items-center gap-3">${avatar(handle, accent)}<div><p class="text-sm font-medium text-white">@${esc(handle)}</p></div></div>
    <div class="flex items-center gap-2">${btns}</div></div>`;
}
function renderConnections(d){
  const c=d.connections||{}, acc=c.accepted||[], pin=c.pending_incoming||[], pout=c.pending_outgoing||[];
  $('conn-in-count').textContent=pin.length; $('conn-active-count').textContent=acc.length; $('conn-out-count').textContent=pout.length;
  $('conn-incoming').innerHTML = pin.length
    ? `<div class="rounded-lg border border-brand-lime/20 bg-brand-limeMuted divide-y divide-brand-borderSubtle overflow-hidden">`+pin.map(x=>connRow(x.peer_handle,true,
        `<button data-action="decline" data-peer="${esc(x.peer_handle)}" class="px-3 py-1.5 text-xs font-medium text-brand-textMuted hover:text-white hover:bg-brand-surface rounded transition-colors">Decline</button>
         <button data-action="accept" data-peer="${esc(x.peer_handle)}" class="px-3 py-1.5 text-xs font-medium text-brand-bg bg-brand-lime hover:opacity-90 rounded transition-colors">Accept</button>`)).join('')+`</div>`
    : `<p class="text-sm text-brand-textMuted">None.</p>`;
  $('conn-active').innerHTML = acc.length
    ? `<div class="rounded-lg border border-brand-borderSubtle bg-[#0D0D0F] divide-y divide-brand-borderSubtle overflow-hidden">`+acc.map(x=>connRow(x.peer_handle,false,
        `<button data-action="disconnect" data-peer="${esc(x.peer_handle)}" class="opacity-0 group-hover:opacity-100 px-3 py-1.5 text-xs font-medium text-red-400 hover:bg-red-500/10 rounded transition-all">Disconnect</button>`)).join('')+`</div>`
    : `<p class="text-sm text-brand-textMuted">No connections yet. Run <span class="font-mono text-brand-lime">/connect &lt;handle&gt;</span> in Claude Code.</p>`;
  $('conn-pending').innerHTML = pout.length
    ? `<div class="rounded-lg border border-brand-borderSubtle bg-brand-bg divide-y divide-brand-borderSubtle overflow-hidden">`+pout.map(x=>connRow(x.peer_handle,false,
        `<span class="text-xs text-brand-textMuted mr-1">awaiting their /connect</span>
         <button data-action="disconnect" data-peer="${esc(x.peer_handle)}" class="px-3 py-1.5 text-xs font-medium text-brand-textMuted hover:text-white hover:bg-white/5 rounded transition-colors">Cancel</button>`)).join('')+`</div>`
    : `<p class="text-sm text-brand-textMuted">None.</p>`;
}
function renderSessions(d){
  const mine=d.my_sessions||[];
  $('sess-sharing').innerHTML = mine.length ? mine.map(sessionRow).join('') : emptyShare();
  const tm=d.teammates||[];
  $('sess-receiving').innerHTML = tm.length ? tm.map(t=>`
    <div class="rounded-lg border border-brand-borderSubtle bg-[#0D0D0F] overflow-hidden mb-3">
      <div class="p-4 flex items-center gap-3 border-b border-white/5">${avatar(t.handle,false)}<span class="text-sm font-medium text-white font-mono">@${esc(t.handle)}</span></div>
      ${(t.sessions||[]).map(s=>`<div class="p-4 border-t border-white/5 first:border-t-0">
        <div class="flex items-center justify-between gap-3 flex-wrap">
          <span class="text-xs font-mono text-brand-textMuted">${esc(s.session_id.slice(0,20))}… · ${ago(s.shared_at)}</span>
          <button class="px-3 py-1.5 text-xs font-medium text-brand-cobalt bg-brand-cobaltMuted hover:bg-brand-cobalt/20 border border-brand-cobalt/20 rounded transition-colors" onclick="dump('${esc(t.handle)}','${esc(s.session_id)}',this)">View raw context</button>
        </div></div>`).join('')}
    </div>`).join('')
    : `<div class="rounded-lg border border-dashed border-white/10 p-8 text-center"><p class="text-sm text-brand-textMuted">Nothing shared with you yet.</p><p class="text-xs text-brand-textMuted mt-1">A connected teammate runs <span class="font-mono">/connect &lt;you&gt;</span> in their session.</p></div>`;
}
async function dump(h,sid,btn){
  btn.disabled=true; btn.textContent='loading…';
  try{ const r=await fetch('/dump?teammate='+encodeURIComponent(h)+'&session='+encodeURIComponent(sid)); const t=await r.text();
    const card=btn.closest('.p-4'); let pre=card.querySelector('pre');
    if(!pre){ pre=document.createElement('pre'); pre.className='mt-3 p-3 bg-black border border-white/5 rounded text-[11px] font-mono text-brand-textMuted overflow-auto max-h-80 whitespace-pre-wrap'; card.appendChild(pre); }
    pre.textContent=t; btn.textContent='View raw context'; btn.disabled=false;
  }catch(e){ btn.textContent='error'; }
}

// ── activity log ──
let logPaused=false;
$('btn-pause-log').addEventListener('click', function(){ logPaused=!logPaused; this.textContent=logPaused?'Resume':'Pause';
  this.classList.toggle('text-brand-lime',logPaused); this.classList.toggle('border-brand-lime/30',logPaused); });
async function refreshLogs(){
  if(logPaused) return;
  try{ const d=await getJ('/logs?lines=300'); const el=$('activity-log');
    el.innerHTML=(d.text||'(no activity yet — start the engine)').split('\n').map(l=>{
      let cls='text-brand-textMuted';
      if(/error|fail|traceback/i.test(l)) cls='text-red-400';
      else if(l.includes('[sync]')) cls='text-brand-text';
      return '<div class="'+cls+'">'+esc(l)+'</div>';
    }).join(''); el.scrollTop=el.scrollHeight;
  }catch(e){ $('activity-log').textContent='Error: '+e.message; }
}

// ── settings ──
async function loadSettings(){ try{ const s=await getJ('/settings'); $('set-notif').checked=!!s.notifications_enabled; $('set-autostart').checked=!!s.autostart_installed; $('set-distill').checked=!!s.distill_enabled; }catch(e){} }
const _SETTING_PATHS = {notifications:'/settings/notifications', autostart:'/settings/autostart', distill:'/settings/distill'};
document.querySelectorAll('[data-setting]').forEach(el=>el.addEventListener('change', async function(){
  const path = _SETTING_PATHS[this.dataset.setting];
  try{ await post(path,{enabled:this.checked}); }catch(e){ alert('Failed: '+e.message); }
}));

// ── sign out ──
$('btn-signout').addEventListener('click', async ()=>{
  if(!confirm('Sign out? This stops the daemon and clears your saved sign-in.')) return;
  try{ await post('/logout',{}); }catch(e){}
  resetSignin(); showSignin();
});

// ── in-app sign in ──
const signin = $('signin-screen');
let signinPoll = null;
function showSignin(){ signin.classList.remove('hidden'); signin.classList.add('flex'); }
function hideSignin(){ signin.classList.add('hidden'); signin.classList.remove('flex'); }
function siErr(m){ $('si-error').textContent=m; $('si-error').classList.remove('hidden'); }
function resetSignin(){
  if(signinPoll){ clearInterval(signinPoll); signinPoll=null; }
  $('si-step1').classList.remove('hidden');
  $('si-waiting').classList.add('hidden');
  $('si-step2').classList.add('hidden');
  $('si-error').classList.add('hidden');
  $('si-orgs').innerHTML='';
}
async function startSignin(){
  $('si-error').classList.add('hidden');
  try{ await post('/auth/start',{}); }catch(e){ siErr('Could not open browser: '+e.message); return; }
  $('si-step1').classList.add('hidden');
  $('si-waiting').classList.remove('hidden');
  if(signinPoll) clearInterval(signinPoll);
  signinPoll = setInterval(async ()=>{
    try{ const s=await getJ('/auth/status'); if(s.pending){ clearInterval(signinPoll); signinPoll=null; loadOrgs(); } }catch(e){}
  }, 1500);
}
$('si-github').addEventListener('click', startSignin);
$('si-retry').addEventListener('click', ()=>{ post('/auth/start',{}).catch(()=>{}); });
async function loadOrgs(){
  $('si-waiting').classList.add('hidden');
  $('si-step2').classList.remove('hidden');
  try{
    const d = await getJ('/auth/orgs');
    const orgs = d.orgs||[];
    $('si-orgs').innerHTML = orgs.length
      ? orgs.map(o=>'<button class="si-org w-full text-left px-4 py-3 rounded-lg border border-white/12 bg-[#131419] hover:border-brand-lime/50 hover:bg-white/[0.03] transition text-sm text-white font-mono" data-org="'+esc(o)+'">'+esc(o)+'</button>').join('')
      : '<p class="text-sm text-brand-textMuted">No organizations visible. CodeBaton may need approval for your org — grant it at github.com/settings/applications, then sign out and retry.</p>';
  }catch(e){ siErr('Could not load organizations: '+e.message); }
}
document.addEventListener('click', async e=>{
  const ob = e.target.closest('.si-org'); if(!ob) return;
  document.querySelectorAll('.si-org').forEach(b=>b.disabled=true);
  ob.textContent='Setting up…';
  try{
    await post('/auth/finish',{org:ob.dataset.org});
    resetSignin(); hideSignin(); poll();
  }catch(err){
    siErr('Setup failed: '+err.message);
    document.querySelectorAll('.si-org').forEach(b=>b.disabled=false);
  }
});

// ── update banner + settings card ──
function renderUpdate(u){
  const b=$('update-banner'), icon=$('update-icon'), text=$('update-text'),
        sub=$('update-sub'), bar=$('update-bar-wrap');
  bar.classList.add('hidden'); sub.classList.add('hidden');
  const s=u&&u.state;
  // Settings → Software update card (always reflects current version + state)
  const uv=$('upd-version'), ust=$('upd-status');
  if(uv) uv.textContent='CodeBaton v'+((u&&u.running)||'?');
  if(ust){
    if(s==='downloading'){ ust.textContent='· downloading v'+(u.latest||'')+'…'; ust.className='ml-1 text-brand-lime'; }
    else if(s==='ready'){ ust.textContent='· v'+(u.latest||'')+' ready — reopen to apply'; ust.className='ml-1 text-brand-lime'; }
    else if(s==='checking'){ ust.textContent='· checking…'; ust.className='ml-1 text-brand-textMuted'; }
    else if(s==='error'){ ust.textContent='· last check failed'; ust.className='ml-1 text-red-400'; }
    else if(s==='offline'){ ust.textContent='· offline'; ust.className='ml-1 text-brand-textMuted'; }
    else { ust.textContent='· up to date'; ust.className='ml-1 text-brand-textMuted'; }
  }
  if(s==='checking'){
    b.className='mb-6 rounded-lg border border-white/10 bg-white/[0.03] px-4 py-3 flex items-center gap-3 text-sm';
    icon.innerHTML='<span class="block w-2 h-2 rounded-full bg-brand-textMuted animate-pulse"></span>';
    text.textContent='Checking for updates…';
  } else if(s==='downloading'){
    b.className='mb-6 rounded-lg border border-brand-lime/30 bg-brand-lime/[0.06] px-4 py-3 flex items-center gap-3 text-sm';
    icon.innerHTML='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#00E57A" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg>';
    text.textContent='Downloading update'+(u.latest?(' v'+u.latest):'')+'…';
    bar.classList.remove('hidden');
  } else if(s==='ready'){
    b.className='mb-6 rounded-lg border border-brand-lime/40 bg-brand-lime/[0.08] px-4 py-3 flex items-center gap-3 text-sm';
    icon.innerHTML='<span class="block w-2 h-2 rounded-full bg-brand-lime shadow-[0_0_8px_rgba(0,229,122,0.7)]"></span>';
    text.textContent='Update'+(u.latest?(' v'+u.latest):'')+' downloaded';
    sub.textContent='Quit CodeBaton (⌘Q) and reopen to apply it.';
    sub.classList.remove('hidden');
  } else {
    b.classList.add('hidden'); return;
  }
  b.classList.remove('hidden');
}
async function pollUpdate(){
  try{ renderUpdate(await getJ('/update/status')); }catch(e){}
}
$('btn-check-update').addEventListener('click', async function(){
  const btn=this, orig=btn.textContent;
  btn.disabled=true; btn.textContent='Checking…';
  try{ await post('/update/check'); }catch(e){}
  await pollUpdate();
  btn.disabled=false; btn.textContent=orig;
});

// boot
poll(); refreshLogs(); go('view-overview'); pollUpdate();
setInterval(poll, 3000);
setInterval(pollUpdate, 4000);
setInterval(()=>{ if(active==='view-activity') refreshLogs(); }, 3000);
</script>
</body></html>
"""


# ─── HTTP server ───────────────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    backend = None  # bound in run_dashboard before serve_forever (None until signed in)
    org = None
    port = None
    backend_url = None
    pending = {}  # captured-but-not-finalized OAuth token during in-app sign-in

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
                if self.backend is None:
                    self._send_json(200, {"signed_in": False})
                    return
                snap = self.backend.dashboard()
                snap["org"] = self.org
                snap["daemon"] = _daemon_status()
                snap["signed_in"] = True
                self._send_json(200, snap)
                return
            if path == "/update/status":
                import importlib.metadata as _md
                try:
                    running = _md.version("teammate-sync")
                except Exception:
                    running = "?"
                p = Path("~/.teammate-sync/update-status.json").expanduser()
                data = {"state": "idle"}
                if p.exists():
                    try:
                        data = json.loads(p.read_text())
                    except (json.JSONDecodeError, OSError):
                        pass
                data["running"] = running
                self._send_json(200, data)
                return
            if path == "/auth/status":
                self._send_json(200, {
                    "signed_in": self.backend is not None,
                    "pending": bool(type(self).pending.get("token")),
                })
                return
            if path == "/auth/callback":
                # GitHub OAuth redirected here with the access token.
                token = (query.get("access_token") or [None])[0]
                if token:
                    type(self).pending["token"] = token
                    body = (
                        b"<!doctype html><html><body style=\"font-family:-apple-system,sans-serif;"
                        b"background:#0a0b0e;color:#e8e8ea;text-align:center;padding:4em 1em;\">"
                        b"<h2 style=\"color:#00E57A\">Signed in</h2>"
                        b"<p>Return to CodeBaton to choose your workspace. You can close this tab.</p>"
                        b"</body></html>"
                    )
                    self._send(200, body, "text/html; charset=utf-8")
                else:
                    self._send(400, b"missing access_token", "text/plain")
                return
            if path == "/auth/orgs":
                token = type(self).pending.get("token")
                if not token:
                    self._send_json(409, {"error": "no pending sign-in"})
                    return
                import httpx
                me = httpx.get(
                    f"{self.backend_url.rstrip('/')}/v1/me",
                    headers={"Authorization": f"Bearer {token}"}, timeout=15,
                )
                if me.status_code != 200:
                    self._send_json(502, {"error": f"/v1/me {me.status_code}"})
                    return
                o = httpx.get(
                    "https://api.github.com/user/orgs",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/vnd.github+json"}, timeout=15,
                )
                orgs = [x["login"] for x in o.json()] if o.status_code == 200 else []
                self._send_json(200, {"handle": me.json().get("github_handle"), "orgs": orgs})
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
                from . import cli
                self._send_json(200, {
                    "notifications_enabled": bool(prefs.get("enabled", True)),
                    "autostart_installed": _autostart_installed(),
                    "autostart_supported": sys.platform == "darwin",
                    "distill_enabled": cli.distill_enabled(),
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
            # ── in-app sign-in (works while signed out) ──
            if path == "/auth/start":
                type(self).pending.clear()
                redirect = f"http://127.0.0.1:{self.port}/auth/callback"
                login = (f"{self.backend_url.rstrip('/')}/auth/github/login"
                         f"?redirect_uri={urllib.parse.quote(redirect, safe='')}")
                webbrowser.open(login)
                self._send_json(200, {"ok": True})
                return
            if path == "/auth/finish":
                token = type(self).pending.get("token")
                org = (body.get("org") or "").strip()
                if not token or not org:
                    self._send_json(400, {"error": "missing token or org"})
                    return
                from . import cli
                handle = cli.finish_signin(token, org, self.backend_url)
                # daemon auto-start so the user is fully live
                subprocess.run([_resolve_self_binary(), "up"], capture_output=True, timeout=30)
                # rebuild the backend so the dashboard goes live without restart
                new_backend = _backend()
                type(self).backend = new_backend
                type(self).org = new_backend.org
                type(self).pending.clear()
                self._send_json(200, {"ok": True, "handle": handle, "org": org})
                return
            if path == "/update/check":
                target = str(Path("~/.teammate-sync/site-packages").expanduser())
                try:
                    binary = _resolve_self_binary()
                    subprocess.run([binary, "self-update", "--target", target],
                                   capture_output=True, timeout=120)
                except (OSError, subprocess.SubprocessError, RuntimeError):
                    pass
                p = Path("~/.teammate-sync/update-status.json").expanduser()
                data = {"state": "idle"}
                if p.exists():
                    try:
                        data = json.loads(p.read_text())
                    except (json.JSONDecodeError, OSError):
                        pass
                self._send_json(200, data)
                return
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
            if path == "/settings/distill":
                # On by default; opting out writes a disable marker.
                enabled = bool(body.get("enabled", True))
                flag = Path("~/.teammate-sync/distill.disabled").expanduser()
                if enabled:
                    flag.unlink(missing_ok=True)
                else:
                    flag.parent.mkdir(parents=True, exist_ok=True)
                    flag.touch()
                self._send_json(200, {"ok": True})
                return
            if path == "/unshare":
                sid = (body.get("session_id") or "").strip()
                if not sid:
                    self._send_json(400, {"error": "session_id required"})
                    return
                self._send_json(200, self.backend.unshare_session(sid))
                return
            if path == "/logout":
                # Stop the daemon, then delete the saved auth so the user can
                # re-onboard from scratch (re-watch the sign-in flow).
                subprocess.run([_resolve_self_binary(), "down"], capture_output=True, timeout=10)
                from .auth import auth_file_path
                p = auth_file_path()
                if p.exists():
                    p.unlink()
                # Drop the in-memory session so the SPA falls back to the
                # in-app sign-in screen on its next poll.
                type(self).backend = None
                type(self).org = None
                type(self).pending = {}
                self._send_json(200, {"ok": True})
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


def _start_http_server_in_thread(backend, port: int, backend_url: str) -> "socketserver.ThreadingTCPServer":
    handler_cls = _Handler
    handler_cls.backend = backend
    handler_cls.org = backend.org if backend is not None else None
    handler_cls.port = port
    handler_cls.backend_url = backend_url
    handler_cls.pending = {}
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _teardown_on_quit() -> None:
    """When the GUI is quit, tear everything down so each session starts clean:
    stop the sync engine, and nuke all connections + shares. Nothing lingers,
    so the user connects fresh every time — no autostart, no auto-sharing."""
    try:
        binary = _resolve_self_binary()
    except RuntimeError:
        return
    subprocess.run([binary, "down"], capture_output=True, timeout=10)
    subprocess.run([binary, "disconnect"], capture_output=True, timeout=20)


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
    # Start even when signed out — the SPA shows an in-app GitHub sign-in flow
    # that captures the token, lets you pick your org, wires Claude Code, and
    # brings the dashboard live without a terminal.
    from .auth import DEFAULT_BACKEND_URL
    try:
        backend = _backend()
        backend_url = backend.backend_url
    except (FileNotFoundError, ValueError):
        backend = None
        backend_url = DEFAULT_BACKEND_URL

    if port is None:
        port = _pick_free_port()
    url = f"http://127.0.0.1:{port}/"
    server = _start_http_server_in_thread(backend, port, backend_url)

    # On desktop-app launch, re-apply the Claude Code wiring with current code
    # so fixes (e.g. shell-quoting the binary path) self-heal via self-update.
    from . import cli
    cli.refresh_shell_wiring()

    if serve_only:
        # Machine-readable handshake for the Electron host, then block until the
        # app quits (Electron sends SIGTERM to this process on quit).
        print(json.dumps({"port": port, "url": url}), flush=True)
        import signal
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        try:
            while not stop.is_set():
                stop.wait(3600)
        except KeyboardInterrupt:
            pass
        _teardown_on_quit()
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
                "CodeBaton",
                url,
                width=1100,
                height=720,
                min_size=(880, 560),
                background_color="#0A0A0B",
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

    # GUI closed → tear down so the next session starts clean.
    _teardown_on_quit()
    server.shutdown()
    print("[dashboard] stopped.")
    return 0
