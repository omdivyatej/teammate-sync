"""
teammate-sync cloud backend (v0.2).

Stateless GitHub-OAuth identity + SQLite-backed file storage with per-session
ACL ("directed share" model). Engineers in the same workspace org choose
specifically who can see each session they share.

Auth model: clients send `Authorization: Bearer <github-access-token>`.
We validate via api.github.com on every request (small cache for performance).
Workspace = GitHub organization. Membership = `gh api orgs/<X>/members`.

Endpoints:
  /health
  /auth/github/{login,callback}        browser-driven OAuth

  /v1/me                                who am I (verified via GitHub)
  /v1/teammates?org=X                   list workspace members (org-wide)

  /v1/files?teammate=X                  list a teammate's files (ACL-gated)
  /v1/files/get?teammate=X&path=Y       get a file's bytes (ACL-gated)
  /v1/files                             POST upload a file (caller owns it).
                                        body: {org, path, content_b64,
                                               session_id?, recipients[]}
  /v1/files                             DELETE one file (caller owns it)
  /v1/files/purge?org=X                 DELETE all caller's files in workspace

  /v1/state?teammate=X                  GET freshness for a teammate
  /v1/state                             POST mark caller's corpus as just-synced

  /v1/connections?org=X                 GET list my connections
                                        (accepted + pending_incoming + pending_outgoing)
  /v1/connections/request               POST {org, peer} → send/refresh request
  /v1/connections/accept                POST {org, peer} → accept pending from peer
  /v1/connections/decline               POST {org, peer} → decline pending from peer
  /v1/connections/disconnect            POST {org, peer} → revoke trust both ways

  /v1/sessions/share                    POST {org, session_id, recipients[]} →
                                        mark session as shared with these handles.
  /v1/sessions/unshare                  POST {org, session_id, recipient?} →
                                        revoke; if recipient omitted, revoke from all.

  /v1/dump?teammate=X&session_id=Y      GET raw concatenation of a teammate's
                                        session for the /show slash command —
                                        no AI synthesis.
  /v1/dashboard?org=X                   GET aggregated state for the dashboard UI.
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
    description="GitHub-OAuth identity + SQLite-backed file storage with per-session ACL.",
    version="0.4.0",
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
    content_b64: str
    session_id: str | None = None
    recipients: list[str] = []


class FileAppendRequest(BaseModel):
    org: str
    path: str
    content_b64: str   # the DELTA bytes only, not the whole file
    expected_size: int # caller's belief of current server-side size
    session_id: str | None = None
    recipients: list[str] = []


class SyncStateResponse(BaseModel):
    org: str
    teammate: str
    last_sync_epoch: float | None


class ConnectionRequest(BaseModel):
    org: str
    peer: str


class SessionShareRequest(BaseModel):
    org: str
    session_id: str
    recipients: list[str] = []


class SessionUnshareRequest(BaseModel):
    org: str
    session_id: str
    recipient: str | None = None


class KnowledgeUploadRequest(BaseModel):
    org: str
    content: str


class KnowledgeDoc(BaseModel):
    engineer_handle: str
    content: str
    updated_at: float


class KnowledgeResponse(BaseModel):
    docs: list[KnowledgeDoc]


class QueryCreateRequest(BaseModel):
    org: str
    target: str
    question: str
    kind: str = "answer"
    session_id: str | None = None


class QueryAnswerRequest(BaseModel):
    org: str
    answer: str
    citation: str | None = None


# ─── Auth helpers ──────────────────────────────────────────────────────────

GITHUB_USER_CACHE: dict[str, dict] = {}
GITHUB_MEMBERSHIP_CACHE: dict[tuple[str, str], bool] = {}


async def github_user_from_bearer(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
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

    user["__token__"] = token
    GITHUB_USER_CACHE[token] = user
    return user


async def require_workspace_member(user: dict, org: str) -> None:
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
      <p>You can close this tab and return to your terminal.</p>
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
async def teammates(org: str, user: dict = Depends(github_user_from_bearer)) -> TeammatesResponse:
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


# ─── File storage (ACL-gated) ──────────────────────────────────────────────

@app.get("/v1/files", response_model=FileListResponse)
async def list_files(
    org: str,
    teammate: str,
    user: dict = Depends(github_user_from_bearer),
) -> FileListResponse:
    """List files in a teammate's corpus, filtered to those the caller is allowed to see."""
    await require_workspace_member(user, org)
    entries = await storage.list_visible_paths(org, user["login"], teammate)
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
    await require_workspace_member(user, org)
    if not await storage.can_read_file(org, user["login"], teammate, path):
        raise HTTPException(status_code=403, detail=f"Not allowed to read {teammate}/{path}")
    content = await storage.get_file(org, teammate, path)
    if content is None:
        raise HTTPException(status_code=404, detail=f"File not found: {teammate}/{path}")
    return Response(content=content, media_type="application/octet-stream")


@app.post("/v1/files")
async def upload_file(
    req: FileUploadRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """
    Upload a file owned by the caller. If session_id + recipients are
    supplied, also (re-)registers the session_shares rows.
    """
    await require_workspace_member(user, req.org)
    import base64
    try:
        content = base64.b64decode(req.content_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"content_b64 not valid base64: {e}")
    await storage.put_file(req.org, user["login"], req.path, content)
    n_shares = 0
    if req.session_id and req.recipients:
        n_shares = await storage.share_session(
            req.org, user["login"], req.session_id, req.recipients
        )
    return {
        "ok": True,
        "owner": user["login"],
        "path": req.path,
        "size": len(content),
        "shares_registered": n_shares,
    }


@app.post("/v1/knowledge")
async def upload_knowledge(
    req: KnowledgeUploadRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """Upsert the caller's durable knowledge doc (distilled decisions). Persists
    org-wide and survives the caller going offline — powers /ask-all."""
    await require_workspace_member(user, req.org)
    await storage.upsert_knowledge(req.org, user["login"], req.content)
    return {"ok": True, "engineer": user["login"], "size": len(req.content)}


@app.get("/v1/knowledge", response_model=KnowledgeResponse)
async def get_knowledge(
    org: str,
    user: dict = Depends(github_user_from_bearer),
) -> KnowledgeResponse:
    """All engineers' knowledge docs in the caller's org (offline-readable).
    Privacy is org-isolation only for now — any org member can read the org's
    knowledge. Finer-grained access is a later refinement."""
    await require_workspace_member(user, org)
    docs = await storage.get_org_knowledge(org)
    return KnowledgeResponse(docs=[KnowledgeDoc(**d) for d in docs])


@app.post("/v1/query")
async def create_query(
    req: QueryCreateRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """Asker enqueues a live question for `target`. The target's daemon answers
    it locally (raw transcript never leaves their machine) and posts back.

    Requires an ACCEPTED connection between asker and target — the connection
    is the consent gate. No connection, no query."""
    await require_workspace_member(user, req.org)
    if req.target == user["login"]:
        raise HTTPException(status_code=400, detail="can't query yourself")
    if not await storage.is_connected(req.org, user["login"], req.target):
        raise HTTPException(
            status_code=403,
            detail=(f"Not connected to {req.target}. Run /connect {req.target} first "
                    f"(and they must /connect you back) before you can /ask them."),
        )
    kind = req.kind if req.kind in ("answer", "list") else "answer"
    qid = await storage.create_query(req.org, user["login"], req.target, req.question,
                                     kind=kind, session_id=req.session_id)
    return {"ok": True, "query_id": qid}


@app.get("/v1/query/pending")
async def pending_queries(
    org: str,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """The target's daemon polls this for questions addressed to it."""
    await require_workspace_member(user, org)
    return {"queries": await storage.pending_queries_for(org, user["login"])}


@app.post("/v1/query/{qid}/answer")
async def answer_query(
    qid: str,
    req: QueryAnswerRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """The target posts its locally-generated answer. Only the addressed
    target may answer (enforced by matching target_handle == caller)."""
    await require_workspace_member(user, req.org)
    ok = await storage.answer_query(req.org, qid, user["login"], req.answer, req.citation)
    if not ok:
        raise HTTPException(status_code=404, detail="no pending query for you with that id")
    return {"ok": True}


@app.get("/v1/query/{qid}")
async def get_query(
    qid: str,
    org: str,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """Asker polls for the answer. Only the asker or target may read it."""
    await require_workspace_member(user, org)
    q = await storage.get_query(org, qid, user["login"])
    if q is None:
        raise HTTPException(status_code=404, detail="query not found or not yours")
    return q


@app.post("/v1/files/append")
async def append_file(
    req: FileAppendRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """
    Conditional append. Caller declares `expected_size`; if it matches the
    server's current file size, we append the delta and return the new
    size. Otherwise we return ok=False and the client must full-re-upload
    via POST /v1/files.

    This is the delta-upload primitive for jsonl files which are
    strictly append-only — saves ~95% of upload bandwidth vs re-sending
    the entire file every time Claude writes a turn.
    """
    await require_workspace_member(user, req.org)
    import base64
    try:
        delta = base64.b64decode(req.content_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"content_b64 not valid base64: {e}")

    new_size, was_appended = await storage.append_file(
        req.org, user["login"], req.path, delta, req.expected_size
    )
    n_shares = 0
    if was_appended and req.session_id and req.recipients:
        n_shares = await storage.share_session(
            req.org, user["login"], req.session_id, req.recipients
        )
    return {
        "ok": was_appended,
        "current_size": new_size,
        "shares_registered": n_shares,
    }


@app.delete("/v1/files")
async def delete_one_file(
    org: str,
    path: str,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    await require_workspace_member(user, org)
    n = await storage.delete_file(org, user["login"], path)
    return {"ok": True, "deleted": n}


@app.delete("/v1/files/purge")
async def purge_my_files(
    org: str,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
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
    await require_workspace_member(user, org)
    await storage.put_sync_state(org, user["login"])
    return {"ok": True}


# ─── Connections ───────────────────────────────────────────────────────────

@app.get("/v1/connections")
async def get_connections(
    org: str,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    await require_workspace_member(user, org)
    return {"org": org, **await storage.list_connections(org, user["login"])}


@app.post("/v1/connections/request")
async def post_connection_request(
    req: ConnectionRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    await require_workspace_member(user, req.org)
    try:
        result = await storage.connection_request(req.org, user["login"], req.peer)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **result}


@app.post("/v1/connections/accept")
async def post_connection_accept(
    req: ConnectionRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    await require_workspace_member(user, req.org)
    updated = await storage.connection_decide(req.org, user["login"], req.peer, accept=True)
    if not updated:
        raise HTTPException(status_code=404, detail=f"No pending request from {req.peer}")
    return {"ok": True, "status": "accepted"}


@app.post("/v1/connections/decline")
async def post_connection_decline(
    req: ConnectionRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    await require_workspace_member(user, req.org)
    updated = await storage.connection_decide(req.org, user["login"], req.peer, accept=False)
    if not updated:
        raise HTTPException(status_code=404, detail=f"No pending request from {req.peer}")
    return {"ok": True, "status": "declined"}


@app.post("/v1/connections/disconnect")
async def post_connection_disconnect(
    req: ConnectionRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    await require_workspace_member(user, req.org)
    n = await storage.connection_disconnect(req.org, user["login"], req.peer)
    return {"ok": True, "removed_rows": n}


# ─── Session shares (per-session ACL) ──────────────────────────────────────

@app.post("/v1/sessions/share")
async def post_session_share(
    req: SessionShareRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    await require_workspace_member(user, req.org)
    n = await storage.share_session(req.org, user["login"], req.session_id, req.recipients)
    return {"ok": True, "registered": n}


@app.post("/v1/sessions/unshare")
async def post_session_unshare(
    req: SessionUnshareRequest,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    await require_workspace_member(user, req.org)
    n = await storage.unshare_session(req.org, user["login"], req.session_id, req.recipient)
    return {"ok": True, "removed": n}


# ─── Dump (raw view, no synthesis) ─────────────────────────────────────────

@app.get("/v1/dump")
async def dump_session(
    org: str,
    teammate: str,
    session_id: str | None = None,
    user: dict = Depends(github_user_from_bearer),
):
    """
    Return a raw concatenation of teammate's session file (or any visible file
    if session_id == 'CLAUDE.md' etc). Used by the /show slash command —
    purely for human/debug inspection, no AI processing.

    If session_id is omitted, returns a list of visible session IDs instead.
    """
    await require_workspace_member(user, org)
    if session_id is None:
        files = await storage.list_visible_paths(org, user["login"], teammate)
        return {
            "org": org,
            "teammate": teammate,
            "visible_files": files,
        }
    path = f"{session_id}.jsonl" if not session_id.endswith(".jsonl") else session_id
    if not await storage.can_read_file(org, user["login"], teammate, path):
        raise HTTPException(status_code=403, detail=f"Not allowed to read {teammate}/{path}")
    content = await storage.get_file(org, teammate, path)
    if content is None:
        raise HTTPException(status_code=404, detail=f"File not found: {teammate}/{path}")
    return Response(content=content, media_type="application/octet-stream")


# ─── Dashboard ─────────────────────────────────────────────────────────────

@app.get("/v1/dashboard")
async def dashboard(
    org: str,
    user: dict = Depends(github_user_from_bearer),
) -> dict:
    """
    Single-page aggregated state for the dashboard UI. Returns everything
    the dashboard needs in one round-trip so polling stays cheap.
    """
    await require_workspace_member(user, org)
    return await storage.dashboard_snapshot(org, user["login"])
