"""
SQLite-backed storage for teammate-sync cloud backend (v0.2).

Schema:
  files(workspace_org, owner_handle, path, content, updated_at)
    — files uploaded by each engineer in each workspace.
    — content is BLOB; can hold any bytes (CLAUDE.md text, session jsonls, etc.)

  sync_state(workspace_org, owner_handle, last_sync_epoch, updated_at)
    — per-engineer freshness timestamp the MCP surfaces to askers.

  connections(workspace_org, requester_handle, recipient_handle,
              status, requested_at, decided_at)
    — persistent trust between two engineers. Created when either party
      tries to /share with the other for the first time. Bidirectional
      once status='accepted'.
    — status ∈ {'pending', 'accepted', 'declined'}.
    — disconnect = delete the row.

  session_shares(workspace_org, session_id, owner_handle, recipient_handle, shared_at)
    — per-session sharing decisions. A row exists for each (session, recipient).

DB lives at $TEAMMATE_DB_PATH (defaults to /data/teammate.db, where /data is
the Fly Volume mount point). Falls back to ./teammate.db for local dev.
"""
import os
import time
from pathlib import Path

import aiosqlite


DB_PATH = os.environ.get("TEAMMATE_DB_PATH", "/data/teammate.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    workspace_org  TEXT NOT NULL,
    owner_handle   TEXT NOT NULL,
    path           TEXT NOT NULL,
    content        BLOB NOT NULL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (workspace_org, owner_handle, path)
);

CREATE INDEX IF NOT EXISTS idx_files_owner
    ON files (workspace_org, owner_handle);

CREATE TABLE IF NOT EXISTS sync_state (
    workspace_org   TEXT NOT NULL,
    owner_handle    TEXT NOT NULL,
    last_sync_epoch REAL NOT NULL,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (workspace_org, owner_handle)
);

CREATE TABLE IF NOT EXISTS connections (
    workspace_org    TEXT NOT NULL,
    requester_handle TEXT NOT NULL,
    recipient_handle TEXT NOT NULL,
    status           TEXT NOT NULL CHECK(status IN ('pending', 'accepted', 'declined')),
    requested_at     REAL NOT NULL,
    decided_at       REAL,
    PRIMARY KEY (workspace_org, requester_handle, recipient_handle)
);

CREATE INDEX IF NOT EXISTS idx_connections_recipient
    ON connections (workspace_org, recipient_handle, status);

CREATE TABLE IF NOT EXISTS session_shares (
    workspace_org    TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    owner_handle     TEXT NOT NULL,
    recipient_handle TEXT NOT NULL,
    shared_at        REAL NOT NULL,
    PRIMARY KEY (workspace_org, session_id, recipient_handle)
);

CREATE INDEX IF NOT EXISTS idx_session_shares_owner
    ON session_shares (workspace_org, owner_handle, recipient_handle);

CREATE INDEX IF NOT EXISTS idx_session_shares_recipient
    ON session_shares (workspace_org, recipient_handle, owner_handle);

-- Durable per-engineer distilled decision log (knowledge.md). Unlike files/
-- session_shares (ephemeral, torn down on quit), this PERSISTS: it survives
-- the engineer going offline or quitting, and is readable org-wide. One row
-- per engineer per org; the distiller updates (never overwrites away) the
-- engineer's own evolving doc. Powers /ask-all (team memory).
CREATE TABLE IF NOT EXISTS knowledge (
    workspace_org  TEXT NOT NULL,
    engineer_handle TEXT NOT NULL,
    content        TEXT NOT NULL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (workspace_org, engineer_handle)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_org
    ON knowledge (workspace_org);

-- Federated live queries: store-and-forward request/response so an asker can
-- query a teammate's live Claude session WITHOUT the teammate's raw transcript
-- ever leaving their machine. Only the question + answer transit here; the
-- target's daemon answers locally and posts back just the answer.
CREATE TABLE IF NOT EXISTS queries (
    id             TEXT PRIMARY KEY,
    workspace_org  TEXT NOT NULL,
    asker_handle   TEXT NOT NULL,
    target_handle  TEXT NOT NULL,
    question       TEXT NOT NULL,
    status         TEXT NOT NULL CHECK(status IN ('pending','answered','failed')),
    answer         TEXT,
    citation       TEXT,
    created_at     REAL NOT NULL,
    answered_at    REAL,
    kind           TEXT NOT NULL DEFAULT 'answer',
    session_id     TEXT
);

CREATE INDEX IF NOT EXISTS idx_queries_target
    ON queries (workspace_org, target_handle, status);
"""


async def init_db() -> None:
    """Create schema if it doesn't exist. Idempotent. Call on app startup."""
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        # Migrations for DBs created before kind/session_id existed.
        for ddl in ("kind TEXT NOT NULL DEFAULT 'answer'", "session_id TEXT"):
            try:
                await db.execute(f"ALTER TABLE queries ADD COLUMN {ddl}")
            except Exception:
                pass  # column already present
        await db.commit()


# ─── Knowledge (durable, org-wide, offline-readable) ───────────────────────

async def upsert_knowledge(workspace_org: str, engineer_handle: str, content: str) -> None:
    """Replace this engineer's knowledge doc with the latest distilled version.
    One row per engineer; the distiller evolves the doc client-side (append +
    supersede), so this just stores the newest full content."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO knowledge (workspace_org, engineer_handle, content, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(workspace_org, engineer_handle)
            DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at
            """,
            (workspace_org, engineer_handle, content, now),
        )
        await db.commit()


async def get_org_knowledge(workspace_org: str) -> list[dict]:
    """All engineers' knowledge docs in an org, newest-updated first. Powers
    /ask-all — readable org-wide, works whether or not the owner is online."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT engineer_handle, content, updated_at
            FROM knowledge WHERE workspace_org=?
            ORDER BY updated_at DESC
            """,
            (workspace_org,),
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"engineer_handle": r[0], "content": r[1], "updated_at": r[2]}
        for r in rows
    ]


# ─── Federated queries (live ask, store-and-forward) ───────────────────────

async def create_query(workspace_org: str, asker: str, target: str, question: str,
                       kind: str = "answer", session_id: str | None = None) -> str:
    """Enqueue a request for `target`. kind='answer' (default) is a question to
    answer from a session; kind='list' asks the target's daemon to return the
    set of sessions it shares with `asker`. Returns the query id."""
    import secrets
    qid = secrets.token_hex(12)
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO queries
               (id, workspace_org, asker_handle, target_handle, question, status,
                created_at, kind, session_id)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (qid, workspace_org, asker, target, question, now, kind, session_id),
        )
        await db.commit()
    return qid


async def pending_queries_for(workspace_org: str, target: str) -> list[dict]:
    """Pending queries addressed to `target` (their daemon polls this)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, asker_handle, question, created_at, kind, session_id
               FROM queries
               WHERE workspace_org=? AND target_handle=? AND status='pending'
               ORDER BY created_at ASC""",
            (workspace_org, target),
        ) as cur:
            rows = await cur.fetchall()
    return [{"id": r[0], "asker": r[1], "question": r[2], "created_at": r[3],
             "kind": r[4], "session_id": r[5]} for r in rows]


async def answer_query(workspace_org: str, qid: str, answerer: str,
                       answer: str, citation: str | None) -> bool:
    """Post an answer. Only the query's target may answer. Returns True if
    a pending query was found and updated for this answerer."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE queries SET status='answered', answer=?, citation=?, answered_at=?
               WHERE id=? AND workspace_org=? AND target_handle=? AND status='pending'""",
            (answer, citation, now, qid, workspace_org, answerer),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_query(workspace_org: str, qid: str, requester: str) -> dict | None:
    """Fetch a query's status/answer. Only the asker or the target may read it."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT asker_handle, target_handle, question, status, answer, citation,
                      created_at, answered_at
               FROM queries WHERE id=? AND workspace_org=?""",
            (qid, workspace_org),
        ) as cur:
            r = await cur.fetchone()
    if r is None:
        return None
    if requester not in (r[0], r[1]):
        return None  # not a party to this query
    return {
        "id": qid, "asker": r[0], "target": r[1], "question": r[2],
        "status": r[3], "answer": r[4], "citation": r[5],
        "created_at": r[6], "answered_at": r[7],
    }


# ─── Files ─────────────────────────────────────────────────────────────────

async def put_file(workspace_org: str, owner_handle: str, path: str, content: bytes) -> None:
    """Upsert a file. Overwrites existing content at the same path."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO files (workspace_org, owner_handle, path, content, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(workspace_org, owner_handle, path)
            DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at
            """,
            (workspace_org, owner_handle, path, content, now),
        )
        await db.commit()


async def append_file(
    workspace_org: str,
    owner_handle: str,
    path: str,
    delta_bytes: bytes,
    expected_size: int,
) -> tuple[int, bool]:
    """
    Append `delta_bytes` to an existing file at `path`, but ONLY if the
    file's current size matches `expected_size`. This is the conditional-
    append primitive used by the daemon's delta-upload path.

    Returns (current_size_after_op, was_appended).
      - was_appended=True  → server now has existing + delta
      - was_appended=False → size mismatch (or file missing); caller must
        full-re-upload via put_file. `current_size_after_op` is the actual
        size the server saw, useful for the client to resync.

    Idempotent-ish: an append of the same delta with the same expected_size
    is safe to retry only until size moves forward.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT content FROM files WHERE workspace_org=? AND owner_handle=? AND path=?",
            (workspace_org, owner_handle, path),
        ) as cur:
            row = await cur.fetchone()
        existing = row[0] if row else b""
        if len(existing) != expected_size:
            return len(existing), False
        new_content = existing + delta_bytes
        now = time.time()
        await db.execute(
            """
            INSERT INTO files (workspace_org, owner_handle, path, content, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(workspace_org, owner_handle, path)
            DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at
            """,
            (workspace_org, owner_handle, path, new_content, now),
        )
        await db.commit()
        return len(new_content), True


async def get_file_size(workspace_org: str, owner_handle: str, path: str) -> int:
    """Return current byte length of a file. 0 if missing."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT LENGTH(content) FROM files WHERE workspace_org=? AND owner_handle=? AND path=?",
            (workspace_org, owner_handle, path),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_file(workspace_org: str, owner_handle: str, path: str) -> bytes | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT content FROM files WHERE workspace_org=? AND owner_handle=? AND path=?",
            (workspace_org, owner_handle, path),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def list_paths(workspace_org: str, owner_handle: str) -> list[dict]:
    """Return [{path, size, updated_at}] for all files owned by this handle in this workspace."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT path, LENGTH(content) AS size, updated_at
            FROM files
            WHERE workspace_org=? AND owner_handle=?
            ORDER BY path
            """,
            (workspace_org, owner_handle),
        ) as cur:
            return [
                {"path": r[0], "size": r[1], "updated_at": r[2]}
                async for r in cur
            ]


async def delete_file(workspace_org: str, owner_handle: str, path: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM files WHERE workspace_org=? AND owner_handle=? AND path=?",
            (workspace_org, owner_handle, path),
        )
        await db.commit()
        return cur.rowcount


async def purge_owner(workspace_org: str, owner_handle: str) -> int:
    """
    Delete every file owned by this engineer in this workspace, plus all
    session_shares rows they own. Connections survive (the trust relationship
    persists even when current content is wiped).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM files WHERE workspace_org=? AND owner_handle=?",
            (workspace_org, owner_handle),
        )
        await db.execute(
            "DELETE FROM sync_state WHERE workspace_org=? AND owner_handle=?",
            (workspace_org, owner_handle),
        )
        await db.execute(
            "DELETE FROM session_shares WHERE workspace_org=? AND owner_handle=?",
            (workspace_org, owner_handle),
        )
        await db.commit()
        return cur.rowcount


async def put_sync_state(workspace_org: str, owner_handle: str) -> None:
    """Mark this engineer's corpus as just-synced (now)."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO sync_state (workspace_org, owner_handle, last_sync_epoch, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(workspace_org, owner_handle)
            DO UPDATE SET last_sync_epoch=excluded.last_sync_epoch, updated_at=excluded.updated_at
            """,
            (workspace_org, owner_handle, now, now),
        )
        await db.commit()


async def get_sync_state(workspace_org: str, owner_handle: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT last_sync_epoch, updated_at
            FROM sync_state
            WHERE workspace_org=? AND owner_handle=?
            """,
            (workspace_org, owner_handle),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {"last_sync_epoch": row[0], "updated_at": row[1]}


# ─── Connections (persistent trust) ────────────────────────────────────────

async def connection_request(
    workspace_org: str, requester: str, recipient: str
) -> dict:
    """
    Insert or refresh a connection request from `requester` to `recipient`.

    Returns the resulting row dict. Behavior:
      - if no row exists yet: insert with status='pending'
      - if row exists with status='pending' (same direction): leave as-is (idempotent)
      - if row exists with status='accepted' (either direction): leave as-is (already connected)
      - if row exists with status='declined': flip back to 'pending' (re-asking)

    We also check the reverse direction — if recipient already requested
    requester, we auto-accept (mutual desire to connect).
    """
    if requester == recipient:
        raise ValueError("Cannot connect to yourself.")
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        # Reverse direction: did recipient already request requester?
        async with db.execute(
            "SELECT status FROM connections WHERE workspace_org=? AND requester_handle=? AND recipient_handle=?",
            (workspace_org, recipient, requester),
        ) as cur:
            reverse = await cur.fetchone()
        if reverse and reverse[0] == "pending":
            # Mutual interest — accept it.
            await db.execute(
                "UPDATE connections SET status='accepted', decided_at=? WHERE workspace_org=? AND requester_handle=? AND recipient_handle=?",
                (now, workspace_org, recipient, requester),
            )
            await db.commit()
            return {"status": "accepted", "auto_accepted": True}
        if reverse and reverse[0] == "accepted":
            return {"status": "accepted", "auto_accepted": False}

        # Forward direction: insert or refresh.
        async with db.execute(
            "SELECT status FROM connections WHERE workspace_org=? AND requester_handle=? AND recipient_handle=?",
            (workspace_org, requester, recipient),
        ) as cur:
            forward = await cur.fetchone()
        if forward and forward[0] == "accepted":
            return {"status": "accepted", "auto_accepted": False}
        if forward and forward[0] == "pending":
            return {"status": "pending", "auto_accepted": False}

        # Insert (or flip declined→pending)
        await db.execute(
            """
            INSERT INTO connections (workspace_org, requester_handle, recipient_handle, status, requested_at)
            VALUES (?, ?, ?, 'pending', ?)
            ON CONFLICT(workspace_org, requester_handle, recipient_handle)
            DO UPDATE SET status='pending', requested_at=excluded.requested_at, decided_at=NULL
            """,
            (workspace_org, requester, recipient, now),
        )
        await db.commit()
        return {"status": "pending", "auto_accepted": False}


async def connection_decide(
    workspace_org: str, recipient: str, requester: str, accept: bool
) -> bool:
    """
    Recipient accepts or declines a pending request from requester.
    Returns True if a row was updated; False if no pending request existed.
    """
    now = time.time()
    new_status = "accepted" if accept else "declined"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE connections
               SET status=?, decided_at=?
             WHERE workspace_org=? AND requester_handle=? AND recipient_handle=? AND status='pending'
            """,
            (new_status, now, workspace_org, requester, recipient),
        )
        await db.commit()
        return cur.rowcount > 0


async def connection_disconnect(
    workspace_org: str, actor: str, peer: str
) -> int:
    """
    Either party can disconnect. Removes all connection rows between them
    (both directions) AND all session_shares rows between them.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            DELETE FROM connections
            WHERE workspace_org=?
              AND ((requester_handle=? AND recipient_handle=?)
                OR (requester_handle=? AND recipient_handle=?))
            """,
            (workspace_org, actor, peer, peer, actor),
        )
        await db.execute(
            """
            DELETE FROM session_shares
            WHERE workspace_org=?
              AND ((owner_handle=? AND recipient_handle=?)
                OR (owner_handle=? AND recipient_handle=?))
            """,
            (workspace_org, actor, peer, peer, actor),
        )
        await db.commit()
        return cur.rowcount


async def is_connected(workspace_org: str, a: str, b: str) -> bool:
    """True iff there's an accepted connection between a and b (either direction)."""
    if a == b:
        return True  # talking to yourself is always allowed
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT 1 FROM connections
            WHERE workspace_org=?
              AND status='accepted'
              AND ((requester_handle=? AND recipient_handle=?)
                OR (requester_handle=? AND recipient_handle=?))
            LIMIT 1
            """,
            (workspace_org, a, b, b, a),
        ) as cur:
            return await cur.fetchone() is not None


async def list_connections(workspace_org: str, me: str) -> dict:
    """
    Return {accepted: [...], pending_incoming: [...], pending_outgoing: [...]}.
    Each entry is {peer_handle, requested_at, decided_at}.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # accepted (either direction)
        async with db.execute(
            """
            SELECT
                CASE WHEN requester_handle=? THEN recipient_handle ELSE requester_handle END AS peer,
                requested_at, decided_at, requester_handle=? AS i_initiated
            FROM connections
            WHERE workspace_org=? AND status='accepted'
              AND (requester_handle=? OR recipient_handle=?)
            ORDER BY decided_at DESC
            """,
            (me, me, workspace_org, me, me),
        ) as cur:
            accepted = [
                {"peer_handle": r[0], "requested_at": r[1], "decided_at": r[2], "i_initiated": bool(r[3])}
                async for r in cur
            ]
        # pending incoming (others asked me)
        async with db.execute(
            """
            SELECT requester_handle, requested_at FROM connections
            WHERE workspace_org=? AND recipient_handle=? AND status='pending'
            ORDER BY requested_at DESC
            """,
            (workspace_org, me),
        ) as cur:
            pending_incoming = [
                {"peer_handle": r[0], "requested_at": r[1]} async for r in cur
            ]
        # pending outgoing (I asked others)
        async with db.execute(
            """
            SELECT recipient_handle, requested_at FROM connections
            WHERE workspace_org=? AND requester_handle=? AND status='pending'
            ORDER BY requested_at DESC
            """,
            (workspace_org, me),
        ) as cur:
            pending_outgoing = [
                {"peer_handle": r[0], "requested_at": r[1]} async for r in cur
            ]
    return {
        "accepted": accepted,
        "pending_incoming": pending_incoming,
        "pending_outgoing": pending_outgoing,
    }


# ─── Session shares ────────────────────────────────────────────────────────

async def share_session(
    workspace_org: str, owner: str, session_id: str, recipients: list[str]
) -> int:
    """
    Mark a session as shared with the given recipients. Idempotent — re-sharing
    refreshes shared_at but doesn't duplicate. Returns the number of rows
    inserted/refreshed.
    """
    if not recipients:
        return 0
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        for r in recipients:
            await db.execute(
                """
                INSERT INTO session_shares
                    (workspace_org, session_id, owner_handle, recipient_handle, shared_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workspace_org, session_id, recipient_handle)
                DO UPDATE SET shared_at=excluded.shared_at, owner_handle=excluded.owner_handle
                """,
                (workspace_org, session_id, owner, r, now),
            )
        await db.commit()
    return len(recipients)


async def unshare_session(
    workspace_org: str, owner: str, session_id: str, recipient: str | None = None
) -> int:
    """
    Revoke a session share. If recipient is None, revoke from all recipients.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if recipient is None:
            cur = await db.execute(
                "DELETE FROM session_shares WHERE workspace_org=? AND owner_handle=? AND session_id=?",
                (workspace_org, owner, session_id),
            )
        else:
            cur = await db.execute(
                """
                DELETE FROM session_shares
                WHERE workspace_org=? AND owner_handle=? AND session_id=? AND recipient_handle=?
                """,
                (workspace_org, owner, session_id, recipient),
            )
        await db.commit()
        return cur.rowcount


async def list_sessions_shared_by(workspace_org: str, owner: str) -> list[dict]:
    """List sessions this user has shared. Returns [{session_id, recipients, shared_at}]."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT session_id, recipient_handle, shared_at
            FROM session_shares
            WHERE workspace_org=? AND owner_handle=?
            ORDER BY session_id, shared_at DESC
            """,
            (workspace_org, owner),
        ) as cur:
            rows = [r async for r in cur]
    # Group by session_id
    by_sid: dict[str, dict] = {}
    for sid, recip, shared_at in rows:
        if sid not in by_sid:
            by_sid[sid] = {"session_id": sid, "recipients": [], "shared_at": shared_at}
        by_sid[sid]["recipients"].append(recip)
        by_sid[sid]["shared_at"] = max(by_sid[sid]["shared_at"], shared_at)
    return list(by_sid.values())


async def list_sessions_shared_with(workspace_org: str, recipient: str) -> list[dict]:
    """List sessions others have shared with me. Returns [{session_id, owner_handle, shared_at}]."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT session_id, owner_handle, shared_at
            FROM session_shares
            WHERE workspace_org=? AND recipient_handle=?
            ORDER BY shared_at DESC
            """,
            (workspace_org, recipient),
        ) as cur:
            return [
                {"session_id": r[0], "owner_handle": r[1], "shared_at": r[2]}
                async for r in cur
            ]


# ─── ACL ───────────────────────────────────────────────────────────────────

def _session_id_from_path(path: str) -> str | None:
    """
    Extract a session_id from a file path. Returns None if the path isn't a
    session jsonl. Convention: filename like '<uuid>.jsonl' under any prefix.
    """
    if not path.endswith(".jsonl"):
        return None
    basename = path.rsplit("/", 1)[-1]
    sid = basename[:-len(".jsonl")]
    return sid or None


async def can_read_file(
    workspace_org: str, requester: str, owner: str, path: str
) -> bool:
    """
    Per-file ACL check.

    Rules:
      - Owner can always read their own files.
      - Otherwise: there must be an accepted connection between requester
        and owner. AND:
        - if the path is a session jsonl: there must be a session_shares row
          for that specific session_id → requester.
        - if the path is anything else (CLAUDE.md, scratch notes, .active-sessions.json):
          there must be ANY session_shares row from owner → requester.
    """
    if requester == owner:
        return True
    if not await is_connected(workspace_org, requester, owner):
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        sid = _session_id_from_path(path)
        if sid:
            async with db.execute(
                """
                SELECT 1 FROM session_shares
                WHERE workspace_org=? AND owner_handle=? AND recipient_handle=? AND session_id=?
                LIMIT 1
                """,
                (workspace_org, owner, requester, sid),
            ) as cur:
                return await cur.fetchone() is not None
        # Non-session file: any share row from owner→requester unlocks it.
        async with db.execute(
            """
            SELECT 1 FROM session_shares
            WHERE workspace_org=? AND owner_handle=? AND recipient_handle=?
            LIMIT 1
            """,
            (workspace_org, owner, requester),
        ) as cur:
            return await cur.fetchone() is not None


async def list_visible_paths(
    workspace_org: str, requester: str, owner: str
) -> list[dict]:
    """List paths in owner's corpus that requester is allowed to see."""
    if requester == owner:
        return await list_paths(workspace_org, owner)
    if not await is_connected(workspace_org, requester, owner):
        return []
    # Get the set of session_ids owner has shared with requester.
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT session_id FROM session_shares
            WHERE workspace_org=? AND owner_handle=? AND recipient_handle=?
            """,
            (workspace_org, owner, requester),
        ) as cur:
            shared_sids = {r[0] async for r in cur}
    if not shared_sids:
        return []
    all_paths = await list_paths(workspace_org, owner)
    visible = []
    for p in all_paths:
        sid = _session_id_from_path(p["path"])
        if sid is None:
            # non-session file: visible because at least one share exists
            visible.append(p)
        elif sid in shared_sids:
            visible.append(p)
    return visible


# ─── Dashboard aggregation ─────────────────────────────────────────────────

async def dashboard_snapshot(workspace_org: str, me: str) -> dict:
    """
    Single-query view of state for the dashboard:
      {
        me: <handle>,
        my_sessions: [{session_id, recipients[], shared_at}],
        my_files: [{path, size, updated_at}],
        teammates: [
          {
            handle, sessions: [{session_id, shared_at}], visible_files: [...]
          }
        ],
        connections: {accepted, pending_incoming, pending_outgoing}
      }
    """
    my_sessions = await list_sessions_shared_by(workspace_org, me)
    my_files = await list_paths(workspace_org, me)
    connections = await list_connections(workspace_org, me)
    shared_to_me = await list_sessions_shared_with(workspace_org, me)
    # Group sessions shared to me by owner.
    by_owner: dict[str, list[dict]] = {}
    for s in shared_to_me:
        by_owner.setdefault(s["owner_handle"], []).append(s)
    teammates = []
    for owner, sessions in by_owner.items():
        files = await list_visible_paths(workspace_org, me, owner)
        teammates.append({
            "handle": owner,
            "sessions": sessions,
            "visible_files": files,
        })
    return {
        "me": me,
        "my_sessions": my_sessions,
        "my_files": my_files,
        "teammates": teammates,
        "connections": connections,
    }
