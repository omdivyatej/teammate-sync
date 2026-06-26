#!/usr/bin/env python3
"""
teammate-sync CLI.

Subcommands:
    init        Sign in with GitHub and configure your workspace.
                Opens a browser, captures the OAuth token via a one-shot
                localhost listener, lets you pick which GitHub org is
                your workspace, writes ~/.teammate-sync/auth.json.

    whoami      Show identity from your saved auth (validates token).

    teammates   List teammates in your configured workspace.

    logout      Delete your saved auth file.

Designed to be invoked via the small `teammate-sync` bash wrapper next to
this file, but `python cli.py <cmd>` also works.
"""
import argparse
import http.server
import json
import socketserver
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

from .auth import DEFAULT_BACKEND_URL, auth_file_path, read_auth, write_auth


CALLBACK_TIMEOUT_SECONDS = 180


_SHELL_SAFE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./-"
)


def _stable_binary(binary: str) -> str:
    """Return a shell-safe path to the binary.

    The desktop app's binary lives under ~/Library/Application Support/... —
    that path has a SPACE, which the shell splits when the binary is used
    unquoted in a hook command or slash command (`/bin/sh: .../Application:
    No such file`). And a user's home path could contain other awkward
    characters too (spaces, parens, `&`, …).

    Whenever the path isn't plainly shell-safe, write a wrapper at the
    guaranteed-clean, home-relative path ~/.teammate-sync/bin/teammate-sync
    (home short-names can't contain spaces on macOS/Linux) and use that. The
    wrapper quotes and execs the real binary, so any content in the original
    path — one space or several — is handled."""
    if all(c in _SHELL_SAFE_CHARS for c in binary):
        return binary
    bindir = Path("~/.teammate-sync/bin").expanduser()
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / "teammate-sync"
    wrapper.write_text(f'#!/bin/sh\nexec "{binary}" "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


def _install_hooks_into_claude_settings(binary: str) -> Path:
    """
    Merge SessionStart / PostToolUse / SessionEnd hooks into ~/.claude/settings.json,
    preserving any other settings already there.

    All three hooks dispatch through the installed `teammate-sync` binary —
    no path-to-a-checkout baked in. Hooks write to ~/.teammate-sync/state/.
    """
    settings_path = Path("~/.claude/settings.json").expanduser()
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    for event, op in [("SessionStart", "start"),
                      ("PostToolUse", "heartbeat"),
                      ("SessionEnd", "end")]:
        cmd = f'"{binary}" hook {op}'
        hooks[event] = [{"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}]

    settings_path.write_text(json.dumps(settings, indent=2))
    return settings_path


def _resolve_claude_binary() -> str:
    """Locate the `claude` CLI by absolute path.

    Finder-launched GUI apps (the .dmg) start with a minimal PATH
    (/usr/bin:/bin:/usr/sbin:/sbin) that omits the user's shell PATH, so
    `claude` can't be found by name. Search the common install locations too."""
    import shutil
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".claude/local/claude",
        home / ".local/bin/claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
        home / ".npm-global/bin/claude",
        home / ".bun/bin/claude",
        home / ".volta/bin/claude",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    raise RuntimeError(
        "Could not find the `claude` CLI. Install Claude Code, then sign in again."
    )


def _register_mcp(binary: str) -> bool:
    """Register the MCP server via `claude mcp add`. Returns True on success.

    The server self-loads the Anthropic key from ~/.teammate-sync/auth.json
    at launch — no env var is passed at registration time, so the user
    never has to manage ANTHROPIC_API_KEY in their shell."""
    import os
    import subprocess as _sp

    claude = _resolve_claude_binary()

    # GUI apps inherit a minimal PATH; give claude (and anything it spawns) the
    # usual user bin dirs so it runs the same as it does from a terminal.
    home = Path.home()
    env = os.environ.copy()
    extra = ":".join([
        "/opt/homebrew/bin", "/usr/local/bin",
        str(home / ".claude/local"), str(home / ".local/bin"),
    ])
    env["PATH"] = extra + ":" + env.get("PATH", "")

    # remove any existing registration first (idempotent)
    _sp.run(
        [claude, "mcp", "remove", "teammate-sync", "--scope", "user"],
        capture_output=True,
        env=env,
    )

    result = _sp.run(
        [
            claude, "mcp", "add",
            "--scope", "user",
            "teammate-sync",
            "--",
            binary, "mcp-server",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        print(f"  ⚠️  claude mcp add failed: {result.stderr.strip()}")
        return False
    return True


def cmd_init(args) -> int:
    binary = _resolve_self_binary()
    backend_url = args.backend_url.rstrip("/")
    captured: dict[str, str | None] = {"token": None}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            token = params.get("access_token", [None])[0]
            if token:
                captured["token"] = token
                body = (
                    b"<!doctype html><html><body style=\"font-family:-apple-system,sans-serif;"
                    b"max-width:540px;margin:4em auto;padding:1em;\">"
                    b"<h2>teammate-sync &mdash; signed in</h2>"
                    b"<p>You can close this tab and return to your terminal.</p>"
                    b"</body></html>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing access_token")

        def log_message(self, *args):
            pass  # silence the default per-request stderr line

    # Open a localhost listener on any free port; tell the backend to redirect there.
    with socketserver.TCPServer(("127.0.0.1", 0), CallbackHandler) as server:
        port = server.server_address[1]
        callback_uri = f"http://127.0.0.1:{port}/callback"
        login_url = (
            f"{backend_url}/auth/github/login"
            f"?redirect_uri={urllib.parse.quote(callback_uri, safe='')}"
        )

        print("Opening browser to sign in with GitHub...")
        print(f"  If it doesn't open, visit: {login_url}")
        try:
            webbrowser.open(login_url)
        except Exception:
            pass

        print(f"  Waiting for authorization (up to {CALLBACK_TIMEOUT_SECONDS}s)...")
        server.timeout = 1
        deadline = time.time() + CALLBACK_TIMEOUT_SECONDS
        while captured["token"] is None and time.time() < deadline:
            server.handle_request()

    if not captured["token"]:
        print("\nTimed out waiting for GitHub OAuth callback.")
        return 1

    token = captured["token"]

    # Confirm identity via backend.
    r = httpx.get(
        f"{backend_url}/v1/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"Backend rejected the token ({r.status_code}): {r.text}")
        return 1
    me = r.json()
    print(f"\n✓ Signed in as {me['github_handle']} <{me.get('email') or '(email private)'}>")

    # List the user's GitHub orgs to let them pick a workspace.
    o = httpx.get(
        "https://api.github.com/user/orgs",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    if o.status_code != 200:
        print(f"Failed to list your GitHub orgs ({o.status_code}). Aborting.")
        return 1
    orgs = [item["login"] for item in o.json()]

    if not orgs:
        print(
            "\nNo GitHub orgs visible. The OAuth app may need approval per-org.\n"
            "Visit https://github.com/settings/applications and grant 'teammate-sync'\n"
            "access to your org, then re-run `teammate-sync init`."
        )
        return 1

    print("\nWhich GitHub org is your workspace?")
    for i, name in enumerate(orgs, 1):
        print(f"  {i}. {name}")

    while True:
        choice = input(f"\nEnter 1-{len(orgs)} (or the org name): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(orgs):
            org = orgs[int(choice) - 1]
            break
        if choice in orgs:
            org = choice
            break
        print("Invalid choice, try again.")

    # v0.4: no Anthropic key required anymore. The MCP server no longer
    # synthesizes — it just hands raw context to the host Claude. We
    # preserve any existing key in auth.json from older versions (harmless).
    path = write_auth(token=token, org=org, backend_url=backend_url,
                      github_handle=me.get("github_handle"))
    print(f"\n✓ Saved {path} (mode 0600)")
    print(f"  GitHub:    {me['github_handle']}")
    print(f"  Workspace: {org}")
    print(f"  Backend:   {backend_url}")

    # --- Slash commands + hooks + MCP ---------------------------------------
    print("\nWiring Claude Code integration (slash commands, hooks, MCP server)...")
    _wire_claude_integration(binary)
    print("  ✓ /connect /disconnect /shared /ask installed; hooks + MCP registered")

    # --- Done ----------------------------------------------------------------
    print("\n" + "=" * 60)
    print("✓ Setup complete.")
    print("=" * 60)
    print(f"\nTwo more things you do once:")
    print(f"  1. Start the daemon in a terminal you keep open:")
    print(f"       teammate-sync daemon")
    print(f"  2. Restart any open Claude Code sessions so they pick up the hooks + MCP.")
    print(f"\nThen from any Claude Code session, anywhere, type /share to share that")
    print(f"session's transcript with your teammates. /unshare to revoke.")
    return 0


def cmd_whoami(args) -> int:
    try:
        auth = read_auth()
    except (FileNotFoundError, ValueError) as e:
        print(str(e))
        return 1
    r = httpx.get(
        f"{auth['backend_url'].rstrip('/')}/v1/me",
        headers={"Authorization": f"Bearer {auth['token']}"},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"Saved token is invalid ({r.status_code}). Re-run `teammate-sync init`.")
        return 1
    me = r.json()
    print(f"GitHub handle: {me['github_handle']}")
    print(f"Email:         {me.get('email') or '(email private)'}")
    print(f"Workspace:     {auth['org']}")
    print(f"Backend:       {auth['backend_url']}")
    print(f"Auth file:     {auth_file_path()}")
    return 0


def cmd_teammates(args) -> int:
    from . import share_cli
    return share_cli.cmd_teammates()


# v0.3 slash command surface — four commands total.
# /ask is special: it doesn't shell out to the binary; instead the .md tells
# Claude Code to call the MCP tool directly with parsed args.
SLASH_COMMAND_SPECS = {
    "connect": {
        "subcmd": "connect",
        "args_hint": "[github-handle ...]",
        "description": (
            "Share THIS Claude Code session with one or more teammates. "
            "No args lists workspace members + their current status."
        ),
    },
    "disconnect": {
        "subcmd": "disconnect",
        "args_hint": "[github-handle]",
        "description": (
            "Remove trust. No args = nuke everything (every connection + this session's shares). "
            "Pass a handle to disconnect just that one teammate."
        ),
    },
    "shared": {
        "subcmd": "shared",
        "args_hint": "",
        "description": "Audit which sessions are currently shareable and with whom.",
    },
    "alias": {
        "subcmd": "alias",
        "args_hint": "[name github-handle]",
        "description": (
            "Give a teammate a short local nickname so /ask is easy to type "
            "(e.g. `/alias om om-divyatej`, then `/ask om ...`). No args lists "
            "your aliases; `--rm <name>` removes one."
        ),
    },
}

# Slash commands no longer in v0.3 — install-commands deletes these files if
# present so users upgrading from v0.2 don't end up with stale half-broken
# commands lying around in ~/.claude/commands/.
_RETIRED_SLASH_COMMANDS = {
    "share", "unshare", "connections", "accept", "decline", "teammates", "show",
}


_ASK_SLASH_MD = """---
description: Ask a teammate's LIVE Claude session a question — their Claude answers from their real session; you get just the answer.
argument-hint: "<handle-or-alias> <question>"
---

The user wants to ask ONE teammate a live question. Parse the argument:
  - first whitespace-separated token = the teammate's handle or local alias
  - everything after = the question text

Call the MCP tool `mcp__teammate-sync__ask_teammate_live` with
`teammate` = that handle/alias (pass it as-is; the tool resolves aliases) and
`question` = the rest. The teammate's OWN Claude answers it from their live
session on their machine — their raw transcript never leaves their device; you
receive only the answer. This can take a few seconds to tens of seconds.

Then present the returned text to the user as-is — it is ALREADY the answer,
not raw context to reason over. Do not add preamble or restate the question.
  - If the tool notes the teammate was offline and shows their recorded
    decisions, present those and make clear they're recorded, not live.
  - If it says exactly 'Not found in shared context.', say exactly that.

The user's argument is: $ARGUMENTS
"""


_ASK_ALL_SLASH_MD = """\
---
description: Ask the whole team a question — searches everyone's accumulated decision knowledge (works even when teammates are offline).
argument-hint: "<question>"
---

The user is asking the WHOLE TEAM, not one person. Call the MCP tool
`mcp__teammate-sync__query_team_knowledge` (no arguments). It returns every
engineer's distilled decision log (knowledge.md) from the org's durable store
— so it works even when teammates are offline.

Then YOU read it and answer the user's question using ONLY that content. Keep
it tight:
  - Lead with the answer in 1–3 sentences. No preamble.
  - Cite the engineer and, when present, the date/time of the decision —
    e.g. "(@nikhil, 2026-06-25 14:30)". Prefer the NEWEST entry when decisions
    evolved over time, and mention if it superseded an earlier one.
  - If the answer isn't there, say exactly: "Not found in shared context."
  - Don't speculate beyond what's written.

For what a specific person is doing RIGHT NOW, that's `/ask <teammate>` (the
live view) instead — this command is for accumulated team decisions.

The user's question is: $ARGUMENTS
"""


def _slash_command_md(action: str, binary: str) -> str:
    """
    Generate a slash-command markdown file. Most slash commands shell out to
    the installed `teammate-sync` binary; /ask + /ask-all are special and tell
    Claude to call an MCP tool directly.
    """
    if action == "ask":
        return _ASK_SLASH_MD
    if action == "ask-all":
        return _ASK_ALL_SLASH_MD

    spec = SLASH_COMMAND_SPECS[action]
    hint_yaml = f'argument-hint: "{spec["args_hint"]}"\n' if spec["args_hint"] else ""
    return f"""---
description: {spec["description"]}
{hint_yaml}allowed-tools: Bash({binary}:*)
---

Execute this command via the Bash tool and show its full stdout output to
the user verbatim:

```
"{binary}" {spec["subcmd"]} $ARGUMENTS
```

The CLAUDE_CODE_SESSION_ID and CLAUDE_PROJECT_DIR env vars are set by
Claude Code in the Bash subprocess — the underlying command reads them
when no explicit argument is given.

After showing the output, do NOT add commentary.
"""


# Order matters for the install summary print: list in the order they'd
# logically be used.
_INSTALL_ACTIONS = ["connect", "disconnect", "shared", "alias", "ask", "ask-all"]


def _wire_claude_integration(binary: str) -> None:
    """
    Install the v0.3 slash commands (cleaning up any retired ones), merge the
    session hooks into ~/.claude/settings.json, and register the MCP server.
    Idempotent. Shared by `teammate-sync init` and the in-app (dashboard)
    sign-in flow so both wire Claude Code up identically.
    """
    binary = _stable_binary(binary)
    commands_dir = Path("~/.claude/commands").expanduser()
    commands_dir.mkdir(parents=True, exist_ok=True)
    for retired in _RETIRED_SLASH_COMMANDS:
        (commands_dir / f"{retired}.md").unlink(missing_ok=True)
    for action in _INSTALL_ACTIONS:
        (commands_dir / f"{action}.md").write_text(_slash_command_md(action, binary))
    _install_hooks_into_claude_settings(binary)
    _register_mcp(binary)


def refresh_shell_wiring() -> None:
    """Rewrite the hook + slash-command wiring with the current code.

    Lets fixes to the wiring (e.g. shell-quoting the binary path) propagate
    on desktop-app launch via self-update — no re-sign-in needed. No-op unless
    the desktop app set TEAMMATE_SYNC_BIN. MCP isn't touched (it uses an argv
    array, so it never had the shell-splitting problem)."""
    import os
    binary = os.environ.get("TEAMMATE_SYNC_BIN")
    if not binary:
        return
    binary = _stable_binary(binary)
    commands_dir = Path("~/.claude/commands").expanduser()
    commands_dir.mkdir(parents=True, exist_ok=True)
    for retired in _RETIRED_SLASH_COMMANDS:
        (commands_dir / f"{retired}.md").unlink(missing_ok=True)
    for action in _INSTALL_ACTIONS:
        (commands_dir / f"{action}.md").write_text(_slash_command_md(action, binary))
    _install_hooks_into_claude_settings(binary)


def finish_signin(token: str, org: str, backend_url: str) -> str:
    """
    Persist a captured GitHub token + chosen org, then wire Claude Code.
    Returns the verified GitHub handle. Used by the dashboard's in-app
    sign-in (no terminal, no interactive org prompt).
    """
    r = httpx.get(
        f"{backend_url.rstrip('/')}/v1/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    r.raise_for_status()
    handle = r.json()["github_handle"]
    write_auth(token=token, org=org, backend_url=backend_url, github_handle=handle)
    try:
        binary = _resolve_self_binary()
    except RuntimeError:
        binary = "teammate-sync"
    _wire_claude_integration(binary)
    return handle


def _resolve_self_binary() -> str:
    """
    Find where the `teammate-sync` binary lives.
    Hooks, MCP, and slash commands all dispatch through it.

    Honors $TEAMMATE_SYNC_BIN first — the Electron desktop app sets this to
    a shim that execs the bundled Python, so a system-wide pipx install
    isn't required for the bundled-app flow.
    """
    import os as _os
    import shutil
    env_bin = _os.environ.get("TEAMMATE_SYNC_BIN")
    if env_bin:
        return env_bin
    found = shutil.which("teammate-sync")
    if not found:
        raise RuntimeError(
            "Could not locate the `teammate-sync` binary on PATH. "
            "Reinstall with `pip install teammate-sync` and ensure the "
            "Python scripts directory is on your PATH."
        )
    return found


def cmd_install_commands(args) -> int:
    """Install all v0.3 slash commands into ~/.claude/commands/. Also clean up
    any stale v0.2 slash commands so users don't end up with broken /share,
    /unshare, /accept, etc."""
    binary = _resolve_self_binary()
    commands_dir = Path("~/.claude/commands").expanduser()
    commands_dir.mkdir(parents=True, exist_ok=True)

    # Clean up retired v0.2 slash commands
    cleaned = 0
    for retired in _RETIRED_SLASH_COMMANDS:
        old_path = commands_dir / f"{retired}.md"
        if old_path.exists():
            old_path.unlink()
            cleaned += 1
            print(f"  removed stale /{retired} (no longer in v0.3)")

    # Write the v0.3 set
    for action in _INSTALL_ACTIONS:
        path = commands_dir / f"{action}.md"
        path.write_text(_slash_command_md(action, binary))
        print(f"  wrote {path}")

    print()
    print(f"✓ Installed slash commands into {commands_dir}:")
    print(f"  {', '.join('/' + a for a in _INSTALL_ACTIONS)}")
    if cleaned:
        print(f"  ({cleaned} retired v0.2 command(s) removed)")
    print(f"  Pointing at: {binary}")
    print()
    print("Restart any open Claude Code sessions to pick them up.")
    return 0


def cmd_connect(args) -> int:
    """No args → list workspace + status. With args → share this session with handles."""
    from . import share_cli
    if not args.recipients:
        return share_cli.cmd_connect_list()
    return share_cli.cmd_share(args.recipients)


def cmd_disconnect(args) -> int:
    """No arg → nuke all trust + wipe local shares. With arg → remove that one peer."""
    from . import share_cli
    return share_cli.cmd_disconnect(args.handle)


def cmd_dashboard(args) -> int:
    from . import dashboard as _dashboard
    use_window = None
    if args.browser or args.serve_only:
        use_window = False
    return _dashboard.run_dashboard(
        port=args.port,
        open_browser=not args.no_browser,
        use_window=use_window,
        serve_only=args.serve_only,
    )


def cmd_shared(args) -> int:
    from . import share_cli
    return share_cli.cmd_list()


def cmd_alias(args) -> int:
    """Manage local nicknames for teammates so /ask is easy to type."""
    from . import aliases
    if args.rm:
        name = args.rm.strip().lower()
        if aliases.remove_alias(name):
            print(f"Removed alias '{name}'.")
            return 0
        print(f"No alias named '{name}'.")
        return 1
    if not args.name:
        current = aliases.read_aliases()
        if not current:
            print("No aliases yet. Set one:  teammate-sync alias om om-divyatej")
            return 0
        print("Aliases:")
        for name, handle in sorted(current.items()):
            print(f"  {name} -> {handle}")
        return 0
    if not args.handle:
        print("Usage: teammate-sync alias <name> <github-handle>")
        return 1
    name = args.name.strip().lower()
    handle = args.handle.strip()

    # Validate the handle is a real member of your workspace, so typos are
    # caught now rather than as an empty /ask later. Match case-insensitively
    # and store the workspace's canonical casing.
    from . import share_cli
    try:
        members = share_cli.workspace_handles()
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    canonical = next((h for h in members if h.lower() == handle.lower()), None)
    if canonical is None:
        print(f"'{handle}' isn't a member of your workspace. Members:")
        for h in sorted(members):
            print(f"  - {h}")
        return 1

    aliases.set_alias(name, canonical)
    print(f"Alias set: {name} -> {canonical}   (use it: /ask {name} <question>)")
    return 0


def cmd_daemon(args) -> int:
    from . import daemon as _daemon
    # daemon's main() reads sys.argv; replace it so the global state dirs are used
    sys.argv = ["teammate-sync-daemon"] + (args.extra or [])
    return _daemon.main()


# ─── teammate-sync up / down / logs ────────────────────────────────────────

def _state_dir() -> Path:
    return Path("~/.teammate-sync/state").expanduser()


def _pid_file() -> Path:
    return _state_dir() / "daemon.pid"


def _log_file() -> Path:
    return _state_dir() / "daemon.log"


def _pid_alive(pid: int) -> bool:
    """Cheap check: signal 0 raises if process doesn't exist."""
    import os as _os
    try:
        _os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _read_pid() -> int | None:
    p = _pid_file()
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def cmd_up(args) -> int:
    """Spawn the daemon in the background, write PID, redirect logs to a file."""
    import subprocess as _sp
    import time as _time

    _state_dir().mkdir(parents=True, exist_ok=True)

    existing = _read_pid()
    if existing and _pid_alive(existing):
        print(f"daemon already running (pid {existing}).")
        print(f"logs:  teammate-sync logs")
        print(f"stop:  teammate-sync down")
        return 0
    if existing:
        # Stale pidfile — drop it.
        _pid_file().unlink(missing_ok=True)

    binary = _resolve_self_binary()
    log = _log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log, "a")
    log_handle.write(f"\n--- daemon up at {_iso_now()} ---\n")
    log_handle.flush()

    # start_new_session detaches from this terminal's process group so the
    # daemon survives terminal close + isn't a Job under shell control.
    proc = _sp.Popen(
        [binary, "daemon"],
        stdout=log_handle,
        stderr=_sp.STDOUT,
        stdin=_sp.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    _pid_file().write_text(str(proc.pid))

    # Give it ~1.5s to boot, then verify it's still alive.
    _time.sleep(1.5)
    if not _pid_alive(proc.pid):
        print(f"daemon failed to start. Last log lines:", file=sys.stderr)
        try:
            print(_log_file().read_text()[-2000:], file=sys.stderr)
        except OSError:
            pass
        _pid_file().unlink(missing_ok=True)
        return 1

    print(f"✓ daemon up (pid {proc.pid})")
    print(f"  logs:      teammate-sync logs")
    print(f"  dashboard: teammate-sync dashboard")
    print(f"  stop:      teammate-sync down")
    return 0


def cmd_down(args) -> int:
    """Send TERM to the backgrounded daemon, escalate to KILL if needed."""
    import os as _os
    import signal as _signal
    import time as _time

    pid = _read_pid()
    if pid is None:
        print("daemon not running (no pidfile).")
        return 0
    if not _pid_alive(pid):
        print(f"daemon not running (stale pidfile for pid {pid}; cleaning up).")
        _pid_file().unlink(missing_ok=True)
        return 0

    try:
        _os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        _pid_file().unlink(missing_ok=True)
        print("daemon already gone.")
        return 0

    # Wait up to 5 seconds for clean shutdown.
    for _ in range(50):
        if not _pid_alive(pid):
            break
        _time.sleep(0.1)
    else:
        # Escalate.
        try:
            _os.kill(pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass

    _pid_file().unlink(missing_ok=True)
    print(f"✓ daemon stopped (pid {pid}).")
    return 0


def cmd_upgrade(args) -> int:
    """
    One-shot upgrade: stop daemon, pipx upgrade teammate-sync, reinstall
    slash commands (cleans retired ones), restart daemon. Equivalent to:

        teammate-sync down
        pipx upgrade teammate-sync
        teammate-sync install-commands
        teammate-sync up

    Works because after `pipx upgrade` the new binary lives at the same
    path; we shell out to it for install-commands and up so the latest
    code runs those steps (the current process is still the old code,
    held in memory).
    """
    import shutil
    import subprocess as _sp

    if not shutil.which("pipx"):
        print("Error: pipx not on PATH. Manual upgrade:", file=sys.stderr)
        print("  teammate-sync down", file=sys.stderr)
        print("  pip install -U teammate-sync  # or pipx upgrade if you can install it", file=sys.stderr)
        print("  teammate-sync install-commands", file=sys.stderr)
        print("  teammate-sync up", file=sys.stderr)
        return 1

    print("→ Stopping daemon...")
    cmd_down(args)

    print("\n→ Upgrading via pipx...")
    res = _sp.run(["pipx", "upgrade", "teammate-sync"])
    if res.returncode != 0:
        print("pipx upgrade failed. Daemon is stopped; restart with `teammate-sync up` once you fix the install.",
              file=sys.stderr)
        return res.returncode

    binary = shutil.which("teammate-sync")
    if not binary:
        print("Error: teammate-sync no longer on PATH after upgrade.", file=sys.stderr)
        return 1

    print("\n→ Refreshing slash commands (will clean up any retired ones)...")
    res = _sp.run([binary, "install-commands"])
    if res.returncode != 0:
        return res.returncode

    print("\n→ Starting daemon...")
    res = _sp.run([binary, "up"])
    if res.returncode != 0:
        return res.returncode

    print()
    print("✓ Upgrade complete.")
    print("  Restart any open Claude Code sessions to pick up new hooks + MCP + slash commands.")
    return 0


def _ver_tuple(v: str) -> tuple:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def _update_status_path() -> Path:
    return Path("~/.teammate-sync/update-status.json").expanduser()


def _write_update_status(**fields) -> None:
    """Publish the current update stage so the dashboard can show a banner.
    States: checking | uptodate | downloading | ready | error | offline."""
    import time
    p = _update_status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fields["ts"] = time.time()
    p.write_text(json.dumps(fields))


def cmd_self_update(args) -> int:
    """Install the latest teammate-sync from PyPI into a user-writable dir so
    the desktop app updates without re-downloading the .dmg.

    The app puts --target on PYTHONPATH ahead of the bundled package, so on the
    next launch the daemon, MCP server, hooks, and dashboard all run new code.
    Progress is published to update-status.json for the dashboard banner."""
    import glob
    import importlib.metadata as _md
    import os
    import subprocess as _sp

    target = args.target
    _write_update_status(state="checking")

    # Self-heal: an older broken installer used `pip install --target --upgrade`,
    # which leaves BOTH dist-infos behind. importlib then misreports the version,
    # so updates never "take" and the app nags to reopen forever. If we see more
    # than one teammate_sync dist-info, force a clean reinstall regardless of the
    # version comparison below.
    polluted = len(glob.glob(os.path.join(target, "teammate_sync-*.dist-info"))) > 1

    try:
        latest = httpx.get(
            "https://pypi.org/pypi/teammate-sync/json", timeout=10
        ).json()["info"]["version"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        _write_update_status(state="offline")
        print(f"self-update: skipped (cannot reach PyPI: {e})")
        return 0

    try:
        current = _md.version("teammate-sync")
    except _md.PackageNotFoundError:
        current = "0"

    if not polluted and _ver_tuple(latest) <= _ver_tuple(current):
        _write_update_status(state="uptodate", current=current)
        print(f"self-update: up to date ({current})")
        return 0
    if polluted:
        print(f"self-update: package dir has stacked installs — clean-reinstalling {latest}")

    _write_update_status(state="downloading", current=current, latest=latest)
    print(f"self-update: {current} -> {latest}; installing to {target} …")

    # pip install --target --upgrade does NOT remove the old version — it leaves
    # both dist-infos, and importlib keeps reporting the OLD version, so the
    # update never takes effect (endless "reopen to apply"). Install into a
    # clean staging dir and swap it in, so only the new version remains.
    import shutil
    staging = target + ".new"
    shutil.rmtree(staging, ignore_errors=True)
    res = _sp.run(
        [sys.executable, "-m", "pip", "install",
         "--target", staging, f"teammate-sync=={latest}"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        _write_update_status(state="error", message="install failed")
        print(f"self-update: pip failed: {res.stderr.strip()[:300]}")
        return 1
    # Swap freshly-installed staging into place (same parent dir → atomic
    # rename). On failure above, the existing install is left untouched.
    shutil.rmtree(target, ignore_errors=True)
    os.replace(staging, target)
    _write_update_status(state="ready", current=current, latest=latest)
    print(f"self-update: installed {latest}. Restart CodeBaton to apply.")
    return 0


def cmd_app(args) -> int:
    """Launch the macOS menu bar app, or install/remove its LaunchAgent."""
    if sys.platform != "darwin":
        print("teammate-sync app is currently macOS-only.", file=sys.stderr)
        return 1
    try:
        from . import macapp
    except RuntimeError as e:
        # rumps not installed (e.g. user did pipx install without macOS extras)
        print(f"[teammate-sync app] {e}", file=sys.stderr)
        return 1

    if args.install_launchagent:
        return macapp.install_launchagent_only()
    if args.uninstall_launchagent:
        return macapp.uninstall_launchagent_only()
    return macapp.run()


def cmd_logs(args) -> int:
    """Tail the daemon log. With -f, follow."""
    import subprocess as _sp
    log = _log_file()
    if not log.exists():
        print("no daemon log yet (run `teammate-sync up` to start the daemon).")
        return 0
    cmd = ["tail"]
    if args.follow:
        cmd.append("-f")
    if args.lines:
        cmd.extend(["-n", str(args.lines)])
    cmd.append(str(log))
    try:
        return _sp.call(cmd)
    except KeyboardInterrupt:
        return 0


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def capture_claude_token() -> tuple[bool, str]:
    """Run `claude setup-token` under a PSEUDO-TERMINAL so it behaves exactly
    like it does in a real terminal: auto-opens the browser, waits for the user
    to authorize, then prints the long-lived token — which we capture and store.

    Without a PTY, claude detects 'non-interactive', doesn't open the browser,
    and just hangs. The PTY is the fix. Returns (ok, message); the token is
    never returned or logged."""
    import errno
    import os as _os
    import pty
    import re as _re
    import select
    import signal as _signal
    import time as _time
    from .auth import write_claude_token

    try:
        claude = _resolve_claude_binary()
    except RuntimeError:
        return False, "Claude Code CLI not found."

    env = dict(_os.environ)
    env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")

    # Spawn claude attached to a PTY so it thinks it's interactive.
    pid, master_fd = pty.fork()
    if pid == 0:  # child
        try:
            _os.execve(claude, [claude, "setup-token"], env)
        except Exception:
            _os._exit(127)

    token = None
    buf = b""
    deadline = _time.time() + 300  # 5 min for the user to authorize in the browser
    try:
        while _time.time() < deadline:
            try:
                r, _, _ = select.select([master_fd], [], [], 3)
            except (OSError, ValueError):
                break
            if master_fd in r:
                try:
                    chunk = _os.read(master_fd, 4096)
                except OSError as e:
                    if e.errno == errno.EIO:  # PTY closed = child exited
                        break
                    continue
                if not chunk:
                    break
                buf += chunk
                m = _re.search(rb"sk-ant-oat\d{2}-[A-Za-z0-9_-]+", buf)
                if m:
                    token = m.group(0).decode()
                    break
            # child already exited?
            try:
                done_pid, _ = _os.waitpid(pid, _os.WNOHANG)
                if done_pid == pid:
                    # drain any final output
                    try:
                        buf += _os.read(master_fd, 65536)
                    except OSError:
                        pass
                    m = _re.search(rb"sk-ant-oat\d{2}-[A-Za-z0-9_-]+", buf)
                    if m:
                        token = m.group(0).decode()
                    break
            except OSError:
                break
    finally:
        try:
            _os.kill(pid, _signal.SIGTERM)
        except OSError:
            pass
        try:
            _os.close(master_fd)
        except OSError:
            pass

    if not token:
        return False, "No token captured (authorization may have been cancelled or timed out)."
    write_claude_token(token)
    return True, "Claude authorized for background decision capture + live answers."


def cmd_setup_claude(args) -> int:
    ok, msg = capture_claude_token()
    print(msg)
    return 0 if ok else 1


def cmd_hook(args) -> int:
    """Dispatch a Claude Code session lifecycle hook event."""
    from . import hook as _hook
    sys.argv = ["teammate-sync-hook", args.op]
    return _hook.main()


def distill_enabled() -> bool:
    """Decision capture is ON by default (automatic, silent — like the daemon).
    Fail-safe: a failed distill just logs and never affects sync. Users can opt
    OUT via the Settings toggle, which writes ~/.teammate-sync/distill.disabled."""
    return not Path("~/.teammate-sync/distill.disabled").expanduser().exists()


def cmd_distill(args) -> int:
    """Fold one session into knowledge.md via the engineer's own Claude.
    Invoked detached by the daemon (silent, background). Hidden command."""
    from . import distiller
    from datetime import datetime, timezone
    session = Path(args.session).expanduser()
    out = Path(args.out).expanduser()
    if not session.exists():
        return 1
    sid = args.session_id or session.stem
    try:
        claude = _resolve_claude_binary()
    except RuntimeError:
        return 1
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    ok = distiller.distill_session(session, out, sid, when, claude)
    return 0 if ok else 1


def cmd_mcp_server(args) -> int:
    """Launch the MCP server on stdio (invoked by Claude Code, not by users)."""
    try:
        from . import server as _server
    except ValueError as e:
        # E.g. missing Anthropic key — surface as one clean line, not a traceback.
        print(f"[teammate-sync mcp-server] {e}", file=sys.stderr)
        return 1
    _server.mcp.run()
    return 0


def cmd_logout(args) -> int:
    path = auth_file_path()
    if path.exists():
        path.unlink()
        print(f"Deleted {path}. You are signed out.")
    else:
        print("No auth file found — you are already signed out.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="teammate-sync",
        description="Cross-engineer Claude Code context sharing.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Sign in with GitHub and configure your workspace.")
    p_init.add_argument("--backend-url", default=DEFAULT_BACKEND_URL, help="Cloud backend URL (default: production)")
    p_init.set_defaults(func=cmd_init)

    sub.add_parser("whoami", help="Show identity from your saved auth.").set_defaults(func=cmd_whoami)
    sub.add_parser("teammates", help="List teammates in your workspace.").set_defaults(func=cmd_teammates)
    sub.add_parser("logout", help="Delete your saved auth file.").set_defaults(func=cmd_logout)

    p_install = sub.add_parser(
        "install-commands",
        help="Reinstall the v0.3 slash commands into ~/.claude/commands/. "
             "Cleans up any retired v0.2 commands automatically.",
    )
    p_install.set_defaults(func=cmd_install_commands)

    # ── v0.3 minimal slash-backing surface ──
    p_connect = sub.add_parser(
        "connect",
        help="Share this Claude Code session with named teammates "
             "(no args = list workspace + status).",
    )
    p_connect.add_argument("recipients", nargs="*",
                           help="GitHub handles to share with. Empty = list mode.")
    p_connect.set_defaults(func=cmd_connect)

    p_disconnect = sub.add_parser(
        "disconnect",
        help="Disconnect. No arg = nuke ALL connections + local shares. "
             "<handle> = remove just that one.",
    )
    p_disconnect.add_argument("handle", nargs="?", default=None,
                              help="GitHub handle. Omit to disconnect from everyone.")
    p_disconnect.set_defaults(func=cmd_disconnect)

    sub.add_parser("shared",
                   help="Audit which sessions are shareable + with whom.").set_defaults(func=cmd_shared)

    p_alias = sub.add_parser(
        "alias",
        help="Nickname a teammate's handle for easy /ask (e.g. alias om om-divyatej).",
    )
    p_alias.add_argument("name", nargs="?", default=None,
                         help="Short local name, e.g. 'om'. Omit to list all aliases.")
    p_alias.add_argument("handle", nargs="?", default=None,
                         help="The teammate's GitHub handle, e.g. 'om-divyatej'.")
    p_alias.add_argument("--rm", metavar="NAME", default=None,
                         help="Remove the alias with this name.")
    p_alias.set_defaults(func=cmd_alias)

    # ── Daemon lifecycle ──
    p_up = sub.add_parser(
        "up",
        help="Start the sync daemon in the background (writes PID + logs to ~/.teammate-sync/state/).",
    )
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="Stop the backgrounded sync daemon.")
    p_down.set_defaults(func=cmd_down)

    sub.add_parser(
        "upgrade",
        help="One-shot upgrade: stop daemon → pipx upgrade → refresh slash commands → restart daemon.",
    ).set_defaults(func=cmd_upgrade)

    p_app = sub.add_parser(
        "app",
        help="Launch the macOS menu bar app (foreground). "
             "Auto-start at login via --install-launchagent.",
    )
    p_app.add_argument("--install-launchagent", action="store_true",
                       help="Install a LaunchAgent plist so the app launches at every login.")
    p_app.add_argument("--uninstall-launchagent", action="store_true",
                       help="Remove the LaunchAgent (stop auto-start at login).")
    p_app.set_defaults(func=cmd_app)

    p_logs = sub.add_parser("logs", help="Tail the daemon log.")
    p_logs.add_argument("-f", "--follow", action="store_true",
                        help="Follow new output (like tail -f).")
    p_logs.add_argument("-n", "--lines", type=int, default=50,
                        help="Number of trailing lines to show (default 50).")
    p_logs.set_defaults(func=cmd_logs)

    # ── Dashboard ──
    p_dash = sub.add_parser(
        "dashboard",
        help="Launch the desktop dashboard (native window via pywebview "
             "if available, browser otherwise).",
    )
    p_dash.add_argument("--port", type=int, default=None,
                        help="Port to bind. Default: pick a free one.")
    p_dash.add_argument("--no-browser", action="store_true",
                        help="When falling back to browser mode, don't auto-open it.")
    p_dash.add_argument("--browser", action="store_true",
                        help="Force browser mode (skip pywebview native window).")
    p_dash.add_argument("--serve-only", action="store_true",
                        help="Headless: start the HTTP server, print {\"port\": N} as JSON, "
                             "block forever. Used by the Electron desktop app.")
    p_dash.set_defaults(func=cmd_dashboard)

    # ── Foreground daemon (kept for `teammate-sync up` to invoke + power users) ──
    p_daemon = sub.add_parser(
        "daemon",
        help="Run the sync daemon in the FOREGROUND (use `up` for background).",
    )
    p_daemon.add_argument("extra", nargs="*", help="Optional source dir overrides for testing.")
    p_daemon.set_defaults(func=cmd_daemon)

    # Internal: invoked by Claude Code hooks (SessionStart, PostToolUse, SessionEnd).
    # End users do not call this directly — `init` wires it into ~/.claude/settings.json.
    p_hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    p_hook.add_argument("op", choices=["start", "heartbeat", "end"])
    p_hook.set_defaults(func=cmd_hook)

    # Internal: invoked by Claude Code over stdio as the MCP server.
    sub.add_parser("mcp-server", help=argparse.SUPPRESS).set_defaults(func=cmd_mcp_server)

    # Internal: the daemon spawns this detached to fold a session into
    # knowledge.md (silent background distillation).
    p_distill = sub.add_parser("distill", help=argparse.SUPPRESS)
    p_distill.add_argument("--session", required=True, help="Path to the session .jsonl")
    p_distill.add_argument("--out", required=True, help="Path to knowledge.md to update")
    p_distill.add_argument("--session-id", default=None)
    p_distill.set_defaults(func=cmd_distill)

    # Authorize Claude for headless/background decision capture (browser OAuth
    # via `claude setup-token`). Stores the token for the daemon's distiller.
    sub.add_parser("setup-claude", help=argparse.SUPPRESS).set_defaults(func=cmd_setup_claude)

    # Internal: the desktop app runs this to pull the latest package from PyPI
    # into a user dir (kept on PYTHONPATH), so it updates without a re-download.
    p_selfupdate = sub.add_parser("self-update", help=argparse.SUPPRESS)
    p_selfupdate.add_argument("--target", required=True,
                              help="Directory to install the updated package into.")
    p_selfupdate.set_defaults(func=cmd_self_update)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
