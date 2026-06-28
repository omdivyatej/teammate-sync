#!/usr/bin/env python3
"""
teammate-sync daemon.

Watches one or more source directories and mirrors changes to whichever
storage backend is configured via env vars. The first source is the
"workspace" — that's where control files (.shared-sessions.json,
.active-sessions.json) live and where the daemon checks share-mode.
Additional sources are extra content to mirror (e.g., the user's
Claude Code session jsonl dir at ~/.claude/projects/<encoded-cwd>/).

Usage:
    python daemon.py <workspace-dir> [extra-source-dir ...]

Example (VM as Saketh):
    daemon.py \\
      /home/ubuntu/saketh-workspace/.claude \\
      /home/ubuntu/.claude/projects/-home-ubuntu-saketh-workspace

Env vars (consumed by backend.make_backend_from_env):
    TEAMMATE_BACKEND          local | s3   (default: local)

    Local backend:
        TEAMMATE_CORPUS_DIR   path to target directory

    S3 backend:
        TEAMMATE_S3_BUCKET    bucket name (required)
        TEAMMATE_S3_PREFIX    key prefix, e.g. "saketh/" (optional)
        AWS_REGION            e.g. ap-southeast-1
        AWS_ACCESS_KEY_ID
        AWS_SECRET_ACCESS_KEY
"""
import json
import signal
import os
import subprocess
import sys
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .backend import (
    ACTIVE_SESSIONS_FILENAME,
    SHARED_SESSIONS_FILENAME,
    SYNC_STATE_FILENAME,
    StorageBackend,
    make_backend_from_env,
)


DEBOUNCE_SECONDS = 0.3
# Skip transient/process-local files (lock files, atomic-write tempfiles).
SKIP_SUFFIXES = (".lock", ".tmp")
# A Claude Code session jsonl is named <uuid>.jsonl. We use this to gate
# uploads from the "sessions" source so only explicitly /share'd sessions
# leave the engineer's machine — never their unrelated client work.
import re as _re

_UUID_RE = _re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def extract_session_id_from_path(rel_path: str) -> str | None:
    """Returns the session UUID if the file's basename is <uuid>.jsonl, else None."""
    name = Path(rel_path).name
    if not name.endswith(".jsonl"):
        return None
    candidate = name[: -len(".jsonl")]
    return candidate if _UUID_RE.match(candidate) else None


def read_shared_session_info(workspace: Path) -> dict[str, list[str]]:
    """
    Returns {session_id: [recipient_handles]} for all currently /share'd
    sessions in this workspace. Empty dict if no .shared-sessions.json, or
    malformed, or no sessions. A session with an empty recipients list is
    DROPPED — v0.2 requires explicit recipients, no "shared with nobody."
    """
    shared_file = workspace / SHARED_SESSIONS_FILENAME
    if not shared_file.exists():
        return {}
    try:
        data = json.loads(shared_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    sessions = data.get("sessions", [])
    if not isinstance(sessions, list):
        return {}
    info: dict[str, list[str]] = {}
    for s in sessions:
        if not isinstance(s, dict):
            continue
        sid = s.get("session_id")
        if not isinstance(sid, str):
            continue
        recipients = s.get("recipients") or []
        if not isinstance(recipients, list):
            continue
        recipients = [r for r in recipients if isinstance(r, str) and r]
        if not recipients:
            continue  # v0.2: no recipients = not actually shared
        info[sid] = recipients
    return info


def read_shared_session_ids(workspace: Path) -> set[str]:
    """Backwards-compat thin wrapper — set of session_ids currently shared."""
    return set(read_shared_session_info(workspace).keys())


def is_share_mode_active(workspace: Path) -> bool:
    """Backwards-compat helper. True iff at least one session is /share'd."""
    return bool(read_shared_session_ids(workspace))


def filter_active_sessions(data: bytes, shared_ids: set[str]) -> bytes:
    """Strip the .active-sessions.json registry down to only /share'd sessions
    before uploading, so a teammate never sees the existence or cwd of
    sessions you didn't /connect. Non-JSON content passes through unchanged."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return data
    sessions = obj.get("sessions", [])
    if isinstance(sessions, list):
        obj["sessions"] = [
            s for s in sessions
            if isinstance(s, dict) and s.get("session_id") in shared_ids
        ]
    return json.dumps(obj).encode("utf-8")


def initial_sync_all(
    sources: list[Path],
    backend: StorageBackend,
    shared_session_info: dict[str, list[str]],
    workspace_index: int = 0,
    last_uploaded_size: dict[str, int] | None = None,
) -> int:
    """
    Mirror sources into backend with v0.2 directed-share semantics.

    Source at workspace_index is the "workspace" (CLAUDE.md, scratch notes,
    etc.) — every non-skipped file uploads, but with NO session_id /
    recipients (these are non-session corpus files; backend ACL grants
    them via "any session shared from owner to requester").

    Other sources are "sessions" — only files whose basename is <uuid>.jsonl
    AND whose uuid is in shared_session_info upload, and they upload WITH
    session_id + recipients so the backend can register per-session ACL.

    Single deletion pass at the end prunes backend keys absent from the
    filtered source set.

    Returns count of files written.
    """
    written = 0
    all_source_keys: set[str] = set()

    for i, source in enumerate(sources):
        is_workspace = i == workspace_index
        for src_file in source.rglob("*"):
            if not src_file.is_file():
                continue
            if src_file.name.endswith(SKIP_SUFFIXES):
                continue
            if src_file.name == SHARED_SESSIONS_FILENAME:
                continue  # local-only permission gate

            rel = str(src_file.relative_to(source))

            sid: str | None = None
            recipients: list[str] | None = None
            if not is_workspace:
                sid = extract_session_id_from_path(rel)
                if not sid or sid not in shared_session_info:
                    continue  # not a /share'd session — skip
                recipients = shared_session_info[sid]

            all_source_keys.add(rel)
            data = src_file.read_bytes()
            if rel == ACTIVE_SESSIONS_FILENAME:
                data = filter_active_sessions(data, set(shared_session_info.keys()))
            backend.put_bytes(rel, data, session_id=sid, recipients=recipients)
            if last_uploaded_size is not None:
                last_uploaded_size[rel] = len(data)
            written += 1

    for existing_key in backend.list_keys():
        if existing_key not in all_source_keys:
            backend.delete_key(existing_key)
            if last_uploaded_size is not None:
                last_uploaded_size.pop(existing_key, None)

    backend.put_state()
    return written


def cleanup_backend(backend: StorageBackend) -> int:
    """
    Delete everything in the backend (corpus + control files). Called when
    share-mode goes active → inactive (last /unshare wins).

    Uses backend.purge_owner() (single call) if available — HTTPBackend
    supports this. Otherwise falls back to a list+delete loop.
    """
    purge = getattr(backend, "purge_owner", None)
    if callable(purge):
        try:
            return purge()
        except Exception as e:
            print(f"[sync] purge_owner failed, falling back to loop: {e}", flush=True)

    n = 0
    for key in backend.list_keys():
        backend.delete_key(key)
        n += 1
    for control in (ACTIVE_SESSIONS_FILENAME, SYNC_STATE_FILENAME):
        try:
            backend.delete_key(control)
        except Exception:
            pass
    return n


class DaemonState:
    """
    Shared state across all source watchers. Owns the shared-session set,
    runs cross-source initial_sync / cleanup on transitions.
    """

    def __init__(self, workspace: Path, sources: list[Path], backend: StorageBackend):
        self.workspace = workspace
        self.sources = sources
        self.backend = backend
        self._lock = threading.Lock()
        self.shared_session_info: dict[str, list[str]] = read_shared_session_info(workspace)
        # Per-file last-uploaded byte size. Used to send delta (append) uploads
        # instead of re-sending the entire jsonl on every Claude turn. In-memory
        # only — on daemon restart we don't know server sizes, so the first
        # event for each file falls back to full upload (which then re-seeds).
        self.last_uploaded_size: dict[str, int] = {}
        # Per-session debounce timers for silent background distillation.
        self._distill_timers: dict[str, threading.Timer] = {}

    @property
    def is_active(self) -> bool:
        return bool(self.shared_session_info)

    def schedule_distill(self, session_jsonl: Path, session_id: str) -> None:
        """Debounced, detached, opt-in. ~45s after a shared session's last
        change, spawn `teammate-sync distill` as a fully detached background
        process — silent, never blocks the watcher, output to its own log.
        No-op unless the user has opted in (distill.enabled flag)."""
        from . import cli
        if not cli.distill_enabled():
            return
        existing = self._distill_timers.get(session_id)
        if existing:
            existing.cancel()

        def _fire():
            try:
                binary = os.environ.get("TEAMMATE_SYNC_BIN") or sys.executable
                knowledge = self.workspace / "knowledge.md"
                if binary == sys.executable:
                    cmd = [sys.executable, "-m", "teammate_sync.cli", "distill"]
                else:
                    cmd = [binary, "distill"]
                cmd += ["--session", str(session_jsonl),
                        "--out", str(knowledge),
                        "--session-id", session_id]
                print(f"[knowledge] distilling decisions from session {session_id[:8]} → knowledge.md", flush=True)
                # Fully detached: own session, stdout/stderr discarded (the
                # distiller writes its own log). The daemon never waits.
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                print(f"[sync] distill spawn failed (non-fatal): {e}", flush=True)

        t = threading.Timer(45.0, _fire)
        t.daemon = True
        self._distill_timers[session_id] = t
        t.start()

    @property
    def shared_session_ids(self) -> set[str]:
        return set(self.shared_session_info.keys())

    def recipients_for(self, session_id: str) -> list[str] | None:
        """Return the recipient list for a /share'd session, or None if not shared."""
        return self.shared_session_info.get(session_id)

    def reconcile_shared_sessions(self) -> None:
        """
        Called when .shared-sessions.json changes. Update the local registry of
        which sessions are /share'd with whom — federated answering and distill-
        gating read it. NO backend file sync: nothing is uploaded in the
        federated model (only knowledge.md, via the distiller's /v1/knowledge
        push).
        """
        with self._lock:
            new_info = read_shared_session_info(self.workspace)
            old_set = set(self.shared_session_info.keys())
            new_set = set(new_info.keys())
            self.shared_session_info = new_info
            if new_set != old_set:
                print(f"[sync] shared sessions now: {sorted(new_set) or '(none)'}", flush=True)


class MirrorHandler(FileSystemEventHandler):
    """
    Per-source watcher. Uploads events to backend when share-mode is active
    AND (for non-workspace sources) the file belongs to a /share'd session.
    """

    def __init__(self, source: Path, state: DaemonState, is_workspace: bool):
        self.source = source
        self.state = state
        # If True, this handler watches the workspace dir — all non-skip files
        # sync. If False, it watches a sessions dir — only /share'd session
        # jsonls sync.
        self.is_workspace = is_workspace
        self._lock = threading.Lock()
        self._pending_timer: threading.Timer | None = None

    def dispatch(self, event):
        # A transient network error (DNS blip, backend hiccup) raised inside an
        # on_* handler would otherwise propagate into watchdog's observer thread
        # and kill it permanently — leaving the daemon "up" but no longer
        # syncing. Catch it here so the watcher survives and retries on the next
        # filesystem event.
        try:
            super().dispatch(event)
        except Exception as e:
            path = getattr(event, "dest_path", None) or getattr(event, "src_path", "?")
            print(f"[sync] watcher error on {path}: {e}; continuing", flush=True)

    def _rel_key(self, src_path: str) -> str:
        return str(Path(src_path).relative_to(self.source))

    def _should_skip(self, src_path: str) -> bool:
        return Path(src_path).name.endswith(SKIP_SUFFIXES)

    def _is_share_state_file(self, src_path: str) -> bool:
        return Path(src_path).name == SHARED_SESSIONS_FILENAME

    def _is_allowed_for_session_filter(self, key: str) -> bool:
        """
        For sessions sources (not workspace): allow only files whose
        session_id is currently /share'd. Skip everything else.
        """
        if self.is_workspace:
            return True
        sid = extract_session_id_from_path(key)
        if sid is None:
            return False  # not a session jsonl — skip silently
        return sid in self.state.shared_session_ids

    def _schedule_state_write(self) -> None:
        with self._lock:
            if self._pending_timer:
                self._pending_timer.cancel()
            self._pending_timer = threading.Timer(
                DEBOUNCE_SECONDS,
                self.state.backend.put_state,
            )
            self._pending_timer.daemon = True
            self._pending_timer.start()

    def _upload(self, src_path: str, label: str) -> None:
        # FEDERATED MODEL: nothing is uploaded to the backend. Raw transcripts,
        # active-sessions, logs, CLAUDE.md — all stay LOCAL. The only thing that
        # reaches the backend is knowledge.md, pushed to /v1/knowledge by the
        # distiller. So this just triggers distillation when a /share'd session's
        # transcript changes. (Distilling only /share'd sessions keeps personal
        # sessions out of the org-wide knowledge.md.)
        try:
            if self.is_workspace:
                return  # workspace files never leave the machine
            key = self._rel_key(src_path)
            sid = extract_session_id_from_path(key)
            if sid and sid in self.state.shared_session_ids:
                self.state.schedule_distill(Path(src_path), sid)
        except Exception as e:
            print(f"[sync] watch error on {label} {src_path}: {e}", flush=True)

    def on_created(self, event):
        if event.is_directory or self._should_skip(event.src_path):
            return
        if self._is_share_state_file(event.src_path):
            self.state.reconcile_shared_sessions()
            return
        if not self.state.is_active:
            return
        key = self._rel_key(event.src_path)
        if not self._is_allowed_for_session_filter(key):
            return
        self._upload(event.src_path, "created")

    def on_modified(self, event):
        if event.is_directory or self._should_skip(event.src_path):
            return
        if self._is_share_state_file(event.src_path):
            self.state.reconcile_shared_sessions()
            return
        if not self.state.is_active:
            return
        key = self._rel_key(event.src_path)
        if not self._is_allowed_for_session_filter(key):
            return
        self._upload(event.src_path, "modified")

    def on_deleted(self, event):
        # Federated model uploads nothing, so there's nothing to delete on the
        # backend. Only react to the share-state file changing.
        if event.is_directory:
            return
        if self._is_share_state_file(event.src_path):
            self.state.reconcile_shared_sessions()

    def on_moved(self, event):
        if event.is_directory:
            return
        if self._is_share_state_file(event.dest_path) or self._is_share_state_file(event.src_path):
            self.state.reconcile_shared_sessions()
            return
        # A /share'd session's transcript moved/renamed — re-distill it.
        try:
            if not self.is_workspace:
                sid = extract_session_id_from_path(self._rel_key(event.dest_path))
                if sid and sid in self.state.shared_session_ids:
                    self.state.schedule_distill(Path(event.dest_path), sid)
        except Exception as e:
            print(f"[sync] watch error on move: {e}", flush=True)


def main() -> int:
    print("[sync] main() entered", flush=True)

    # The daemon is now workspace-less. It watches two fixed locations:
    #   1. ~/.teammate-sync/state/   — for shared-sessions.json + active-sessions.json
    #      (subdir, NOT ~/.teammate-sync/ root — auth.json must stay OUT of
    #      the synced tree)
    #   2. ~/.claude/projects/       — every Claude Code session jsonl. Per-session
    #      filter ensures only /share'd sessions actually upload.
    state_dir = Path("~/.teammate-sync/state").expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)

    sessions_dir = Path("~/.claude/projects").expanduser()
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Allow override via argv for testing
    sources = [Path(a).expanduser().resolve() for a in sys.argv[1:]] or [state_dir, sessions_dir]
    for src in sources:
        src.mkdir(parents=True, exist_ok=True)

    workspace = sources[0]
    print(f"[sync] resolving backend (this may briefly hit the network)...", flush=True)

    try:
        backend = make_backend_from_env()
    except (ValueError, FileNotFoundError) as e:
        print(f"Backend configuration error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Backend setup failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("[sync] daemon starting", flush=True)
    print(f"[sync] state dir: {workspace}", flush=True)
    for src in sources[1:]:
        print(f"[sync] watching:  {src}", flush=True)
    print(f"[sync] backend:   {backend!r}", flush=True)

    state = DaemonState(workspace, sources, backend)

    # FEDERATED MODEL: the daemon no longer uploads files. One-time, purge any
    # files left on the backend by older versions (raw transcripts, logs, etc.)
    # so previously-synced data doesn't linger. Best-effort.
    try:
        n = cleanup_backend(backend)
        if n:
            print(f"[sync] purged {n} legacy file(s) from backend (federated model uploads nothing)", flush=True)
    except Exception as e:
        print(f"[sync] legacy purge skipped (non-fatal): {e}", flush=True)

    if state.is_active:
        print(f"[sync] {len(state.shared_session_info)} session(s) /connect-ed; "
              f"distilling decisions locally, answering queries federated.", flush=True)
    else:
        print("[sync] idle — nothing shared yet. Run /connect <teammate> in a Claude Code session to start sharing.", flush=True)

    observer = Observer()
    for i, src in enumerate(sources):
        handler = MirrorHandler(src, state, is_workspace=(i == 0))
        observer.schedule(handler, str(src), recursive=True)
    observer.start()

    shutdown = threading.Event()

    def _signal_handler(signum, frame):
        print(f"[sync] received signal {signum}, shutting down", flush=True)
        shutdown.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # ── Federated query answer-loop ──────────────────────────────────────────
    # Poll for live questions teammates addressed to me, answer each locally
    # from my real session (raw transcript never leaves this machine), post back
    # just the answer. Runs in a background thread; fail-safe per cycle.
    def _query_poller():
        from . import federated
        from .auth import read_claude_token
        def _claude():
            from .cli import _resolve_claude_binary
            return _resolve_claude_binary()
        while not shutdown.is_set():
            try:
                for line in federated.poll_and_answer(
                    read_claude_token, backend.org, backend.backend_url, _claude,
                ) or []:
                    print(line, flush=True)  # surface /ask activity in the log
            except Exception as e:
                print(f"[sync] query poll error (non-fatal): {e}", flush=True)
            shutdown.wait(5)

    poller = threading.Thread(target=_query_poller, daemon=True)
    poller.start()

    try:
        shutdown.wait()
    finally:
        observer.stop()
        observer.join()
        print("[sync] daemon stopped", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
