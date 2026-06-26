#!/usr/bin/env python3
"""
teammate-sync share/connection CLI (v0.2).

Backs the slash commands. Maintains a per-machine .shared-sessions.json
registry of session → recipients, used by the daemon as the upload gate.

Slash commands wired to these subcommands (via cli.py):
    /share <handle> [<handle> ...]   cmd_share(recipients)
    /unshare [<sid>|--all]           cmd_unshare(target)
    /shared                          cmd_list()
    /connections                     cmd_connections()
    /accept <handle>                 cmd_accept(handle)
    /decline <handle>                cmd_decline(handle)
    /disconnect <handle>             cmd_disconnect(handle)
    /teammates                       cmd_teammates()
    /show <handle> [<sid>]           cmd_show(handle, session_id)
"""
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# IMPORTANT: state/ subdir, NOT ~/.teammate-sync/ root. The daemon watches
# ~/.teammate-sync/state/ specifically — keeping credentials (auth.json) and
# other sensitive files OUT of the synced tree.
DEFAULT_FILE = "~/.teammate-sync/state/.shared-sessions.json"
TARGET_FILE = Path(
    os.environ.get("TEAMMATE_SHARED_SESSIONS_FILE", DEFAULT_FILE)
).expanduser()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def update_registry(modify_fn) -> dict:
    """Load → modify → write back, in place, under exclusive fcntl lock."""
    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = TARGET_FILE.with_suffix(".lock")
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            if TARGET_FILE.exists():
                try:
                    state = json.loads(TARGET_FILE.read_text())
                except json.JSONDecodeError:
                    state = {"sessions": []}
            else:
                state = {"sessions": []}

            state = modify_fn(state) or state
            state["updated_at"] = now_iso()
            TARGET_FILE.write_text(json.dumps(state, indent=2))
            return state
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def require_session_id() -> str | None:
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        print(
            "Error: CLAUDE_CODE_SESSION_ID is not set in the environment.\n"
            "  This command should be run from inside a Claude Code session\n"
            "  (via the /share or /unshare slash commands).",
            file=sys.stderr,
        )
        return None
    return sid


def _get_backend():
    """Construct the cloud HTTPBackend for the current user. Raises on no auth."""
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
    self_handle = r.json()["github_handle"]
    return HTTPBackend(
        backend_url=auth["backend_url"],
        token=auth["token"],
        org=auth["org"],
        teammate=self_handle,
    )


# ─── /share ────────────────────────────────────────────────────────────────

def cmd_share(recipients: list[str]) -> int:
    """
    Mark current Claude Code session as shared with `recipients`.
    If `recipients` is empty, share with all currently-accepted connections.
    For any recipient we don't have an accepted connection with yet, fire
    a connection request immediately (so they see it on their next session
    start) — content will then flow once they /accept.
    """
    sid = require_session_id()
    if not sid:
        return 1
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())

    # Hard-stop if the sync engine (daemon) isn't running. The daemon is what
    # uploads the session — without it, /connect would register a share that
    # silently never flows. Refuse like Docker does when its engine is off.
    from .cli import _read_pid, _pid_alive  # lazy: cli imports share_cli
    _pid = _read_pid()
    if not (_pid and _pid_alive(_pid)):
        again = " ".join(recipients) if recipients else "<teammate>"
        print("✗ Can't share — the CodeBaton sync engine is not running.")
        print('  Open CodeBaton and click "Start Engine" (or run: teammate-sync up),')
        print(f"  then run /connect {again} again.")
        print()
        print("  Nothing was shared.")
        return 0

    # Resolve recipients. If none given, use my accepted connections.
    try:
        backend = _get_backend()
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    conn_results: dict[str, str] = {}

    if not recipients:
        conns = backend.list_connections()
        recipients = sorted(c["peer_handle"] for c in conns.get("accepted", []))
        if not recipients:
            print("You have no accepted connections yet.")
            print("To share with someone specifically: /share <github-handle>")
            print("(if they haven't connected with you before, they'll get a")
            print("pending invite to accept first; content flows once accepted.)")
            return 1
        print(f"No handles given — sharing with all {len(recipients)} accepted "
              f"connection(s): {', '.join(recipients)}")
        # Already-accepted, no need to re-request.
    else:
        # Validate: no self-share
        my_handle = backend.teammate
        recipients = [r for r in recipients if r != my_handle]
        if not recipients:
            print("Error: can't share with yourself.", file=sys.stderr)
            return 1

        # Validate: every recipient must be a real member of the workspace.
        # Otherwise /connect elon-musk would fire a request that can never be
        # accepted (he's not in the org). Reject non-members outright.
        try:
            members = set(workspace_handles())
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        bad = [r for r in recipients if r not in members]
        if bad:
            print(f"✗ Not in your workspace: {', '.join(bad)}")
            print(f"  Connection request NOT sent — these aren't members of "
                  f"'{backend.org}'.")
            print(f"  Workspace members: {', '.join(sorted(members)) or '(none)'}")
            return 1

        # For each recipient, fire a connection request. The backend is
        # idempotent — already-accepted = no-op, pending = no-op,
        # mutual-interest = auto-accepts.
        for peer in recipients:
            try:
                res = backend.request_connection(peer)
                conn_results[peer] = res.get("status", "?")
            except Exception as e:
                conn_results[peer] = f"err: {e}"

    # Update the local registry.
    def mod(state: dict) -> dict:
        sessions = state.get("sessions", [])
        sessions = [s for s in sessions
                    if not (isinstance(s, dict) and s.get("session_id") == sid)]
        sessions.append({
            "session_id": sid,
            "cwd": cwd,
            "shared_at": now_iso(),
            "recipients": recipients,
        })
        state["sessions"] = sessions
        return state

    update_registry(mod)
    print(f"✓ Session {sid[:8]} now shared with: {', '.join(recipients)}")
    print(f"  cwd: {cwd}")
    for peer, status in conn_results.items():
        if status == "pending":
            print(f"  → connection request sent to {peer} (awaiting their /accept)")
        elif status == "accepted":
            print(f"  → already connected to {peer}; content flowing")
        elif status.startswith("err:"):
            print(f"  → ⚠ could not request connection to {peer}: {status}")
    print()
    print("  Shared for the lifetime of this Claude Code session. /unshare to")
    print("  revoke sooner; /shared to audit. /connections to see invite status.")
    return 0


# ─── /unshare ──────────────────────────────────────────────────────────────

def cmd_unshare(target: str | None = None) -> int:
    """
    target: None = current session, "<sid>" = that session, "--all" = everything.
    """
    if target == "--all":
        if not TARGET_FILE.exists():
            print("Nothing is currently shared.")
            return 0
        def mod_all(state: dict) -> dict:
            state["sessions"] = []
            return state
        update_registry(mod_all)
        print("✓ All shared sessions removed. The daemon will purge the")
        print("  team's shared store on its next event.")
        return 0

    if target:
        if not TARGET_FILE.exists():
            print("Nothing is currently shared, so nothing to unshare.")
            return 0
        try:
            state = json.loads(TARGET_FILE.read_text())
        except json.JSONDecodeError:
            state = {"sessions": []}
        candidates = [
            s for s in state.get("sessions", [])
            if isinstance(s, dict) and s.get("session_id", "").startswith(target)
        ]
        if not candidates:
            print(f"No shared session matches '{target}'.")
            print(f"Run /shared to see what's shared and pick a full session ID.")
            return 1
        if len(candidates) > 1:
            print(f"'{target}' is ambiguous — matches {len(candidates)} sessions:")
            for c in candidates:
                print(f"  - {c['session_id']}")
            print(f"Use the full session ID.")
            return 1
        sid = candidates[0]["session_id"]
    else:
        sid = require_session_id()
        if not sid:
            return 1

    def mod(state: dict) -> dict:
        sessions = state.get("sessions", [])
        state["sessions"] = [
            s for s in sessions
            if not (isinstance(s, dict) and s.get("session_id") == sid)
        ]
        return state

    state = update_registry(mod)
    n = len(state["sessions"])
    print(f"✓ Session {sid[:8]} removed from shareable set.")
    if n == 0:
        print("  No sessions remain shared. The daemon will clean up")
        print("  the team's shared store on its next event.")
    else:
        print(f"  {n} other session(s) still shared.")
    return 0


def remove_shared_session(session_id: str) -> bool:
    """Called by SessionEnd hook so share state doesn't outlive the session."""
    if not session_id or not TARGET_FILE.exists():
        return False
    removed = {"flag": False}
    def mod(state: dict) -> dict:
        sessions = state.get("sessions", [])
        before = len(sessions)
        state["sessions"] = [
            s for s in sessions
            if not (isinstance(s, dict) and s.get("session_id") == session_id)
        ]
        removed["flag"] = len(state["sessions"]) < before
        return state
    update_registry(mod)
    return removed["flag"]


# ─── /shared ───────────────────────────────────────────────────────────────

def cmd_list() -> int:
    if not TARGET_FILE.exists():
        print("No shared-sessions registry yet. Nothing is being shared.")
        return 0
    try:
        state = json.loads(TARGET_FILE.read_text())
    except json.JSONDecodeError:
        print(f"Registry at {TARGET_FILE} is malformed.")
        return 1

    sessions = state.get("sessions", [])
    if not sessions:
        print("No sessions are currently shared with teammates.")
        return 0

    cur = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    print(f"Currently shared sessions ({len(sessions)}):")
    print()
    for s in sessions:
        if not isinstance(s, dict):
            continue
        sid = s.get("session_id", "?")
        marker = "  ← this session" if sid == cur else ""
        recipients = s.get("recipients") or []
        print(f"  {sid}{marker}")
        print(f"    shared with: {', '.join(recipients) if recipients else '(no recipients — not actually shared)'}")
        print(f"    shared at:   {s.get('shared_at', '?')}")
        if s.get('cwd'):
            print(f"    cwd:         {s['cwd']}")
        print()
    print("To unshare a specific session: /unshare <session-id>")
    print("To unshare ALL sessions:       /unshare --all")
    return 0


# ─── /connections, /accept, /decline, /disconnect ──────────────────────────

def cmd_connections() -> int:
    try:
        backend = _get_backend()
        data = backend.list_connections()
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    accepted = data.get("accepted", [])
    pending_in = data.get("pending_incoming", [])
    pending_out = data.get("pending_outgoing", [])

    print(f"Connections in workspace '{data.get('org')}':")
    print()
    print(f"  Accepted ({len(accepted)}):")
    if accepted:
        for c in accepted:
            who = "you" if c.get("i_initiated") else c["peer_handle"]
            print(f"    - {c['peer_handle']}  (initiated by {who})")
    else:
        print(f"    (none yet — run /share <github-handle> to invite someone)")
    print()
    print(f"  Pending — they need to /accept you ({len(pending_out)}):")
    for c in pending_out:
        print(f"    → {c['peer_handle']}")
    if not pending_out:
        print(f"    (none)")
    print()
    print(f"  Pending — YOU can /accept or /decline ({len(pending_in)}):")
    for c in pending_in:
        print(f"    ← {c['peer_handle']}  /accept {c['peer_handle']}")
    if not pending_in:
        print(f"    (none)")
    return 0


def cmd_accept(handle: str) -> int:
    if not handle:
        print("Usage: /accept <github-handle>", file=sys.stderr)
        return 2
    try:
        backend = _get_backend()
        res = backend.accept_connection(handle)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"✓ Accepted connection from {handle}.")
    print(f"  Their shared sessions will now be visible to you when you /show {handle}")
    print(f"  or query teammate-sync's MCP.")
    return 0


def cmd_decline(handle: str) -> int:
    if not handle:
        print("Usage: /decline <github-handle>", file=sys.stderr)
        return 2
    try:
        backend = _get_backend()
        backend.decline_connection(handle)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"✓ Declined connection from {handle}.")
    return 0


def cmd_disconnect(handle: str | None) -> int:
    """
    Two flavors:
      handle=None  → nuclear: disconnect from every accepted connection AND
                     wipe local share registry entirely.
      handle="foo" → granular: disconnect from foo only AND scrub foo from
                     every session's recipients in the registry (dropping
                     any session whose recipient list becomes empty).
    """
    try:
        backend = _get_backend()
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if handle:
        try:
            backend.disconnect_connection(handle)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        # Scrub from local share registry
        def mod(state: dict) -> dict:
            new = []
            for s in state.get("sessions", []):
                if not isinstance(s, dict):
                    continue
                recipients = [r for r in s.get("recipients", []) if r != handle]
                if recipients:
                    new.append({**s, "recipients": recipients})
            state["sessions"] = new
            return state
        update_registry(mod)
        print(f"✓ Disconnected from {handle}.")
        print(f"  Trust + per-session shares between you and {handle} removed.")
        return 0

    # Nuclear: disconnect from everyone
    try:
        conns = backend.list_connections()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    peers = [c["peer_handle"] for c in conns.get("accepted", [])]
    if not peers:
        # Still wipe local registry in case there are stale entries
        update_registry(lambda s: {**s, "sessions": []})
        print("No active connections. Local share registry cleared.")
        return 0
    errors = []
    for p in peers:
        try:
            backend.disconnect_connection(p)
        except Exception as e:
            errors.append((p, str(e)))
    update_registry(lambda s: {**s, "sessions": []})
    print(f"✓ Disconnected from {len(peers)} teammate(s): {', '.join(peers)}")
    print(f"  All trust + share state wiped.")
    for p, e in errors:
        print(f"  ⚠ partial failure for {p}: {e}", file=sys.stderr)
    return 0


def cmd_connect_list() -> int:
    """List all org members with their current connection status."""
    try:
        backend = _get_backend()
        conns = backend.list_connections()
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    accepted = {c["peer_handle"] for c in conns.get("accepted", [])}
    out_pending = {c["peer_handle"] for c in conns.get("pending_outgoing", [])}
    in_pending = {c["peer_handle"] for c in conns.get("pending_incoming", [])}

    # Also list org members so user can see who's available to connect with.
    from .auth import read_auth
    import httpx
    auth = read_auth()
    r = httpx.get(
        f"{auth['backend_url'].rstrip('/')}/v1/teammates",
        params={"org": auth["org"]},
        headers={"Authorization": f"Bearer {auth['token']}"},
        timeout=20,
    )
    if r.status_code != 200:
        print(f"Backend rejected teammates lookup: {r.status_code}", file=sys.stderr)
        return 1
    members = sorted(t["github_handle"] for t in r.json().get("teammates", []))
    my_handle = backend.teammate
    members = [m for m in members if m != my_handle]

    if not members:
        print(f"No other members in workspace '{auth['org']}'.")
        return 0

    print(f"Workspace '{auth['org']}' — {len(members)} other member(s):")
    print()
    for m in members:
        if m in accepted:
            tag = "✓ connected"
        elif m in in_pending:
            tag = "← they invited you  (run /connect to share back)"
        elif m in out_pending:
            tag = "→ you invited them, awaiting their /connect"
        else:
            tag = "  (run /connect " + m + " to share this session)"
        print(f"  {m:<20}  {tag}")
    print()
    print("To share this session with someone:  /connect <handle> [<handle> ...]")
    return 0


# ─── /show ─────────────────────────────────────────────────────────────────

def cmd_show(handle: str, session_id: str | None = None) -> int:
    if not handle:
        print("Usage: /show <github-handle> [<session-id>]", file=sys.stderr)
        return 2
    try:
        backend = _get_backend()
        result = backend.dump(handle, session_id)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if session_id is None:
        # Index view: list visible files.
        files = result.get("visible_files", [])
        print(f"Files visible from {handle} ({len(files)}):")
        if not files:
            print(f"  (nothing — either {handle} hasn't shared with you yet,")
            print(f"   or you haven't /accept'd their connection request)")
        for f in files:
            print(f"  {f['path']}  ({f['size']} bytes)")
        if files:
            print()
            print(f"To dump a specific session: /show {handle} <session-id>")
        return 0

    # Session dump: stream the raw text.
    print(f"# Raw dump: {handle}/{session_id}")
    print(f"# (no AI synthesis — this is the literal session jsonl)")
    print()
    sys.stdout.buffer.write(result if isinstance(result, bytes) else str(result).encode())
    print()
    return 0


# ─── /teammates ────────────────────────────────────────────────────────────

def workspace_handles() -> list[str]:
    """All GitHub handles in the caller's workspace (org members).

    Raises FileNotFoundError/ValueError if not signed in, or ValueError if
    the backend rejects the request."""
    from .auth import read_auth
    import httpx
    auth = read_auth()
    r = httpx.get(
        f"{auth['backend_url'].rstrip('/')}/v1/teammates",
        params={"org": auth["org"]},
        headers={"Authorization": f"Bearer {auth['token']}"},
        timeout=20,
    )
    if r.status_code != 200:
        raise ValueError(f"Backend rejected the request ({r.status_code}): {r.text}")
    return [t["github_handle"] for t in r.json().get("teammates", [])]


def cmd_teammates() -> int:
    """Slash command wrapper — lists org members."""
    from .auth import read_auth
    import httpx
    try:
        auth = read_auth()
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
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
    print()
    print("To share with someone:    /share <github-handle>")
    print("To see who's connected:   /connections")
    return 0
