"""
SQLite-backed storage for teammate-sync cloud backend.

Schema:
  files(workspace_org, owner_handle, path, content, updated_at)
    — files uploaded by each engineer in each workspace.
    — content is BLOB; can hold any bytes (CLAUDE.md text, session jsonls, etc.)

  sync_state(workspace_org, owner_handle, last_sync_epoch, updated_at)
    — per-engineer freshness timestamp the MCP surfaces to askers.

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
"""


async def init_db() -> None:
    """Create schema if it doesn't exist. Idempotent. Call on app startup."""
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


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
    Delete every file owned by this engineer in this workspace. Called when the
    engineer's last /share session is removed (privacy-first cleanup).
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
