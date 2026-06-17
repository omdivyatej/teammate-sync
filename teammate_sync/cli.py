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
        cmd = f"{binary} hook {op}"
        hooks[event] = [{"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}]

    settings_path.write_text(json.dumps(settings, indent=2))
    return settings_path


def _register_mcp(binary: str) -> bool:
    """Register the MCP server via `claude mcp add`. Returns True on success.

    The server self-loads the Anthropic key from ~/.teammate-sync/auth.json
    at launch — no env var is passed at registration time, so the user
    never has to manage ANTHROPIC_API_KEY in their shell."""
    import subprocess as _sp

    # remove any existing registration first (idempotent)
    _sp.run(
        ["claude", "mcp", "remove", "teammate-sync", "--scope", "user"],
        capture_output=True,
    )

    result = _sp.run(
        [
            "claude", "mcp", "add",
            "--scope", "user",
            "teammate-sync",
            "--",
            binary, "mcp-server",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ⚠️  claude mcp add failed: {result.stderr.strip()}")
        return False
    return True


def _prompt_for_anthropic_key(existing: str | None) -> str | None:
    """
    Interactively prompt for the Anthropic API key.

    If `existing` is non-empty, offer to keep it (default) or replace.
    Returns the key to store (existing or new), or None if the user declined
    to provide one.
    """
    if existing:
        print(f"\nAnthropic API key is already stored "
              f"(starts with {existing[:12]}...).")
        choice = input("  Keep it [k] / replace [r] / skip [s]? ").strip().lower() or "k"
        if choice == "k":
            return existing
        if choice == "s":
            return existing  # keep the existing rather than clearing

    print()
    print("Anthropic API key — used by teammate-sync's MCP server to synthesize")
    print("cited answers when teammates query you. Get one (or reuse an existing")
    print("one) at https://console.anthropic.com/settings/keys.")
    print()
    while True:
        key = input("  Paste your Anthropic API key (or press Enter to skip): ").strip()
        if not key:
            print("  Skipped. The MCP server won't work until you set one.")
            print("  Re-run `teammate-sync init` to add it later.")
            return None
        if not key.startswith("sk-ant-"):
            print("  That doesn't look like an Anthropic key (should start with 'sk-ant-').")
            print("  Try again or press Enter to skip.")
            continue
        return key


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

    # --- Anthropic key (interactive) -----------------------------------------
    # Preserve any existing key in auth.json on re-run.
    existing_key = None
    try:
        existing_key = json.loads(auth_file_path().read_text()).get("anthropic_key")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    anthropic_key = _prompt_for_anthropic_key(existing_key)

    path = write_auth(
        token=token, org=org, backend_url=backend_url,
        anthropic_key=anthropic_key,
    )
    print(f"\n✓ Saved {path} (mode 0600)")
    print(f"  GitHub:    {me['github_handle']}")
    print(f"  Workspace: {org}")
    print(f"  Backend:   {backend_url}")
    print(f"  Anthropic: {'set' if anthropic_key else 'NOT SET (MCP server will fail)'}")

    # --- Slash commands ------------------------------------------------------
    print("\nInstalling /share /unshare /shared slash commands...")
    commands_dir = Path("~/.claude/commands").expanduser()
    commands_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "share.md":   _slash_command_md("share",   binary),
        "unshare.md": _slash_command_md("unshare", binary),
        "shared.md":  _slash_command_md("shared",  binary),
    }
    for name, content in files.items():
        (commands_dir / name).write_text(content)
    print(f"  Wrote {commands_dir}/{{share,unshare,shared}}.md")

    # --- Hooks ---------------------------------------------------------------
    print("\nRegistering Claude Code hooks (SessionStart, PostToolUse, SessionEnd)...")
    settings_path = _install_hooks_into_claude_settings(binary)
    print(f"  Merged into {settings_path}")

    # --- MCP server ----------------------------------------------------------
    # No env var required — the server self-loads its Anthropic key from
    # auth.json at launch. Always register; if the key is missing, the user
    # gets a clear runtime error from the server pointing them back to init.
    print("\nRegistering MCP server with Claude Code (user scope)...")
    if _register_mcp(binary):
        print("  ✓ teammate-sync registered")

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
    try:
        auth = read_auth()
    except (FileNotFoundError, ValueError) as e:
        print(str(e))
        return 1
    r = httpx.get(
        f"{auth['backend_url'].rstrip('/')}/v1/teammates",
        params={"org": auth["org"]},
        headers={"Authorization": f"Bearer {auth['token']}"},
        timeout=20,
    )
    if r.status_code != 200:
        print(f"Backend rejected the request ({r.status_code}): {r.text}")
        return 1
    members = sorted(r.json().get("teammates", []), key=lambda m: m["github_handle"])
    print(f"Teammates in workspace '{auth['org']}' ({len(members)}):")
    for m in members:
        print(f"  - {m['github_handle']}")
    return 0


def _slash_command_md(action: str, binary: str) -> str:
    """
    Generate a slash-command markdown file. Calls the installed
    `teammate-sync` binary so the same markdown works regardless of
    where the package is installed.

    /unshare accepts an optional argument via $ARGUMENTS — empty for
    "this session", a session-id for a specific one, or --all to nuke
    everything.
    """
    if action == "unshare":
        return f"""---
description: Unshare a Claude Code session. No args unshares THIS session; pass a session-id to unshare that specific one; pass --all to unshare everything.
argument-hint: "[session-id | --all]"
allowed-tools: Bash({binary}:*)
---

Execute this command via the Bash tool and show its full stdout
output to the user verbatim:

```
{binary} unshare $ARGUMENTS
```

The CLAUDE_CODE_SESSION_ID env var is set by Claude Code in the Bash
subprocess; the script falls back to it when no argument is given.

After showing the output, do NOT add commentary.
"""

    descriptions = {
        "share":  "Mark this Claude Code session as shareable with teammates via teammate-sync",
        "shared": "List which Claude Code sessions are currently shared with teammates via teammate-sync",
    }
    return f"""---
description: {descriptions[action]}
allowed-tools: Bash({binary}:*)
---

Execute this exact command via the Bash tool and show its full stdout
output to the user verbatim:

```
{binary} {action}
```

The CLAUDE_CODE_SESSION_ID and CLAUDE_PROJECT_DIR env vars are set by
Claude Code in the Bash subprocess — the script reads them from there.

After showing the output, do NOT add commentary.
"""


def _resolve_self_binary() -> str:
    """
    Find where the installed `teammate-sync` binary lives.
    Hooks, MCP, and slash commands all dispatch through it.
    """
    import shutil
    found = shutil.which("teammate-sync")
    if not found:
        raise RuntimeError(
            "Could not locate the `teammate-sync` binary on PATH. "
            "Reinstall with `pip install teammate-sync` and ensure the "
            "Python scripts directory is on your PATH."
        )
    return found


def cmd_install_commands(args) -> int:
    """Install /share, /unshare, /shared into ~/.claude/commands/ for this user."""
    binary = _resolve_self_binary()
    commands_dir = Path("~/.claude/commands").expanduser()
    commands_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "share.md":   _slash_command_md("share",   binary),
        "unshare.md": _slash_command_md("unshare", binary),
        "shared.md":  _slash_command_md("shared",  binary),
    }
    for name, content in files.items():
        (commands_dir / name).write_text(content)
        print(f"  wrote {commands_dir / name}")

    print()
    print(f"✓ Installed /share, /unshare, /shared into {commands_dir}")
    print(f"  Pointing at: {binary}")
    print()
    print("Restart any open Claude Code sessions to pick them up.")
    return 0


def cmd_share(args) -> int:
    from . import share_cli
    return share_cli.cmd_share()


def cmd_unshare(args) -> int:
    from . import share_cli
    if args.all:
        return share_cli.cmd_unshare(target="--all")
    return share_cli.cmd_unshare(args.target)


def cmd_shared(args) -> int:
    from . import share_cli
    return share_cli.cmd_list()


def cmd_daemon(args) -> int:
    from . import daemon as _daemon
    # daemon's main() reads sys.argv; replace it so the global state dirs are used
    sys.argv = ["teammate-sync-daemon"] + (args.extra or [])
    return _daemon.main()


def cmd_hook(args) -> int:
    """Dispatch a Claude Code session lifecycle hook event."""
    from . import hook as _hook
    sys.argv = ["teammate-sync-hook", args.op]
    return _hook.main()


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
        help="Reinstall /share, /unshare, /shared slash commands into ~/.claude/commands/.",
    )
    p_install.set_defaults(func=cmd_install_commands)

    # Direct-invoke commands (also reachable via the /share, /unshare, /shared slash commands)
    sub.add_parser("share",   help="Mark the current Claude Code session as shareable.").set_defaults(func=cmd_share)
    p_unshare = sub.add_parser(
        "unshare",
        help="Unshare a session. No args = this session. "
             "<session-id> = that specific one. --all = nuke everything.",
    )
    p_unshare.add_argument("target", nargs="?", default=None,
                           help="Session ID (full or unambiguous prefix)")
    p_unshare.add_argument("--all", action="store_true",
                           help="Unshare every session in the registry.")
    p_unshare.set_defaults(func=cmd_unshare)
    sub.add_parser("shared",  help="List currently shared sessions.").set_defaults(func=cmd_shared)

    p_daemon = sub.add_parser(
        "daemon",
        help="Run the sync daemon (foreground, leave terminal open).",
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

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
