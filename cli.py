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
import socketserver
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

from auth import DEFAULT_BACKEND_URL, auth_file_path, read_auth, write_auth


CALLBACK_TIMEOUT_SECONDS = 180


def cmd_init(args) -> int:
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

    path = write_auth(token=token, org=org, backend_url=backend_url)
    print(f"\n✓ Saved {path} (mode 0600)")
    print(f"  Workspace: {org}")
    print(f"  Backend:   {backend_url}")
    print()
    print("Next steps:")
    print("  1. Start the daemon:           ./start-daemon.sh")
    print("  2. In a Claude Code session:   /share")
    print("  3. Teammates can now query you via their MCP.")
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


def _slash_command_md(action: str, project_dir: Path, workspace_dir: Path) -> str:
    """Generate a slash-command markdown file with paths resolved for this install."""
    py = project_dir / ".venv" / "bin" / "python"
    cli = project_dir / "share-cli.py"
    shared_file = workspace_dir / ".shared-sessions.json"
    descriptions = {
        "share":   "Mark this Claude Code session as shareable with teammates via teammate-sync",
        "unshare": "Remove this Claude Code session from teammate-sync sharing",
        "list":    "List which Claude Code sessions are currently shared with teammates via teammate-sync",
    }
    return f"""---
description: {descriptions[action]}
allowed-tools: Bash({py}:*)
---

Execute this exact command via the Bash tool and show its full stdout
output to the user verbatim:

```
TEAMMATE_SHARED_SESSIONS_FILE={shared_file} {py} {cli} {action}
```

The CLAUDE_CODE_SESSION_ID env var is set automatically by Claude Code
for Bash subprocesses — the script reads it from there.

After showing the output, do NOT add commentary.
"""


def cmd_install_commands(args) -> int:
    """Install /share, /unshare, /shared into ~/.claude/commands/ for this user."""
    project_dir = Path(__file__).resolve().parent
    workspace_dir = Path(args.workspace).expanduser().resolve()

    if not workspace_dir.exists():
        print(f"Workspace {workspace_dir} doesn't exist yet. Creating it.")
        workspace_dir.mkdir(parents=True, exist_ok=True)

    commands_dir = Path("~/.claude/commands").expanduser()
    commands_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "share.md":   _slash_command_md("share",   project_dir, workspace_dir),
        "unshare.md": _slash_command_md("unshare", project_dir, workspace_dir),
        "shared.md":  _slash_command_md("list",    project_dir, workspace_dir),
    }
    for name, content in files.items():
        path = commands_dir / name
        path.write_text(content)
        print(f"  wrote {path}")

    print()
    print(f"✓ Installed /share, /unshare, /shared into {commands_dir}")
    print(f"  Pointing at: project={project_dir}")
    print(f"               workspace={workspace_dir}")
    print()
    print("Restart any open Claude Code sessions to pick them up.")
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
        help="Write /share, /unshare, /shared slash commands into ~/.claude/commands/ "
             "with absolute paths for this install.",
    )
    p_install.add_argument(
        "--workspace", required=True,
        help="Path to your workspace dir (where CLAUDE.md, scratch notes, and "
             ".shared-sessions.json live). E.g. ~/my-project/.claude",
    )
    p_install.set_defaults(func=cmd_install_commands)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
