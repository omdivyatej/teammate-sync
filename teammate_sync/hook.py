#!/usr/bin/env python3
"""
teammate-sync hook handler (Phase 3c).

Called by Claude Code on session lifecycle events. Reads the hook payload
JSON from stdin and maintains a small registry of currently-active sessions
at .active-sessions.json inside a configured directory.

The registry file gets picked up by the sync daemon and mirrored to the
backend (S3), so the MCP server can answer questions like "what is the
teammate doing right now?" using live state, not just historical corpus.

Usage (configured in ~/.claude/settings.json):
    hook.py start       on SessionStart    → add or refresh entry
    hook.py heartbeat   on PostToolUse     → bump last_activity
    hook.py end         on SessionEnd      → remove entry

Configuration:
    TEAMMATE_ACTIVE_SESSIONS_FILE   destination path; defaults to
                                    ~/penguin-sim/.claude/.active-sessions.json
                                    (so the daemon picks it up via the
                                    synced source dir)
    HEARTBEAT_MIN_INTERVAL_SECONDS  skip heartbeat updates more frequent
                                    than this (default 5) to avoid
                                    thrashing the daemon. Set to 0 to
                                    update on every tool use.

The file is updated with an atomic write + fcntl lock so concurrent hook
fires from parallel sessions don't corrupt it.
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
DEFAULT_TARGET = "~/.teammate-sync/state/.active-sessions.json"
TARGET_FILE = Path(
    os.environ.get("TEAMMATE_ACTIVE_SESSIONS_FILE", DEFAULT_TARGET)
).expanduser()
HEARTBEAT_MIN_INTERVAL = float(
    os.environ.get("HEARTBEAT_MIN_INTERVAL_SECONDS", "5")
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def update_registry(modify_fn) -> None:
    """
    Load registry, apply modify_fn, write back in place under fcntl lock.

    We deliberately write in place rather than tmp+rename. macOS FSEvents
    fires both a moved AND a deleted event for the target on os.replace,
    and the daemon's deleted handler races with the moved handler — the
    file ends up missing in S3. The file is small (<1KB) and the fcntl
    lock prevents concurrent hooks, so a single write is fine.
    """
    TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = TARGET_FILE.with_suffix(".lock")

    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            if TARGET_FILE.exists():
                try:
                    state = json.loads(TARGET_FILE.read_text())
                except json.JSONDecodeError:
                    state = {"sessions": [], "updated_at": None}
            else:
                state = {"sessions": [], "updated_at": None}

            state = modify_fn(state) or state
            state["updated_at"] = now_iso()
            state["updated_at_epoch"] = now_epoch()

            TARGET_FILE.write_text(json.dumps(state, indent=2))
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _normalize_sessions(state: dict) -> list[dict]:
    sessions = state.get("sessions", [])
    return [s for s in sessions if isinstance(s, dict) and s.get("session_id")]


def op_start(state: dict, payload: dict) -> dict:
    session_id = payload.get("session_id")
    sessions = _normalize_sessions(state)
    existing = next((s for s in sessions if s["session_id"] == session_id), None)
    entry = {
        "session_id": session_id,
        "cwd": payload.get("cwd"),
        "transcript_path": payload.get("transcript_path"),
        "started_at": existing["started_at"] if existing else now_iso(),
        "last_activity": now_iso(),
        "last_activity_epoch": now_epoch(),
    }
    sessions = [s for s in sessions if s["session_id"] != session_id]
    sessions.append(entry)
    state["sessions"] = sessions
    return state


def op_heartbeat(state: dict, payload: dict) -> dict:
    session_id = payload.get("session_id")
    sessions = _normalize_sessions(state)
    existing = next((s for s in sessions if s["session_id"] == session_id), None)

    if existing:
        last_epoch = existing.get("last_activity_epoch", 0)
        if now_epoch() - last_epoch < HEARTBEAT_MIN_INTERVAL:
            return None  # skip — too frequent
        existing["last_activity"] = now_iso()
        existing["last_activity_epoch"] = now_epoch()
        # Update cwd in case it changed (CwdChanged event also fires but be defensive)
        if payload.get("cwd"):
            existing["cwd"] = payload["cwd"]
    else:
        sessions.append({
            "session_id": session_id,
            "cwd": payload.get("cwd"),
            "transcript_path": payload.get("transcript_path"),
            "started_at": now_iso(),
            "last_activity": now_iso(),
            "last_activity_epoch": now_epoch(),
        })

    state["sessions"] = sessions
    return state


def op_end(state: dict, payload: dict) -> dict:
    session_id = payload.get("session_id")
    sessions = _normalize_sessions(state)
    state["sessions"] = [s for s in sessions if s["session_id"] != session_id]
    # Share state is per-session — when the session ends, drop it from the
    # shared registry too. The daemon's watcher will then purge the cloud
    # copy on its next event (same path as a manual /unshare).
    try:
        from . import share_cli
        share_cli.remove_shared_session(session_id)
    except Exception as e:
        # Never let cleanup failure break the host session's shutdown.
        print(f"[teammate-sync hook] share cleanup failed: {e}", file=sys.stderr)
    return state


OPS = {
    "start": op_start,
    "heartbeat": op_heartbeat,
    "end": op_end,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in OPS:
        print(f"Usage: hook.py {{{'|'.join(OPS)}}}", file=sys.stderr)
        return 2

    op_name = sys.argv[1]
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Hook payload malformed — silently no-op so we never block the session
        return 0

    if not payload.get("session_id"):
        return 0

    try:
        update_registry(lambda state: OPS[op_name](state, payload))
    except Exception as e:
        # Never crash the host session because of us
        print(f"[teammate-sync hook] error: {e}", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
