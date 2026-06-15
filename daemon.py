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
import sys
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from backend import (
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


def read_shared_session_ids(workspace: Path) -> set[str]:
    """
    Returns the set of session_ids currently /share'd in this workspace.
    Empty set if no .shared-sessions.json, or it's malformed, or has no sessions.
    """
    shared_file = workspace / SHARED_SESSIONS_FILENAME
    if not shared_file.exists():
        return set()
    try:
        data = json.loads(shared_file.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    sessions = data.get("sessions", [])
    if not isinstance(sessions, list):
        return set()
    return {
        s["session_id"]
        for s in sessions
        if isinstance(s, dict) and isinstance(s.get("session_id"), str)
    }


def is_share_mode_active(workspace: Path) -> bool:
    """Backwards-compat helper. True iff at least one session is /share'd."""
    return bool(read_shared_session_ids(workspace))


def initial_sync_all(
    sources: list[Path],
    backend: StorageBackend,
    shared_session_ids: set[str],
    workspace_index: int = 0,
) -> int:
    """
    Mirror sources into backend. Source at workspace_index is the "workspace"
    (CLAUDE.md, scratch notes, etc.) — every non-skipped file uploads. Other
    sources are "sessions" — only files whose basename is <uuid>.jsonl AND
    whose uuid is in shared_session_ids upload. Anything else is skipped
    (privacy: unrelated Claude Code work never leaves the machine).

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

            if not is_workspace:
                sid = extract_session_id_from_path(rel)
                if not sid or sid not in shared_session_ids:
                    continue  # not a /share'd session — skip

            all_source_keys.add(rel)
            backend.put_bytes(rel, src_file.read_bytes())
            written += 1

    for existing_key in backend.list_keys():
        if existing_key not in all_source_keys:
            backend.delete_key(existing_key)

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
        self.shared_session_ids: set[str] = read_shared_session_ids(workspace)

    @property
    def is_active(self) -> bool:
        return bool(self.shared_session_ids)

    def reconcile_shared_sessions(self) -> None:
        """
        Called when .shared-sessions.json changes. Computes the new
        shared-session set and reacts:
          - empty → has sessions: ACTIVATE + initial_sync_all
          - has sessions → empty: DEACTIVATE + cleanup_backend
          - changed contents (both non-empty): re-run initial_sync_all
            (its mirror semantics drop now-unshared session jsonls from
            backend in the deletion pass)
        """
        with self._lock:
            new_set = read_shared_session_ids(self.workspace)
            old_set = self.shared_session_ids

            if not old_set and new_set:
                print(
                    f"[sync] share-mode ACTIVATED ({len(new_set)} session(s) shared) → "
                    f"uploading workspace + shared sessions",
                    flush=True,
                )
                self.shared_session_ids = new_set
                n = initial_sync_all(self.sources, self.backend, new_set)
                print(f"[sync] initial sync complete: {n} files uploaded", flush=True)
            elif old_set and not new_set:
                print("[sync] share-mode DEACTIVATED → cleaning backend", flush=True)
                n = cleanup_backend(self.backend)
                self.shared_session_ids = new_set  # empty
                print(f"[sync] backend cleaned: {n} objects removed", flush=True)
            elif old_set != new_set:
                added = new_set - old_set
                removed = old_set - new_set
                print(
                    f"[sync] shared set changed (+{len(added)} -{len(removed)}) → reconciling",
                    flush=True,
                )
                self.shared_session_ids = new_set
                n = initial_sync_all(self.sources, self.backend, new_set)
                print(f"[sync] reconciliation complete: {n} files in sync", flush=True)


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
        try:
            key = self._rel_key(src_path)
            data = Path(src_path).read_bytes()
            self.state.backend.put_bytes(key, data)
            print(f"[sync] {label} → {key}", flush=True)
            self._schedule_state_write()
        except Exception as e:
            print(f"[sync] error on {label} {src_path}: {e}", flush=True)

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
        if event.is_directory or self._should_skip(event.src_path):
            return
        if self._is_share_state_file(event.src_path):
            self.state.reconcile_shared_sessions()
            return
        if not self.state.is_active:
            return
        key = self._rel_key(event.src_path)
        # Always allow deletes for backend hygiene — even if filter would
        # reject the upload, we don't want stale keys lingering.
        try:
            self.state.backend.delete_key(key)
            print(f"[sync] deleted → {key}", flush=True)
            self._schedule_state_write()
        except Exception as e:
            print(f"[sync] error on delete {event.src_path}: {e}", flush=True)

    def on_moved(self, event):
        if event.is_directory or self._should_skip(event.dest_path):
            return
        if self._is_share_state_file(event.dest_path) or self._is_share_state_file(event.src_path):
            self.state.reconcile_shared_sessions()
            return
        if not self.state.is_active:
            return
        try:
            old_key = self._rel_key(event.src_path)
            new_key = self._rel_key(event.dest_path)
            # Apply the filter to the destination — if the new path is not
            # an allowed session, just delete the old key (don't upload).
            if self._is_allowed_for_session_filter(new_key):
                data = Path(event.dest_path).read_bytes()
                self.state.backend.put_bytes(new_key, data)
            self.state.backend.delete_key(old_key)
            print(f"[sync] moved → {new_key}", flush=True)
            self._schedule_state_write()
        except Exception as e:
            print(f"[sync] error on move {event.src_path} → {event.dest_path}: {e}", flush=True)


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

    if state.is_active:
        n = initial_sync_all(sources, backend, state.shared_session_ids)
        print(
            f"[sync] initial sync complete: {n} files uploaded "
            f"({len(state.shared_session_ids)} session(s) /share'd)",
            flush=True,
        )
    else:
        print("[sync] share-mode INACTIVE — daemon idle until /share is run", flush=True)

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

    try:
        shutdown.wait()
    finally:
        observer.stop()
        observer.join()
        print("[sync] daemon stopped", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
