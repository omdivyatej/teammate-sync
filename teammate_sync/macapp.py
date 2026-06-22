"""
teammate-sync macOS menu bar app (v0.5, Stage 1).

Lightweight menu bar UI that wraps the existing `teammate-sync` CLI.
No logic is duplicated — every action shells out to the CLI binary on
PATH, so the app and the CLI stay in lockstep across upgrades.

Lifecycle:
    $ teammate-sync app
    → runs in the foreground, places an icon in the menu bar.
    → close terminal or quit-from-menu to stop.

For auto-start at login:
    $ teammate-sync app --install-launchagent
    → writes a LaunchAgent plist; macOS runs the app at every login.

Status icon (single char in the menu bar title):
    ●   daemon running AND at least one session is shared
    ○   daemon running but idle (no shares)
    —   daemon not running
    ?   not configured yet (auth.json missing)

Menu items:
    Status: <description>
    ----
    Start daemon          (calls `teammate-sync up`)
    Stop daemon           (calls `teammate-sync down`)
    ----
    Open dashboard...     (calls `teammate-sync dashboard`)
    Show daemon log...    (opens daemon.log in Console.app via `open`)
    ----
    Sign in / init...     (opens Terminal running `teammate-sync init`)
    Install auto-start    (writes LaunchAgent plist)
    Uninstall auto-start
    ----
    Quit teammate-sync app

Polls local state every 5s — pid file alive? shared-sessions count?
"""
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path


# Probe macOS + rumps availability lazily so this module imports on any
# platform (the CLI's `cmd_app` will print a clean error before reaching here
# on non-Mac).
if sys.platform != "darwin":
    raise RuntimeError("teammate-sync app is macOS-only.")

try:
    import rumps  # noqa
except ImportError as e:
    raise RuntimeError(
        "teammate-sync app needs `rumps`. Reinstall: `pipx reinstall teammate-sync`"
    ) from e


STATE_DIR = Path("~/.teammate-sync/state").expanduser()
AUTH_FILE = Path("~/.teammate-sync/auth.json").expanduser()
PID_FILE = STATE_DIR / "daemon.pid"
LOG_FILE = STATE_DIR / "daemon.log"
SHARED_FILE = STATE_DIR / ".shared-sessions.json"
LAUNCHAGENT_PATH = Path("~/Library/LaunchAgents/com.teammate-sync.app.plist").expanduser()
LAUNCHAGENT_LABEL = "com.teammate-sync.app"

POLL_SECONDS = 5.0          # local state poll (pid/shared count)
NOTIFY_POLL_SECONDS = 30.0  # backend poll for new connection events
NOTIFY_PREFS_FILE = STATE_DIR / ".notify-prefs.json"


def _binary() -> str:
    """
    Locate the teammate-sync binary. Prefers PATH lookup; falls back to
    the currently-running script's path (works when the app is launched
    by LaunchAgent with a minimal PATH).
    """
    found = shutil.which("teammate-sync")
    if found:
        return found
    # Fallback: we ARE teammate-sync — sys.argv[0] is our script path.
    candidate = Path(sys.argv[0]).resolve()
    if candidate.exists() and candidate.name == "teammate-sync":
        return str(candidate)
    raise RuntimeError("teammate-sync not on PATH and self-path lookup failed.")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _shared_count() -> int:
    """Count sessions in the local share registry."""
    if not SHARED_FILE.exists():
        return 0
    try:
        data = json.loads(SHARED_FILE.read_text())
        sessions = data.get("sessions") or []
        return sum(
            1 for s in sessions
            if isinstance(s, dict) and s.get("session_id") and (s.get("recipients") or [])
        )
    except (json.JSONDecodeError, OSError):
        return 0


def _status_summary() -> tuple[str, str]:
    """
    Returns (icon_char, status_text).
      icon: '●' active, '○' idle, '—' down, '?' unconfigured
      text: human-readable status line
    """
    if not AUTH_FILE.exists():
        return "?", "Not configured — sign in to start"

    pid = _read_pid()
    alive = pid is not None and _pid_alive(pid)
    if not alive:
        return "—", "Daemon stopped"

    shares = _shared_count()
    if shares == 0:
        return "○", f"Daemon running (idle — no sessions shared)"
    return "●", f"Daemon running — {shares} session(s) shared"


def _run_cli_silent(*args: str, timeout: float = 30.0) -> int:
    """Run a teammate-sync subcommand silently. Returns exit code."""
    try:
        res = subprocess.run(
            [_binary(), *args],
            capture_output=True,
            timeout=timeout,
        )
        return res.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 1


def _open_terminal_running(command: str) -> None:
    """Open Terminal.app and run `command` in a fresh window."""
    escaped = command.replace('"', '\\"')
    script = f'tell application "Terminal" to do script "{escaped}"'
    subprocess.Popen(["osascript", "-e", script])
    subprocess.Popen(["osascript", "-e", 'tell application "Terminal" to activate'])


# ──── notifications ────────────────────────────────────────────────────────

def _notify(title: str, subtitle: str, message: str) -> None:
    """
    Fire a macOS notification.

    Prefers `terminal-notifier` if installed (cleaner branding, can set
    custom sender bundle id). Falls back to `osascript` AppleScript
    `display notification` which works on every Mac out of the box but
    shows "Script Editor" as the source — mildly weird, functionally fine.

    Either path: macOS will prompt the user once to allow notifications
    for the source app, then remembers the decision.
    """
    if shutil.which("terminal-notifier"):
        subprocess.Popen(
            [
                "terminal-notifier",
                "-title", title,
                "-subtitle", subtitle,
                "-message", message,
                "-group", LAUNCHAGENT_LABEL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{esc(message)}" '
        f'with title "{esc(title)}" '
        f'subtitle "{esc(subtitle)}"'
    )
    subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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


def _fetch_connections() -> dict | None:
    """
    Hit /v1/connections on the backend. Returns the JSON dict, or None if
    not configured / network failed / backend rejected.
    """
    try:
        import httpx
        from .auth import read_auth
    except (ImportError, ModuleNotFoundError):
        return None
    try:
        auth = read_auth()
    except (FileNotFoundError, ValueError):
        return None
    try:
        r = httpx.get(
            f"{auth['backend_url'].rstrip('/')}/v1/connections",
            params={"org": auth["org"]},
            headers={"Authorization": f"Bearer {auth['token']}"},
            timeout=5.0,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _launchagent_plist() -> str:
    """Render the LaunchAgent plist content for auto-start at login."""
    binary = _binary()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHAGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary}</string>
        <string>app</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>{Path("~/.teammate-sync/state/app.log").expanduser()}</string>
    <key>StandardErrorPath</key>
    <string>{Path("~/.teammate-sync/state/app.log").expanduser()}</string>
</dict>
</plist>
"""


class TeammateSyncApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(
            "teammate-sync",
            title="?",  # set properly on first poll
            quit_button=None,  # we provide our own
        )
        self.status_item = rumps.MenuItem("Status: starting up...")
        self.start_item = rumps.MenuItem("Start daemon", callback=self.on_start)
        self.stop_item = rumps.MenuItem("Stop daemon", callback=self.on_stop)
        self.dashboard_item = rumps.MenuItem("Open dashboard...", callback=self.on_dashboard)
        self.logs_item = rumps.MenuItem("Show daemon log...", callback=self.on_logs)
        self.init_item = rumps.MenuItem("Sign in / init...", callback=self.on_init)
        self.install_la_item = rumps.MenuItem(
            "Install auto-start at login", callback=self.on_install_launchagent
        )
        self.uninstall_la_item = rumps.MenuItem(
            "Remove auto-start", callback=self.on_uninstall_launchagent
        )
        self.notify_item = rumps.MenuItem(
            "Notifications: on", callback=self.on_toggle_notifications
        )
        self.quit_item = rumps.MenuItem("Quit teammate-sync app", callback=self.on_quit)

        self.menu = [
            self.status_item,
            None,
            self.start_item,
            self.stop_item,
            None,
            self.dashboard_item,
            self.logs_item,
            None,
            self.init_item,
            self.install_la_item,
            self.uninstall_la_item,
            None,
            self.notify_item,
            self.quit_item,
        ]

        # Notification state — track previous backend connection snapshot
        # so we only notify about NEW events (not pre-existing ones).
        self._last_pending_in: set[str] | None = None
        self._last_accepted: set[str] | None = None
        prefs = _read_notify_prefs()
        self._notifications_enabled = bool(prefs.get("enabled", True))
        self._refresh_notify_label()

        # Initial poll + recurring poll
        self._poll()
        self.poll_timer = rumps.Timer(self._on_tick, POLL_SECONDS)
        self.poll_timer.start()
        # Backend connection poll on its own (slower) cadence
        self.notify_timer = rumps.Timer(self._on_notify_tick, NOTIFY_POLL_SECONDS)
        self.notify_timer.start()

    # ──── status polling ────────────────────────────────────────────────

    def _on_tick(self, _: object) -> None:
        # Run poll on a worker thread so menu UI stays responsive even if
        # the pid_alive / shared_count checks block briefly on disk.
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self) -> None:
        try:
            icon, text = _status_summary()
            self.title = icon
            self.status_item.title = f"Status: {text}"
            # Toggle button enabled states based on daemon state
            daemon_up = icon in ("●", "○")
            self.start_item.set_callback(None if daemon_up else self.on_start)
            self.stop_item.set_callback(self.on_stop if daemon_up else None)
            # Enable launchagent items based on plist presence
            la_present = LAUNCHAGENT_PATH.exists()
            self.install_la_item.set_callback(None if la_present else self.on_install_launchagent)
            self.uninstall_la_item.set_callback(self.on_uninstall_launchagent if la_present else None)
        except Exception as e:
            self.title = "!"
            self.status_item.title = f"Status: error: {e}"

    # ──── menu actions ──────────────────────────────────────────────────

    def on_start(self, _: object) -> None:
        rc = _run_cli_silent("up")
        if rc != 0:
            rumps.alert(
                title="teammate-sync",
                message=f"Could not start daemon (exit {rc}). Check `teammate-sync logs`.",
            )
        self._poll()

    def on_stop(self, _: object) -> None:
        _run_cli_silent("down")
        self._poll()

    def on_dashboard(self, _: object) -> None:
        # The CLI's `dashboard` command picks a free port and opens the
        # browser automatically. We just background it and forget.
        subprocess.Popen(
            [_binary(), "dashboard"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def on_logs(self, _: object) -> None:
        if not LOG_FILE.exists():
            rumps.alert(
                title="teammate-sync",
                message="No daemon log yet. Start the daemon first.",
            )
            return
        # `open -a Console` opens the file in Console.app, which has a nice
        # live-tail view. Falls back to default app if Console isn't there.
        subprocess.Popen(["open", "-a", "Console", str(LOG_FILE)])

    def on_init(self, _: object) -> None:
        # `init` is interactive (browser OAuth, org picker). Run in Terminal.
        _open_terminal_running("teammate-sync init")

    def on_install_launchagent(self, _: object) -> None:
        LAUNCHAGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAUNCHAGENT_PATH.write_text(_launchagent_plist())
        # Load it so it starts immediately AND on next login.
        subprocess.run(["launchctl", "unload", str(LAUNCHAGENT_PATH)], capture_output=True)
        rc = subprocess.run(
            ["launchctl", "load", "-w", str(LAUNCHAGENT_PATH)],
            capture_output=True,
        ).returncode
        if rc == 0:
            rumps.alert(
                title="teammate-sync",
                message="Auto-start installed. The app will launch at every login.",
            )
        else:
            rumps.alert(
                title="teammate-sync",
                message=f"Wrote plist, but launchctl load failed (exit {rc}).",
            )
        self._poll()

    def on_uninstall_launchagent(self, _: object) -> None:
        if LAUNCHAGENT_PATH.exists():
            subprocess.run(
                ["launchctl", "unload", "-w", str(LAUNCHAGENT_PATH)],
                capture_output=True,
            )
            LAUNCHAGENT_PATH.unlink()
            rumps.alert(
                title="teammate-sync",
                message="Auto-start removed. The app will no longer launch at login.",
            )
        self._poll()

    def on_toggle_notifications(self, _: object) -> None:
        self._notifications_enabled = not self._notifications_enabled
        _write_notify_prefs({"enabled": self._notifications_enabled})
        self._refresh_notify_label()
        if self._notifications_enabled:
            _notify(
                title="teammate-sync",
                subtitle="Notifications enabled",
                message="You'll see a pop-up when a teammate wants to connect or accepts your invite.",
            )

    def _refresh_notify_label(self) -> None:
        self.notify_item.title = (
            "Notifications: on" if self._notifications_enabled else "Notifications: off"
        )

    def on_quit(self, _: object) -> None:
        rumps.quit_application()

    # ──── backend polling for connection events ─────────────────────────

    def _on_notify_tick(self, _: object) -> None:
        threading.Thread(target=self._poll_connections, daemon=True).start()

    def _poll_connections(self) -> None:
        """
        Fetch /v1/connections, diff against previous snapshot, fire
        notifications on:
          - new pending_incoming entries (someone wants to connect with you)
          - new accepted entries (a /connect you sent got accepted)

        First poll just records the baseline — we don't fire notifications
        for pre-existing state when the app boots, to avoid spam after a
        restart with old pending invites.
        """
        data = _fetch_connections()
        if data is None:
            return

        pending_in = {c["peer_handle"] for c in data.get("pending_incoming", [])}
        accepted = {c["peer_handle"] for c in data.get("accepted", [])}

        if self._last_pending_in is not None and self._notifications_enabled:
            for handle in sorted(pending_in - self._last_pending_in):
                _notify(
                    title="teammate-sync",
                    subtitle="Connection request",
                    message=f"{handle} wants to connect. In Claude Code, run /connect {handle} to share back.",
                )
            if self._last_accepted is not None:
                for handle in sorted(accepted - self._last_accepted):
                    _notify(
                        title="teammate-sync",
                        subtitle="Connection accepted",
                        message=f"{handle} accepted your invite. Their context is now visible to you via /ask.",
                    )

        self._last_pending_in = pending_in
        self._last_accepted = accepted


def run() -> int:
    TeammateSyncApp().run()
    return 0


def install_launchagent_only() -> int:
    """Headless install — called by `teammate-sync app --install-launchagent`."""
    LAUNCHAGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHAGENT_PATH.write_text(_launchagent_plist())
    subprocess.run(["launchctl", "unload", str(LAUNCHAGENT_PATH)], capture_output=True)
    rc = subprocess.run(
        ["launchctl", "load", "-w", str(LAUNCHAGENT_PATH)],
        capture_output=True,
        text=True,
    )
    if rc.returncode != 0:
        print(f"launchctl load failed: {rc.stderr}", file=sys.stderr)
        return rc.returncode
    print(f"✓ LaunchAgent installed at {LAUNCHAGENT_PATH}")
    print(f"  The menu bar app will start at every login.")
    print(f"  To remove: teammate-sync app --uninstall-launchagent")
    return 0


def uninstall_launchagent_only() -> int:
    """Headless uninstall."""
    if not LAUNCHAGENT_PATH.exists():
        print("LaunchAgent not installed; nothing to do.")
        return 0
    subprocess.run(["launchctl", "unload", "-w", str(LAUNCHAGENT_PATH)], capture_output=True)
    LAUNCHAGENT_PATH.unlink()
    print(f"✓ Removed {LAUNCHAGENT_PATH}")
    return 0
