"""
Storage backend abstraction for teammate-sync.

Three implementations:
- LocalBackend  — writes to a local filesystem directory (dev / tests)
- S3Backend     — direct-to-S3 (legacy from Phase 3a, kept for hosted-self-deploy)
- HTTPBackend   — calls the teammate-sync cloud backend (Phase 5+, the default)

Selected via env var TEAMMATE_BACKEND (local|s3|cloud). The daemon and MCP
server both construct the backend the same way, so they always agree.

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
    def put_bytes(
        self,
        key: str,
        data: bytes,
        session_id: str | None = None,
        recipients: list[str] | None = None,
    ) -> None:
        """
        Write bytes at key.

        For session-jsonl uploads to the cloud backend, the caller passes
        session_id + recipients so the backend can register the per-session
        ACL row alongside the file. Local/S3 backends ignore these
        parameters (they have no notion of ACL beyond owner).
        """

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

    def put_bytes(
        self,
        key: str,
        data: bytes,
        session_id: str | None = None,
        recipients: list[str] | None = None,
    ) -> None:
        # session_id + recipients ignored — local backend has no ACL.
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

    def put_bytes(
        self,
        key: str,
        data: bytes,
        session_id: str | None = None,
        recipients: list[str] | None = None,
    ) -> None:
        # session_id + recipients ignored — S3 backend has no ACL beyond bucket policy.
        self.s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def delete_key(self, key: str) -> None:
        # delete_object is idempotent
        self.s3.delete_object(Bucket=self.bucket, Key=self._key(key))

    def __repr__(self) -> str:
        return f"S3Backend(bucket={self.bucket}, prefix={self.prefix!r}, region={self._region})"


class HTTPBackend(StorageBackend):
    """
    Storage backend that talks to the teammate-sync cloud backend over HTTP.

    DAEMON use: teammate=<my own github handle>, since daemon writes its own
        files. The backend authoritatively binds the owner to the auth token,
        so wrong values get rejected — but we still need it right for the
        list/delete paths which take teammate as a parameter.

    MCP use: teammate=<the queried engineer>. Reads scoped to that teammate.
        Writes would 403 (owner mismatch) — which is correct, MCP never writes.

    All requests authenticated with a GitHub OAuth access token via Bearer.
    """

    def __init__(self, backend_url: str, token: str, org: str, teammate: str):
        import httpx  # lazy

        self.backend_url = backend_url.rstrip("/")
        self.org = org
        self.teammate = teammate
        self._token = token
        self._client = httpx.Client(
            base_url=self.backend_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    def _basename(self, key: str) -> str:
        return Path(key).name

    def list_keys(self) -> list[str]:
        r = self._client.get(
            "/v1/files",
            params={"org": self.org, "teammate": self.teammate},
        )
        r.raise_for_status()
        keys = [f["path"] for f in r.json().get("files", [])]
        # Filter control + skip-suffix files client-side (same semantics as
        # LocalBackend and S3Backend).
        return [
            k for k in keys
            if self._basename(k) not in CONTROL_FILES
            and not self._basename(k).endswith(SKIP_SUFFIXES)
        ]

    def get_bytes(self, key: str) -> bytes | None:
        r = self._client.get(
            "/v1/files/get",
            params={"org": self.org, "teammate": self.teammate, "path": key},
        )
        # 404 = file genuinely missing. 403 = ACL forbids this read — treat
        # as "not visible" rather than crashing the synthesis pipeline.
        if r.status_code in (403, 404):
            return None
        r.raise_for_status()
        return r.content

    def put_bytes(
        self,
        key: str,
        data: bytes,
        session_id: str | None = None,
        recipients: list[str] | None = None,
    ) -> None:
        """
        Upload one file. For session jsonl files, pass session_id + recipients
        so the backend registers per-session ACL rows alongside the file.
        Non-session files (CLAUDE.md, scratch notes) leave both None.
        """
        import base64

        payload = {
            "org": self.org,
            "path": key,
            "content_b64": base64.b64encode(data).decode("ascii"),
        }
        if session_id:
            payload["session_id"] = session_id
        if recipients:
            payload["recipients"] = recipients
        r = self._client.post("/v1/files", json=payload)
        r.raise_for_status()

    def delete_key(self, key: str) -> None:
        r = self._client.delete(
            "/v1/files",
            params={"org": self.org, "path": key},
        )
        if r.status_code != 404:
            r.raise_for_status()

    def get_state(self) -> dict | None:
        r = self._client.get(
            "/v1/state",
            params={"org": self.org, "teammate": self.teammate},
        )
        r.raise_for_status()
        data = r.json()
        epoch = data.get("last_sync_epoch")
        if not isinstance(epoch, (int, float)):
            return None
        return {"last_sync_epoch": epoch, "last_sync_iso": ""}

    def put_state(self) -> None:
        r = self._client.post("/v1/state", params={"org": self.org})
        r.raise_for_status()

    def purge_owner(self) -> int:
        """
        Single-call optimization: delete every file the caller owns in this
        workspace. Used by the daemon's cleanup when share-mode flips off.
        """
        r = self._client.delete("/v1/files/purge", params={"org": self.org})
        r.raise_for_status()
        return r.json().get("deleted", 0)

    # ─── v0.2: connections, dump, dashboard ────────────────────────────────

    def list_connections(self) -> dict:
        r = self._client.get("/v1/connections", params={"org": self.org})
        r.raise_for_status()
        return r.json()

    def request_connection(self, peer: str) -> dict:
        r = self._client.post(
            "/v1/connections/request",
            json={"org": self.org, "peer": peer},
        )
        r.raise_for_status()
        return r.json()

    def accept_connection(self, peer: str) -> dict:
        r = self._client.post(
            "/v1/connections/accept",
            json={"org": self.org, "peer": peer},
        )
        r.raise_for_status()
        return r.json()

    def decline_connection(self, peer: str) -> dict:
        r = self._client.post(
            "/v1/connections/decline",
            json={"org": self.org, "peer": peer},
        )
        r.raise_for_status()
        return r.json()

    def disconnect_connection(self, peer: str) -> dict:
        r = self._client.post(
            "/v1/connections/disconnect",
            json={"org": self.org, "peer": peer},
        )
        r.raise_for_status()
        return r.json()

    def unshare_session(self, session_id: str, recipient: str | None = None) -> dict:
        payload = {"org": self.org, "session_id": session_id}
        if recipient is not None:
            payload["recipient"] = recipient
        r = self._client.post("/v1/sessions/unshare", json=payload)
        r.raise_for_status()
        return r.json()

    def dump(self, teammate: str, session_id: str | None = None) -> bytes | dict:
        params = {"org": self.org, "teammate": teammate}
        if session_id:
            params["session_id"] = session_id
        r = self._client.get("/v1/dump", params=params)
        r.raise_for_status()
        if session_id:
            return r.content
        return r.json()

    def dashboard(self) -> dict:
        r = self._client.get("/v1/dashboard", params={"org": self.org})
        r.raise_for_status()
        return r.json()

    def __repr__(self) -> str:
        return f"HTTPBackend(url={self.backend_url}, org={self.org}, teammate={self.teammate})"


def make_backend_from_env() -> StorageBackend:
    """
    Construct the WRITER backend based on env vars. Used by the daemon to
    decide where its own teammate's content goes.

    Backend selection via TEAMMATE_BACKEND:
      - "cloud" (DEFAULT): talks to teammate-sync cloud backend. Reads auth
        from ~/.teammate-sync/auth.json (see auth.py).
      - "s3": legacy direct-to-S3 (set TEAMMATE_S3_BUCKET + TEAMMATE_HANDLE).
      - "local": local filesystem (set TEAMMATE_CORPUS_DIR).
    """
    import httpx  # lazy

    backend = os.environ.get("TEAMMATE_BACKEND", "cloud").lower()

    if backend == "local":
        target = os.environ.get("TEAMMATE_CORPUS_DIR", "./example_data")
        return LocalBackend(target)

    if backend == "s3":
        bucket = os.environ.get("TEAMMATE_S3_BUCKET")
        if not bucket:
            raise ValueError("TEAMMATE_S3_BUCKET must be set when TEAMMATE_BACKEND=s3")
        handle = os.environ.get("TEAMMATE_HANDLE")
        explicit_prefix = os.environ.get("TEAMMATE_S3_PREFIX")
        if handle:
            prefix = handle.strip("/") + "/"
        elif explicit_prefix is not None:
            prefix = explicit_prefix
        else:
            raise ValueError(
                "Either TEAMMATE_HANDLE (preferred) or TEAMMATE_S3_PREFIX "
                "must be set when TEAMMATE_BACKEND=s3"
            )
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        return S3Backend(bucket=bucket, prefix=prefix, region=region)

    if backend == "cloud":
        from .auth import read_auth

        auth = read_auth()  # raises with a clear message if missing
        # Resolve our own GitHub handle so the daemon's list/delete calls
        # target our own corpus (writes are owner-bound by the backend anyway).
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

    raise ValueError(f"Unknown TEAMMATE_BACKEND: {backend!r}. Use 'cloud', 's3', or 'local'.")


def make_s3_backend_for(handle: str) -> S3Backend:
    """
    Construct a READER backend for a specific teammate handle. Used by the
    MCP server to query any teammate by parameter.

    Reads bucket + region from env (TEAMMATE_S3_BUCKET, AWS_REGION) and
    composes the prefix from the handle.
    """
    bucket = os.environ.get("TEAMMATE_S3_BUCKET")
    if not bucket:
        raise ValueError("TEAMMATE_S3_BUCKET must be set")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    prefix = handle.strip("/") + "/"
    return S3Backend(bucket=bucket, prefix=prefix, region=region)


def list_s3_teammates() -> list[str]:
    """
    Discover available teammates by listing top-level "directories" in the
    configured S3 bucket. Used by the MCP server's list_teammates tool.
    Returns handles without trailing slash.
    """
    import boto3

    bucket = os.environ.get("TEAMMATE_S3_BUCKET")
    if not bucket:
        raise ValueError("TEAMMATE_S3_BUCKET must be set")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    s3 = boto3.client("s3", region_name=region)

    handles: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Delimiter="/"):
        for entry in page.get("CommonPrefixes", []) or []:
            p = entry.get("Prefix", "").rstrip("/")
            if p:
                handles.append(p)
    return sorted(handles)
