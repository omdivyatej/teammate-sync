"""
Auth helper for teammate-sync clients (daemon + MCP).

Reads ~/.teammate-sync/auth.json, written by `teammate-sync init` (Phase 5c).
Format:
    {
      "token":         "gho_...",                   # GitHub OAuth access token
      "org":           "SolarCheckr",               # workspace = GitHub org name
      "backend_url":   "https://teammate-sync-backend.fly.dev",
      "anthropic_key": "sk-ant-..."                 # for MCP server's synthesis calls
    }
"""
import json
import os
from pathlib import Path


DEFAULT_AUTH_FILE = "~/.teammate-sync/auth.json"
DEFAULT_BACKEND_URL = "https://teammate-sync-backend.fly.dev"


def auth_file_path() -> Path:
    return Path(os.environ.get("TEAMMATE_AUTH_FILE", DEFAULT_AUTH_FILE)).expanduser()


def claude_token_path() -> Path:
    """Where the headless Claude OAuth token (from `claude setup-token`) lives.
    Kept next to auth.json — OUTSIDE ~/.teammate-sync/state/ (the synced tree) —
    so this credential is NEVER uploaded. Read by the distiller to run
    `claude -p` in the background daemon, which can't reach the interactive
    Keychain login."""
    return auth_file_path().parent / "claude-token"


def read_claude_token() -> str | None:
    p = claude_token_path()
    if not p.exists():
        return None
    tok = p.read_text().strip()
    return tok or None


def write_claude_token(token: str) -> Path:
    """Store the headless token with 0600 perms. Never logged."""
    p = claude_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token.strip())
    p.chmod(0o600)
    return p


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


def read_anthropic_key() -> str:
    """
    Return the Anthropic API key the MCP server uses for synthesis calls.

    Priority: auth.json field > ANTHROPIC_API_KEY env var (fallback for tests).
    Raises ValueError with a clear remediation path if neither is set.
    """
    try:
        data = json.loads(auth_file_path().read_text())
        key = data.get("anthropic_key")
        if key:
            return key
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return env_key

    raise ValueError(
        f"No Anthropic API key found.\n"
        f"  Run `teammate-sync init` to store one (interactive prompt), or\n"
        f"  set ANTHROPIC_API_KEY in the environment (mostly for testing)."
    )


def write_auth(
    token: str,
    org: str,
    backend_url: str = DEFAULT_BACKEND_URL,
    anthropic_key: str | None = None,
    github_handle: str | None = None,
) -> Path:
    """Persist the auth file with restrictive permissions (0600).

    Preserves any existing fields not overwritten by this call — re-running
    init with a different field set won't blow away the others (e.g.,
    refreshing the GitHub token shouldn't wipe the stored Anthropic key).

    github_handle is cached so the app can construct its backend offline,
    without a mandatory /v1/me network call on every launch."""
    path = auth_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            existing = {}

    payload = {**existing, "token": token, "org": org, "backend_url": backend_url}
    if anthropic_key is not None:
        payload["anthropic_key"] = anthropic_key
    if github_handle is not None:
        payload["github_handle"] = github_handle

    path.write_text(json.dumps(payload, indent=2))
    path.chmod(0o600)
    return path
