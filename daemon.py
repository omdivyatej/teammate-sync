#!/usr/bin/env python3
"""
teammate-sync daemon.

Watches a source directory (a teammate's Claude Code workspace) and mirrors
changes to whichever storage backend is configured via env vars (local
filesystem or S3).

Usage:
    python daemon.py <source-dir>

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

Example:
    TEAMMATE_BACKEND=s3 TEAMMATE_S3_BUCKET=teammate-sync-omdivyatej \\
      TEAMMATE_S3_PREFIX=saketh/ AWS_REGION=ap-southeast-1 \\
      python daemon.py ~/penguin-sim/.claude
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
# These should never reach the backend.
SKIP_SUFFIXES = (".lock", ".tmp")


def is_share_mode_active(source: Path) -> bool:
    """
    The daemon is gated on .shared-sessions.json — until at least one session
    in this workspace is marked /shared, the daemon does NOT upload anything
    to the backend. This is the privacy default.
    """
    shared_file = source / SHARED_SESSIONS_FILENAME
    if not shared_file.exists():
        return False
    try:
        data = json.loads(shared_file.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    sessions = data.get("sessions", [])
    return isinstance(sessions, list) and len(sessions) > 0


def initial_sync(source: Path, backend: StorageBackend) -> int:
    """
    Mirror source into backend. Returns count of files written.
    Deletes keys in the backend that are absent from the source (mirror semantics).
    Skips .shared-sessions.json (local-only permission gate, never uploaded).
    """
    written = 0
    source_keys: set[str] = set()

    for src_file in source.rglob("*"):
        if not src_file.is_file():
            continue
        if src_file.name.endswith(SKIP_SUFFIXES):
            continue
        if src_file.name == SHARED_SESSIONS_FILENAME:
            continue  # local-only, never upload
        rel = str(src_file.relative_to(source))
        source_keys.add(rel)
        backend.put_bytes(rel, src_file.read_bytes())
        written += 1

    for existing_key in backend.list_keys():
        if existing_key not in source_keys:
            backend.delete_key(existing_key)

    backend.put_state()
    return written


def cleanup_backend(backend: StorageBackend) -> int:
    """
    Delete every object the backend exposes plus the control files. Called
    when share-mode transitions from active → inactive (last /unshare wins).
    Returns count of deletes.
    """
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


class MirrorHandler(FileSystemEventHandler):
    def __init__(self, source: Path, backend: StorageBackend):
        self.source = source
        self.backend = backend
        self._lock = threading.Lock()
        self._pending_timer: threading.Timer | None = None
        # Track previous share-mode state to detect transitions
        self._was_active = is_share_mode_active(self.source)

    def _rel_key(self, src_path: str) -> str:
        return str(Path(src_path).relative_to(self.source))

    def _should_skip(self, src_path: str) -> bool:
        return Path(src_path).name.endswith(SKIP_SUFFIXES)

    def _is_share_state_file(self, src_path: str) -> bool:
        return Path(src_path).name == SHARED_SESSIONS_FILENAME

    def _handle_share_state_change(self) -> bool:
        """
        Re-read .shared-sessions.json and react to transitions:
          - inactive → active: initial_sync (populate backend)
          - active → inactive: cleanup_backend (wipe team store)
        Returns the new active state.
        """
        now_active = is_share_mode_active(self.source)
        if now_active and not self._was_active:
            print("[sync] share-mode ACTIVATED → uploading workspace to backend", flush=True)
            n = initial_sync(self.source, self.backend)
            print(f"[sync] initial sync complete: {n} files uploaded", flush=True)
        elif self._was_active and not now_active:
            print("[sync] share-mode DEACTIVATED → cleaning backend", flush=True)
            n = cleanup_backend(self.backend)
            print(f"[sync] backend cleaned: {n} objects removed", flush=True)
        self._was_active = now_active
        return now_active

    def _schedule_state_write(self) -> None:
        with self._lock:
            if self._pending_timer:
                self._pending_timer.cancel()
            self._pending_timer = threading.Timer(
                DEBOUNCE_SECONDS,
                self.backend.put_state,
            )
            self._pending_timer.daemon = True
            self._pending_timer.start()

    def _upload(self, src_path: str, label: str) -> None:
        try:
            key = self._rel_key(src_path)
            data = Path(src_path).read_bytes()
            self.backend.put_bytes(key, data)
            print(f"[sync] {label} → {key}", flush=True)
            self._schedule_state_write()
        except Exception as e:
            print(f"[sync] error on {label} {src_path}: {e}", flush=True)

    def on_created(self, event):
        if event.is_directory or self._should_skip(event.src_path):
            return
        if self._is_share_state_file(event.src_path):
            self._handle_share_state_change()
            return
        if not self._was_active:
            return
        self._upload(event.src_path, "created")

    def on_modified(self, event):
        if event.is_directory or self._should_skip(event.src_path):
            return
        if self._is_share_state_file(event.src_path):
            self._handle_share_state_change()
            return
        if not self._was_active:
            return
        self._upload(event.src_path, "modified")

    def on_deleted(self, event):
        if event.is_directory or self._should_skip(event.src_path):
            return
        if self._is_share_state_file(event.src_path):
            self._handle_share_state_change()
            return
        if not self._was_active:
            return
        try:
            key = self._rel_key(event.src_path)
            self.backend.delete_key(key)
            print(f"[sync] deleted → {key}", flush=True)
            self._schedule_state_write()
        except Exception as e:
            print(f"[sync] error on delete {event.src_path}: {e}", flush=True)

    def on_moved(self, event):
        if event.is_directory or self._should_skip(event.dest_path):
            return
        if self._is_share_state_file(event.dest_path) or self._is_share_state_file(event.src_path):
            self._handle_share_state_change()
            return
        if not self._was_active:
            return
        try:
            old_key = self._rel_key(event.src_path)
            new_key = self._rel_key(event.dest_path)
            # Backends don't have a native rename; copy then delete.
            data = Path(event.dest_path).read_bytes()
            self.backend.put_bytes(new_key, data)
            self.backend.delete_key(old_key)
            print(f"[sync] moved → {new_key}", flush=True)
            self._schedule_state_write()
        except Exception as e:
            print(f"[sync] error on move {event.src_path} → {event.dest_path}: {e}", flush=True)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: daemon.py <source-dir>", file=sys.stderr)
        print("  Target is configured via env vars (see file docstring).", file=sys.stderr)
        return 2

    source = Path(sys.argv[1]).expanduser().resolve()
    if not source.exists() or not source.is_dir():
        print(f"Source must be an existing directory: {source}", file=sys.stderr)
        return 1

    try:
        backend = make_backend_from_env()
    except ValueError as e:
        print(f"Backend configuration error: {e}", file=sys.stderr)
        return 1

    print("[sync] daemon starting", flush=True)
    print(f"[sync] source:  {source}", flush=True)
    print(f"[sync] backend: {backend!r}", flush=True)

    if is_share_mode_active(source):
        n = initial_sync(source, backend)
        print(f"[sync] initial sync complete: {n} files uploaded", flush=True)
    else:
        print("[sync] share-mode INACTIVE — daemon idle until /share is run", flush=True)

    handler = MirrorHandler(source, backend)
    observer = Observer()
    observer.schedule(handler, str(source), recursive=True)
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
