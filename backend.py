"""
Storage backend abstraction for teammate-sync.

Two implementations:
- LocalBackend  — writes to a local filesystem directory (Phase 2)
- S3Backend     — writes to an S3 bucket (Phase 3a, hosted)

Selected via env var TEAMMATE_BACKEND (local|s3). The daemon and MCP server
both construct the backend the same way from env, so they always agree on
where the corpus lives.

The interface is intentionally minimal — just key/value bytes — so adding
a new backend later (R2, GCS, etc.) is a small isolated change.
"""
import json
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path


SYNC_STATE_FILENAME = ".sync-state.json"
ACTIVE_SESSIONS_FILENAME = ".active-sessions.json"
SHARED_SESSIONS_FILENAME = ".shared-sessions.json"

# Control files are managed by the system (daemon writes sync state, hooks
# write active sessions, share-cli writes shared sessions). They're
# addressable via get_bytes/put_bytes but omitted from list_keys so the MCP
# doesn't try to render them as corpus content. The daemon also never
# uploads .shared-sessions.json to the backend — it's a local-only
# permission gate.
CONTROL_FILES = {
    SYNC_STATE_FILENAME,
    ACTIVE_SESSIONS_FILENAME,
    SHARED_SESSIONS_FILENAME,
}
# Transient/process-local files that should never appear in the corpus
# (fcntl lock files, atomic-write tempfiles).
SKIP_SUFFIXES = (".lock", ".tmp")


class StorageBackend(ABC):
    """Abstract storage backend for mirroring a teammate's working corpus."""

    @abstractmethod
    def list_keys(self) -> list[str]:
        """List all keys (relative paths) in the backend, excluding the state file."""

    @abstractmethod
    def get_bytes(self, key: str) -> bytes | None:
        """Read bytes at key. Returns None if missing."""

    @abstractmethod
    def put_bytes(self, key: str, data: bytes) -> None:
        """Write bytes at key."""

    @abstractmethod
    def delete_key(self, key: str) -> None:
        """Delete the object at key. No-op if missing."""

    def get_state(self) -> dict | None:
        raw = self.get_bytes(SYNC_STATE_FILENAME)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def put_state(self) -> None:
        state = {
            "last_sync_iso": datetime.now(timezone.utc).isoformat(),
            "last_sync_epoch": time.time(),
        }
        self.put_bytes(
            SYNC_STATE_FILENAME,
            json.dumps(state, indent=2).encode("utf-8"),
        )


class LocalBackend(StorageBackend):
    def __init__(self, target_dir: str | Path):
        self.target = Path(target_dir).expanduser().resolve()
        self.target.mkdir(parents=True, exist_ok=True)

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        for f in self.target.rglob("*"):
            if not f.is_file():
                continue
            if f.name in CONTROL_FILES or f.name.endswith(SKIP_SUFFIXES):
                continue
            keys.append(str(f.relative_to(self.target)))
        return sorted(keys)

    def get_bytes(self, key: str) -> bytes | None:
        path = self.target / key
        if not path.exists():
            return None
        return path.read_bytes()

    def put_bytes(self, key: str, data: bytes) -> None:
        path = self.target / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete_key(self, key: str) -> None:
        path = self.target / key
        if path.exists():
            path.unlink()

    def __repr__(self) -> str:
        return f"LocalBackend(target={self.target})"


class S3Backend(StorageBackend):
    def __init__(self, bucket: str, prefix: str = "", region: str | None = None):
        import boto3  # lazy import — only required if S3 backend is in use

        self.bucket = bucket
        # Normalize prefix: no leading "/", always trailing "/" (or empty)
        normalized = prefix.lstrip("/").rstrip("/")
        self.prefix = (normalized + "/") if normalized else ""
        self._region = region
        self.s3 = boto3.client("s3", region_name=region)

    def _key(self, key: str) -> str:
        return self.prefix + key

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        kwargs = {"Bucket": self.bucket}
        if self.prefix:
            kwargs["Prefix"] = self.prefix
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []) or []:
                full_key = obj["Key"]
                rel = full_key[len(self.prefix):] if self.prefix else full_key
                if rel in CONTROL_FILES or rel == "" or rel.endswith(SKIP_SUFFIXES):
                    continue
                keys.append(rel)
        return sorted(keys)

    def get_bytes(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=self._key(key))
            return resp["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def put_bytes(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def delete_key(self, key: str) -> None:
        # delete_object is idempotent
        self.s3.delete_object(Bucket=self.bucket, Key=self._key(key))

    def __repr__(self) -> str:
        return f"S3Backend(bucket={self.bucket}, prefix={self.prefix!r}, region={self._region})"


def make_backend_from_env() -> StorageBackend:
    """Construct the backend based on env vars. Shared by daemon and MCP."""
    backend = os.environ.get("TEAMMATE_BACKEND", "local").lower()

    if backend == "local":
        target = os.environ.get("TEAMMATE_CORPUS_DIR", "./example_data")
        return LocalBackend(target)

    if backend == "s3":
        bucket = os.environ.get("TEAMMATE_S3_BUCKET")
        if not bucket:
            raise ValueError("TEAMMATE_S3_BUCKET must be set when TEAMMATE_BACKEND=s3")
        prefix = os.environ.get("TEAMMATE_S3_PREFIX", "")
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        return S3Backend(bucket=bucket, prefix=prefix, region=region)

    raise ValueError(f"Unknown TEAMMATE_BACKEND: {backend!r}. Use 'local' or 's3'.")
