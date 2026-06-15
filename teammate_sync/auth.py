"""
Auth helper for teammate-sync clients (daemon + MCP).

Reads ~/.teammate-sync/auth.json, written by `teammate-sync init` (Phase 5c).
Format:
    {
      "token":       "gho_...",                   # GitHub OAuth access token
      "org":         "SolarCheckr",               # workspace = GitHub org name
      "backend_url": "https://teammate-sync-backend.fly.dev"
    }
"""
import json
import os
from pathlib import Path


DEFAULT_AUTH_FILE = "~/.teammate-sync/auth.json"
DEFAULT_BACKEND_URL = "https://teammate-sync-backend.fly.dev"


def auth_file_path() -> Path:
    return Path(os.environ.get("TEAMMATE_AUTH_FILE", DEFAULT_AUTH_FILE)).expanduser()


def read_auth() -> dict:
    """Read the auth file. Raises with a clear message if missing or malformed."""
    path = auth_file_path()
    if not path.exists():
        raise FileNotFoundError(
            f"teammate-sync auth file not found at {path}.\n"
            f"Run `teammate-sync init` to sign in with GitHub and create it."
        )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"Auth file {path} is not valid JSON: {e}")
    for required in ("token", "org"):
        if not data.get(required):
            raise ValueError(f"Auth file {path} missing required field: {required!r}")
    data.setdefault("backend_url", DEFAULT_BACKEND_URL)
    return data


def write_auth(token: str, org: str, backend_url: str = DEFAULT_BACKEND_URL) -> Path:
    """Persist the auth file with restrictive permissions (0600)."""
    path = auth_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"token": token, "org": org, "backend_url": backend_url}
    path.write_text(json.dumps(payload, indent=2))
    path.chmod(0o600)
    return path
