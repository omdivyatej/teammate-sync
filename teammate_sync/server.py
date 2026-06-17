#!/usr/bin/env python3
"""
teammate-sync MCP server.

Exposes one tool — query_teammate_context(question) — that reads a
teammate's Claude Code working corpus from a configured storage backend
and returns a cited synthesis of the answer.

The storage backend (local filesystem or S3) is configured via env vars;
see backend.py.
"""
import json
import os
import time
from datetime import datetime, timezone

from anthropic import Anthropic
from mcp.server.fastmcp import FastMCP

import httpx

from .auth import read_anthropic_key, read_auth
from .backend import ACTIVE_SESSIONS_FILENAME, HTTPBackend, StorageBackend


SYNTHESIS_MODEL = os.environ.get("TEAMMATE_SYNTHESIS_MODEL", "claude-sonnet-4-6")
# Soft cap on corpus bytes passed to the synthesis call. Claude Sonnet 4.6 has
# a large context window, but we keep this conservative for cost + latency.
MAX_CORPUS_BYTES = 400_000
STALE_THRESHOLD_SECONDS = 30 * 60  # 30 minutes


mcp = FastMCP("teammate-sync")
# Read the Anthropic key from ~/.teammate-sync/auth.json (written by
# `teammate-sync init`). Failing fast here gives the user a clear remediation
# message before any MCP tool call is attempted.
anthropic_client = Anthropic(api_key=read_anthropic_key())
print("[server] backend-mediated mode — auth from ~/.teammate-sync/auth.json", flush=True)


SYNTHESIS_PROMPT_TEMPLATE = """You are reading a teammate's Claude Code working corpus to answer a coworker's question.

A coworker asks: {question}

Below is the teammate's corpus, in two parts:
  1. ACTIVE SESSIONS — live process state (cwd, last activity time) of the teammate's currently-running Claude Code processes.
  2. PERSISTENT CORPUS — their CLAUDE.md, session transcripts, and scratch notes.

Session ordering: each session transcript is labelled with one of:
  - [ACTIVE — LIVE NOW]: the session the teammate is currently typing in. Prefer this for questions about "right now", "most recent message", or "what is he saying."
  - [MOST RECENT SESSION]: the highest-timestamp session if none is currently active.
  - [older session #N]: historical, deprioritize unless explicitly asked about the past.
Each header also has a `last message <timestamp>` so you can verify chronology yourself.

CORPUS:
{corpus}

Instructions:
- Answer ONLY the asked question. Do not summarize the corpus broadly.
- Be concise — under 500 words.
- CITE the specific source for every factual claim: file name (e.g. "CLAUDE.md"), or session id with the marker (e.g. "session abc-123 [ACTIVE — LIVE NOW]").
- For "most recent" questions, the [ACTIVE — LIVE NOW] session OR the [MOST RECENT SESSION] should be your primary source. If you cite an [older session #N] for a "most recent" question, you're wrong.
- If the corpus does not contain the answer, say exactly: "Not found in shared context."
- Do not speculate beyond what is written in the corpus.
- Do not include preamble like "Based on the corpus..." — just answer.

Answer:"""


def _parse_iso_epoch(ts: str) -> float | None:
    """Parse an ISO-8601 timestamp string to an epoch float. None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def render_jsonl_session(filename: str, content: bytes) -> tuple[str, float]:
    """
    Render a Claude Code session jsonl file (as bytes) into readable text,
    and return the max message timestamp found (epoch) so the caller can
    sort sessions by recency.
    """
    rendered_lines: list[str] = []
    max_epoch = 0.0
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception as e:
        return f"[Error decoding {filename}: {e}]", 0.0

    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        ts_epoch = _parse_iso_epoch(obj.get("timestamp", ""))
        if ts_epoch and ts_epoch > max_epoch:
            max_epoch = ts_epoch

        msg_type = obj.get("type", "?")
        message = obj.get("message")
        if not isinstance(message, dict):
            if msg_type == "tool_result":
                top_content = obj.get("content", "")
                rendered_lines.append(f"[tool_result] {str(top_content)[:400]}")
            continue

        block_content = message.get("content")
        if isinstance(block_content, str):
            rendered_lines.append(f"[{msg_type}] {block_content}")
            continue

        if not isinstance(block_content, list):
            continue

        block_texts: list[str] = []
        for block in block_content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                block_texts.append(block.get("text", ""))
            elif btype == "thinking":
                continue  # noise for synthesis
            elif btype == "tool_use":
                tool_name = block.get("name", "?")
                tool_input = block.get("input", {})
                block_texts.append(
                    f"[tool_use: {tool_name}({json.dumps(tool_input)[:200]})]"
                )
            elif btype == "tool_result":
                result = block.get("content", "")
                block_texts.append(f"[tool_result] {str(result)[:300]}")

        combined = "\n".join(t for t in block_texts if t.strip())
        if combined:
            rendered_lines.append(f"[{msg_type}] {combined}")

    return "\n\n".join(rendered_lines), max_epoch


def format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} seconds ago"
    if seconds < 3600:
        return f"{int(seconds / 60)} minutes ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)} hours ago"
    return f"{int(seconds / 86400)} days ago"


def format_active_sessions(raw: bytes) -> str:
    """Render the .active-sessions.json registry as a readable section."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    sessions = data.get("sessions", [])
    if not isinstance(sessions, list) or not sessions:
        return ""
    now = time.time()
    lines: list[str] = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        sid = s.get("session_id", "?")
        cwd = s.get("cwd") or "?"
        last_epoch = s.get("last_activity_epoch")
        if isinstance(last_epoch, (int, float)):
            last_age = format_age(max(0, now - last_epoch))
        else:
            last_age = "unknown"
        lines.append(f"- session {sid}: cwd={cwd}, last active {last_age}")
    return "\n".join(lines)


def _get_active_session_id(backend: StorageBackend) -> str | None:
    """
    Read .active-sessions.json and return the session_id of the most recently
    active session, used to flag the live session in the corpus.
    """
    raw = backend.get_bytes(ACTIVE_SESSIONS_FILENAME)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    sessions = data.get("sessions", [])
    if not isinstance(sessions, list) or not sessions:
        return None
    # Pick the one with the most recent last_activity_epoch
    best = None
    best_epoch = -1.0
    for s in sessions:
        if not isinstance(s, dict):
            continue
        epoch = s.get("last_activity_epoch")
        if isinstance(epoch, (int, float)) and epoch > best_epoch:
            best_epoch = epoch
            best = s.get("session_id")
    return best


def load_corpus(backend: StorageBackend) -> str:
    """
    Read all relevant content from the backend and return a single text blob
    formatted for the synthesis prompt.
    """
    sections: list[str] = []

    keys = backend.list_keys()

    # Active sessions (live state) — first so it's most salient
    active_bytes = backend.get_bytes(ACTIVE_SESSIONS_FILENAME)
    if active_bytes:
        formatted = format_active_sessions(active_bytes)
        if formatted:
            sections.append(f"=== ACTIVE SESSIONS (live) ===\n{formatted}")

    if not keys and not sections:
        return "[Error: backend has no readable files]"

    # CLAUDE.md first if present
    if "CLAUDE.md" in keys:
        content = backend.get_bytes("CLAUDE.md")
        if content is not None:
            sections.append(f"=== CLAUDE.md ===\n{content.decode('utf-8', errors='replace')}")

    # Session jsonl files — sort by most-recent-message timestamp, newest first,
    # and explicitly mark the currently-active session so synthesis can prefer
    # it for "right now" / "most recent" questions.
    active_session_id = _get_active_session_id(backend)

    rendered_sessions: list[tuple[float, str, str]] = []  # (epoch, key, rendered)
    for key in [k for k in keys if k.endswith(".jsonl")]:
        content = backend.get_bytes(key)
        if content is None:
            continue
        rendered, last_epoch = render_jsonl_session(key, content)
        if rendered:
            rendered_sessions.append((last_epoch, key, rendered))

    rendered_sessions.sort(key=lambda t: t[0], reverse=True)

    for rank, (epoch, key, rendered) in enumerate(rendered_sessions):
        sid = key[:-len(".jsonl")] if key.endswith(".jsonl") else key
        # Session jsonls come from nested project dirs like
        # "-home-ubuntu/<uuid>"; the active-sessions registry stores raw UUIDs.
        # Compare on the last path component.
        sid_uuid = sid.split("/")[-1]
        if active_session_id and sid_uuid == active_session_id:
            label = "ACTIVE — LIVE NOW"
        elif rank == 0:
            label = "MOST RECENT SESSION"
        else:
            label = f"older session #{rank + 1}"
        if epoch > 0:
            ts_str = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
            header = f"=== Session {sid} [{label}] — last message {ts_str} ==="
        else:
            header = f"=== Session {sid} [{label}] ==="
        sections.append(f"{header}\n{rendered}")

    # Other .md files (scratch notes), skipping CLAUDE.md (already included)
    other_md = [k for k in keys if k.endswith(".md") and k != "CLAUDE.md"]
    for key in sorted(other_md):
        content = backend.get_bytes(key)
        if content is None:
            continue
        sections.append(f"=== Note: {key} ===\n{content.decode('utf-8', errors='replace')}")

    corpus = "\n\n".join(sections)
    if not corpus:
        return "[Error: backend returned no readable content]"

    corpus_bytes = corpus.encode("utf-8")
    if len(corpus_bytes) > MAX_CORPUS_BYTES:
        corpus = corpus_bytes[:MAX_CORPUS_BYTES].decode("utf-8", errors="ignore")
        corpus += "\n\n[... corpus truncated to fit size limit ...]"

    return corpus


def get_sync_freshness(backend: StorageBackend) -> dict | None:
    state = backend.get_state()
    if state is None:
        return None
    last_sync_epoch = state.get("last_sync_epoch")
    if not isinstance(last_sync_epoch, (int, float)):
        return None
    age_seconds = max(0, time.time() - last_sync_epoch)
    return {
        "age_seconds": age_seconds,
        "is_stale": age_seconds > STALE_THRESHOLD_SECONDS,
    }


@mcp.tool()
def list_teammates() -> list[str]:
    """List all teammates in your workspace whose Claude Code context is queryable.

    Workspace = the GitHub organization configured in your auth file. Returns
    the GitHub handles of all org members. They may not have shared anything
    yet — try query_teammate_context to see.

    Returns:
        Sorted list of GitHub handles. Empty list if you're the only member
        or the OAuth app hasn't been approved for your org.
    """
    try:
        auth = read_auth()
        r = httpx.get(
            f"{auth['backend_url'].rstrip('/')}/v1/teammates",
            params={"org": auth["org"]},
            headers={"Authorization": f"Bearer {auth['token']}"},
            timeout=20.0,
        )
        r.raise_for_status()
        return sorted(t["github_handle"] for t in r.json().get("teammates", []))
    except Exception as e:
        return [f"[Error: {e}]"]


@mcp.tool()
def query_teammate_context(teammate: str, question: str) -> str:
    """Query a specific teammate's Claude Code working context.

    Reads the named teammate's CLAUDE.md, session transcripts, and scratch
    notes from the shared cloud backend, then returns a synthesized, cited
    answer (~500 words) drawn ONLY from their actual corpus.

    If you don't know who's available, call list_teammates() first.

    If the answer is not in the corpus (or the teammate hasn't /share'd
    anything yet), the tool returns: "Not found in shared context."

    Args:
        teammate: The teammate's GitHub handle (e.g. "saketh"). Case-sensitive.
        question: A natural-language question. Be specific — e.g.
                  "What did they decide about cursor-based pagination?"
                  rather than "Tell me everything they did."

    Returns:
        A cited, synthesized answer string with a freshness stamp,
        prefixed with the teammate's handle.
    """
    try:
        auth = read_auth()
        backend = HTTPBackend(
            backend_url=auth["backend_url"],
            token=auth["token"],
            org=auth["org"],
            teammate=teammate,
        )
    except (FileNotFoundError, ValueError) as e:
        return f"[Error: {e}]"

    corpus = load_corpus(backend)
    if corpus.startswith("[Error"):
        return f"[{teammate}] {corpus}"

    prompt = SYNTHESIS_PROMPT_TEMPLATE.format(question=question, corpus=corpus)

    response = anthropic_client.messages.create(
        model=SYNTHESIS_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    answer = None
    for block in response.content:
        if getattr(block, "type", None) == "text":
            answer = block.text
            break

    if answer is None:
        return f"[{teammate}] [Error: synthesis returned no text]"

    freshness = get_sync_freshness(backend)
    if freshness is None:
        return f"[from {teammate}]\n\n{answer}"

    age_str = format_age(freshness["age_seconds"])
    if freshness["is_stale"]:
        prefix = (
            f"⚠️  Stale sync warning: {teammate}'s corpus was last "
            f"updated {age_str}. Information below may be outdated.\n\n"
        )
        suffix = f"\n\n— {teammate}'s context as of {age_str}"
        return prefix + answer + suffix

    return f"[from {teammate}]\n\n{answer}\n\n— {teammate}'s context as of {age_str}"


if __name__ == "__main__":
    mcp.run()
