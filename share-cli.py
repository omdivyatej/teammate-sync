#!/usr/bin/env python3
"""
teammate-sync share CLI (Phase 3d-A).

Maintains a per-workspace .shared-sessions.json registry — the gate that
tells the daemon whether anything is shareable right now. Default state
(no file, or file with empty sessions list) = nothing syncs to S3.

Invoked from Claude Code via slash commands in ~/.claude/commands/:
    /share    → share-cli.py share        adds the current session
    /unshare  → share-cli.py unshare      removes the current session
    /shared   → share-cli.py list         shows what's currently shared

The session id comes from CLAUDE_CODE_SESSION_ID, injected by Claude Code
into any subprocess (including slash-command shell execution).

Configuration:
    TEAMMATE_SHARED_SESSIONS_FILE
        Where to maintain the registry. Defaults to
        ~/penguin-sim/.claude/.shared-sessions.json (the daemon's watched
        dir in the local sim). In production this is wherever the
        teammate's workspace lives.

In-place fcntl-locked writes (same pattern as hook.py — atomic
tmp+rename was avoided because macOS FSEvents races with the daemon).
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


def cmd_share() -> int:
    sid = require_session_id()
    if not sid:
        return 1

    # Capture cwd so the daemon knows which project this session belongs to.
    # CLAUDE_PROJECT_DIR is set by Claude Code in Bash subprocesses; fallback
    # to actual cwd if the env var isn't available.
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())

    def mod(state: dict) -> dict:
        sessions = state.get("sessions", [])
        # Replace existing entry for this session_id (refresh cwd / shared_at)
        sessions = [s for s in sessions
                    if not (isinstance(s, dict) and s.get("session_id") == sid)]
        sessions.append({
            "session_id": sid,
            "cwd": cwd,
            "shared_at": now_iso(),
        })
        state["sessions"] = sessions
        return state

    state = update_registry(mod)
    n = len(state["sessions"])
    print(f"✓ Session {sid[:8]} is now shareable with teammates.")
    print(f"  cwd: {cwd}")
    print(f"  Total shared sessions: {n}")
    print()
    print("  The daemon will sync this session's transcript to your team's")
    print("  shared store. Use /unshare to revoke; /shared to audit.")
    return 0


def cmd_unshare() -> int:
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
        print(f"  {n} other session(s) still shared — workspace continues syncing.")
    return 0


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
    for s in sessions:
        if not isinstance(s, dict):
            continue
        sid = s.get("session_id", "?")
        marker = "  ← this session" if sid == cur else ""
        print(f"  - {sid[:8]}  (shared {s.get('shared_at', '?')}){marker}")
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("share", "unshare", "list"):
        print("Usage: share-cli.py {share|unshare|list}", file=sys.stderr)
        return 2
    return {
        "share": cmd_share,
        "unshare": cmd_unshare,
        "list": cmd_list,
    }[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())
