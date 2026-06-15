"""
teammate-sync cloud backend.

Stateless GitHub-OAuth identity + SQLite-backed file storage so engineers'
daemons can sync into a single shared store, scoped to their GitHub org.

Auth model: clients send `Authorization: Bearer <github-access-token>`.
We validate via api.github.com on every request (small cache for performance).
Workspace = GitHub organization. Membership = `gh api orgs/<X>/members`.

Endpoints:
  /health
  /auth/github/{login,callback}      browser-driven OAuth
  /v1/me                              who am I (verified via GitHub)
  /v1/teammates?org=X                 list workspace members
  /v1/files?teammate=X                list files in a teammate's corpus
  /v1/files/get?teammate=X&path=Y     get a file's bytes
  /v1/files                           POST upload a file (owned by caller)
  /v1/files                           DELETE one file (owned by caller)
  /v1/files/purge                     DELETE all caller's files in a workspace
  /v1/state?teammate=X                GET freshness for a teammate
  /v1/state                           POST mark caller's corpus as just-synced

All /v1/files and /v1/state endpoints enforce: caller must be a member of
the workspace (org) they're operating on. Reads can target any teammate in
that workspace; writes always target the caller's own handle.
"""
import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

import storage


GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/github/callback")
GITHUB_API = "https://api.github.com"
GITHUB_OAUTH = "https://github.com/login/oauth"

GITHUB_SCOPES = "read:user user:email read:org"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.init_db()
    yield


app = FastAPI(
    title="teammate-sync cloud backend",
    description="GitHub-OAuth identity + SQLite-backed file storage for teammate-sync clients.",
    version="0.2.0",
    lifespan=lifespan,
)


# ─── Models ────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    version: str


class MeResponse(BaseModel):
    github_handle: str
    email: str | None
    name: str | None


class Teammate(BaseModel):
    github_handle: str


class TeammatesResponse(BaseModel):
    org: str
    teammates: list[Teammate]


class FileEntry(BaseModel):
    path: str
    size: int
    updated_at: float


class FileListResponse(BaseModel):
    org: str
    teammate: str
    files: list[FileEntry]


class FileUploadRequest(BaseModel):
    org: str
    path: str
    content_b64: str  # base64-encoded bytes


class SyncStateResponse(BaseModel):
    org: str
    teammate: str
    last_sync_epoch: float | None


# ─── Auth helpers ──────────────────────────────────────────────────────────

GITHUB_USER_CACHE: dict[str, dict] = {}
GITHUB_MEMBERSHIP_CACHE: dict[tuple[str, str], bool] = {}  # (token, org) → is_member


async def github_user_from_bearer(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """Validate the Bearer token by calling GitHub /user. Returns user object."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.split(None, 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty bearer token")

    cached = GITHUB_USER_CACHE.get(token)
    if cached:
        return cached

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{GITHUB_API}/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="GitHub rejected this token")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"GitHub /user failed: {r.status_code}")

    user = r.json()
    if not user.get("email"):
        async with httpx.AsyncClient(timeout=10) as client:
            er = await client.get(
                f"{GITHUB_API}/user/emails",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
        if er.status_code == 200:
            primary = next((e for e in er.json() if e.get("primary") and e.get("verified")), None)
            if primary:
                user["email"] = primary["email"]

    user["__token__"] = token  # stash so downstream handlers can reuse for membership checks
    GITHUB_USER_CACHE[token] = user
    return user


async def require_workspace_member(user: dict, org: str) -> None:
    """Verify the authenticated user is a member of the given GitHub org. 403 if not."""
    token = user["__token__"]
    cache_key = (token, org)
    if GITHUB_MEMBERSHIP_CACHE.get(cache_key):
        return
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{GITHUB_API}/orgs/{org}/members/{user['login']}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
    if r.status_code != 204:
        raise HTTPException(
            status_code=403,
            detail=f"@{user['login']} is not a member of org '{org}' (or the OAuth app needs org approval)",
        )
    GITHUB_MEMBERSHIP_CACHE[cache_key] = True


# ─── Health + OAuth ────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=app.version)


@app.get("/auth/github/login")
def github_login(redirect_uri: str | None = None):
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Server missing GITHUB_CLIENT_ID")

    from urllib.parse import urlencode

    state = secrets.token_urlsafe(24)
    if redirect_uri:
        state = f"{state}|{redirect_uri}"

    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": GITHUB_SCOPES,
        "state": state,
        "allow_signup": "true",
    }
    return RedirectResponse(url=f"{GITHUB_OAUTH}/authorize?{urlencode(params)}")


@app.get("/auth/github/callback")
async def github_callback(code: str, state: str = ""):
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Server missing GitHub OAuth credentials")

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{GITHUB_OAUTH}/access_token",
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": OAUTH_REDIRECT_URI,
            },
            headers={"Accept": "application/json"},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"GitHub token exchange failed: {r.status_code}")
    payload = r.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail=f"No access_token in GitHub response: {payload}")

    cli_redirect = ""
    if "|" in state:
        _, cli_redirect = state.split("|", 1)

    if cli_redirect:
        from urllib.parse import urlencode
        return RedirectResponse(url=f"{cli_redirect}?{urlencode({'access_token': access_token})}")

    html = f"""
    <!doctype html><html><body style="font-family: -apple-system, sans-serif; max-width: 640px; margin: 4em auto; padding: 1em;">
      <h2>teammate-sync — signed in</h2>
      <p>Paste this into your terminal:</p>
      <pre style="background:#f6f8fa;padding:1em;border-radius:6px;user-select:all;">teammate-sync auth set-token {access_token}</pre>
      <p style="color:#555;">You can close this tab.</p>
    </body></html>
    """
    return HTMLResponse(content=html)


# ─── Identity + teammates ──────────────────────────────────────────────────

@app.get("/v1/me", response_model=MeResponse)
async def me(user: dict = Depends(github_user_from_bearer)) -> MeResponse:
    return MeResponse(
        github_handle=user.get("login", ""),
        email=user.get("email"),
        name=user.get("name"),
    )


@app.get("/v1/teammates", response_model=TeammatesResponse)
async def teammates(
    org: str,
    user: dict = Depends(github_user_from_bearer),
) -> TeammatesResponse:
    await require_workspace_member(user, org)
    token = user["__token__"]

    members: list[Teammate] = []
    async with httpx.AsyncClient(timeout=15) as client:
        page = 1
        while True:
            lr = await client.get(
                f"{GITHUB_API}/orgs/{org}/members",
                params={"per_page": 100, "page": page},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
            if lr.status_code != 200:
                raise HTTPException(status_code=502, detail=f"GitHub /members failed: {lr.status_code}")
            batch = lr.json()
            if not batch:
                break
            members.extend(Teammate(github_handle=m["login"]) for m in batch)
            if len(batch) < 100:
                break
            page += 1

    return TeammatesResponse(org=org, teammates=members)


# ─── File storage ──────────────────────────────────────────────────────────

@app.get("/v1/files", response_model=FileListResponse)
async def list_files(
    org: str,
    teammate: str,
    user: dict = Depends(github_user_from_bearer),
) -> FileListResponse:
    """List all files in a teammate's corpus. Caller must be in same org."""
    await require_workspace_member(user, org)
    entries = await storage.list_paths(org, teammate)
    return FileListResponse(
        org=org,
        teammate=teammate,
        files=[FileEntry(**e) for e in entries],
    )


@app.get("/v1/files/get")
async def get_file(
    org: str,
    teammate: str,
    path: str,
    user: dict = Depends(github_user_from_bearer),
):
    """Return raw bytes of one file. Caller must be in same org."""
    await require_workspace_member(user, org)
    content = await storage.get_file(org, teammate, path)
    if content is None:
        raise HTTPException(status_code=404, detail=f"File not found: {teammate}/{path}")
    return Response(content=content, media_type="application/octet-stream")


@app.post("/v1/files")
async def upload_file(
    req: FileUploadRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """Upload a file owned by the caller. Caller must be in the given org."""
    await require_workspace_member(user, req.org)
    import base64
    try:
        content = base64.b64decode(req.content_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"content_b64 not valid base64: {e}")
    await storage.put_file(req.org, user["login"], req.path, content)
    return {"ok": True, "owner": user["login"], "path": req.path, "size": len(content)}


@app.delete("/v1/files")
async def delete_one_file(
    org: str,
    path: str,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """Delete one of the caller's own files. Caller must be in the given org."""
    await require_workspace_member(user, org)
    n = await storage.delete_file(org, user["login"], path)
    return {"ok": True, "deleted": n}


@app.delete("/v1/files/purge")
async def purge_my_files(
    org: str,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """Delete EVERY file owned by the caller in the given workspace (used by /unshare)."""
    await require_workspace_member(user, org)
    n = await storage.purge_owner(org, user["login"])
    return {"ok": True, "deleted": n}


# ─── Sync state (freshness) ────────────────────────────────────────────────

@app.get("/v1/state", response_model=SyncStateResponse)
async def get_state(
    org: str,
    teammate: str,
    user: dict = Depends(github_user_from_bearer),
) -> SyncStateResponse:
    await require_workspace_member(user, org)
    state = await storage.get_sync_state(org, teammate)
    return SyncStateResponse(
        org=org,
        teammate=teammate,
        last_sync_epoch=state["last_sync_epoch"] if state else None,
    )


@app.post("/v1/state")
async def put_state(
    org: str,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """Mark caller's corpus as just-synced (now). Daemon calls this after batches."""
    await require_workspace_member(user, org)
    await storage.put_sync_state(org, user["login"])
    return {"ok": True}
