#!/usr/bin/env python3
"""
teammate-sync MCP server (v0.4).

Exposes ONE tool — get_teammate_context — which returns the raw assembled
session corpus for a connected teammate. No AI synthesis. No Anthropic API
key required.

The host Claude (the one that called the tool, running in the user's
Claude Code TUI) does the reasoning over the returned corpus. This is the
right shape for MCP: tools provide context, the calling LLM reasons.

Why we dropped synthesis (was in v0.3.x):
  - Lossless: no paraphrasing layer between asker and data
  - No Anthropic key required — the single biggest onboarding pain we had
  - Cheaper + faster (no extra Claude API round-trip)
  - More expressive: host Claude can summarize, grep, compare across turns

The storage backend is configured via env vars; see backend.py.
"""
import json
import os
import time
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

import httpx

from .auth import read_auth
from .backend import ACTIVE_SESSIONS_FILENAME, HTTPBackend, StorageBackend


# Soft cap on assembled corpus bytes returned to the host Claude. Host
# Claude has plenty of context (1M tokens on Claude 4.x), but we cap to
# avoid surprising blowups on extremely long teammate sessions.
MAX_CORPUS_BYTES = 400_000
STALE_THRESHOLD_SECONDS = 30 * 60  # 30 minutes


mcp = FastMCP("teammate-sync")
print("[server] v0.4 context-fetcher mode — auth from ~/.teammate-sync/auth.json", flush=True)


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
    Render a Claude Code session jsonl into a clean conversation transcript.

    Filters out Claude Code framework noise so the host Claude (reading
    this via /ask) sees the actual conversation, not plumbing:

      - Slash command invocations (the <command-message> / <command-name>
        XML-ish wire format Claude Code uses internally)
      - The boilerplate text our own slash-command .md files inject
        ("Execute this command via the Bash tool...")
      - tool_use blocks that just invoke the teammate-sync CLI
      - tool_result blocks containing teammate-sync CLI output
      - assistant "thinking" blocks (model's internal scratch reasoning)

    Speakers labeled "Engineer:" / "Claude:" instead of [user]/[assistant].
    Tool calls rendered as parenthetical actions:
        (ran shell: <cmd>)
        (read: <path>)
        (edited: <path>)
        etc.
    """
    import re as _re

    rendered_blocks: list[str] = []
    max_epoch = 0.0

    try:
        text = content.decode("utf-8", errors="replace")
    except Exception as e:
        return f"[Error decoding {filename}: {e}]", 0.0

    # Markers that indicate a tool_result body is teammate-sync CLI/MCP output
    # rather than meaningful conversation. Used to drop bookkeeping turns.
    _TEAMMATE_OUTPUT_MARKERS = (
        "now shared with",
        "removed from shareable",
        "All shared sessions removed",
        "No active connections",
        "Currently shared sessions",
        "Workspace '",
        "Disconnected from",
        "Total shared sessions",
        "Skipped. The MCP server",
        "Can't share",
        "Nothing was shared",
        "sync engine is not running",
        "No context visible from",
        "Not found in shared context",
        "teammate-sync context — teammate:",
        "Alias set:",
        "tool_reference",
    )
    _SLASH_PREAMBLE = "Execute this command via the Bash tool"
    # A user turn that is the /ask prompt expansion Claude Code injects.
    _ASK_EXPANSION_MARKERS = ("query teammate-sync", "get_teammate_context")
    # Text that is teammate-sync's own output echoed back (the recursion source
    # + /connect//alias echoes) — plumbing, not the engineer's conversation.
    _TS_OUTPUT_MARKERS = (
        "now shared with", "connection request sent",
        "Shared for the lifetime of this Claude Code session",
        "No context visible from", "Not found in shared context",
        "teammate-sync context — teammate:", "Alias set:",
    )

    def _is_teammate_sync_tool_use(blocks: list) -> bool:
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name = str(b.get("name", ""))
                if name.startswith("mcp__teammate-sync__"):
                    return True  # nested MCP query
                inp = b.get("input", {}) or {}
                # Bash `teammate-sync …`, or a ToolSearch selecting the MCP tool.
                blob = f"{inp.get('command', '')} {inp.get('query', '')}"
                if "teammate-sync" in blob:
                    return True
        return False

    def _is_teammate_sync_tool_result(blocks: list) -> bool:
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                body = str(b.get("content", ""))
                if any(m in body for m in _TEAMMATE_OUTPUT_MARKERS):
                    return True
        return False

    def _content_text(block_content) -> str:
        """Get the text portion of a message's content, joined as one string."""
        if isinstance(block_content, str):
            return block_content
        if isinstance(block_content, list):
            return " ".join(
                b.get("text", "")
                for b in block_content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return ""

    skip_mode = False  # True after we see a slash command; eat the boilerplate
    ask_pending = False  # True after an /ask note; eat its (host-Claude) answer

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
            continue

        block_content = message.get("content")
        content_text = _content_text(block_content)

        # ── Filter 1: slash-command invocation (Claude Code's <command-*> tags) ──
        if "<command-name>" in content_text or "<command-message>" in content_text:
            # Render once as a brief annotation, then skip the next few
            # bookkeeping turns until real conversation resumes.
            name_match = _re.search(r"<command-name>([^<]+)</command-name>", content_text)
            args_match = _re.search(r"<command-args>([^<]*)</command-args>", content_text)
            cmd_name = name_match.group(1).strip() if name_match else "/?"
            cmd_args = args_match.group(1).strip() if args_match else ""
            invocation = f"{cmd_name} {cmd_args}".strip()
            first = invocation.split()[0] if invocation else ""
            if first == "/ask":
                # Keep a one-line note, drop the instruction expansion + answer.
                recipient = (cmd_args.split() or ["a teammate"])[0]
                rendered_blocks.append(f"[Engineer asked teammate {recipient} a question]")
                ask_pending = True
            elif first not in ("/connect", "/disconnect", "/shared"):
                # /connect//disconnect//shared are pure plumbing — no note.
                rendered_blocks.append(f"[Engineer ran: {invocation}]")
            skip_mode = True
            continue

        # ── Filter 2: while in skip_mode, swallow the slash-command's noise ──
        if skip_mode:
            if _SLASH_PREAMBLE in content_text:
                continue
            if isinstance(block_content, list):
                if _is_teammate_sync_tool_use(block_content):
                    continue
                if _is_teammate_sync_tool_result(block_content):
                    continue
            # First non-slash-related turn: we're back in conversation.
            skip_mode = False

        # ── Filter 3: drop the /ask prompt expansion + teammate-sync echoes ──
        # (the recursion source: a /ask inside a shared session otherwise bakes
        # the whole fetched corpus into the transcript and re-shares it).
        if any(m in content_text for m in _ASK_EXPANSION_MARKERS):
            continue
        if content_text and any(m in content_text for m in _TS_OUTPUT_MARKERS):
            continue

        # Drop ANY turn that invokes teammate-sync or carries its output — the
        # /connect//ask execution (Claude running the CLI + its result), nested
        # MCP calls, ToolSearch selecting the MCP tool, the "Can't share" guard
        # message, etc. Applies regardless of skip_mode (the /connect execution
        # turns arrive after skip_mode has already ended).
        if isinstance(block_content, list) and (
            _is_teammate_sync_tool_use(block_content)
            or _is_teammate_sync_tool_result(block_content)
        ):
            continue

        # Skip the host-Claude answer to an /ask (one assistant text turn after
        # the note). Intervening plumbing turns carry no text, so they don't
        # clear the flag; the next real engineer turn does.
        if ask_pending:
            if msg_type == "assistant":
                has_text = (isinstance(block_content, str) and block_content.strip()) or (
                    isinstance(block_content, list) and any(
                        isinstance(b, dict) and b.get("type") == "text"
                        and b.get("text", "").strip()
                        for b in block_content))
                if has_text:
                    ask_pending = False
                    continue
            elif msg_type == "user" and content_text.strip():
                ask_pending = False

        # ── Normal rendering: clean speaker label + body ──
        speaker = "Engineer" if msg_type == "user" else "Claude"

        if isinstance(block_content, str):
            rendered_blocks.append(f"{speaker}: {block_content.strip()}")
            continue

        if not isinstance(block_content, list):
            continue

        text_parts: list[str] = []
        action_parts: list[str] = []

        for block in block_content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text", "")
                if t.strip():
                    text_parts.append(t)
            elif btype == "thinking":
                continue
            elif btype == "tool_use":
                tname = block.get("name", "?")
                if tname.startswith("mcp__teammate-sync__"):
                    continue  # nested teammate-sync query — plumbing, skip
                tinp = block.get("input", {}) or {}
                if tname == "Bash":
                    cmd = str(tinp.get("command", "")).strip()[:200]
                    action_parts.append(f"  (ran shell: {cmd})")
                elif tname == "Read":
                    p = tinp.get("file_path", "?")
                    action_parts.append(f"  (read: {p})")
                elif tname == "Write":
                    p = tinp.get("file_path", "?")
                    action_parts.append(f"  (wrote: {p})")
                elif tname == "Edit":
                    p = tinp.get("file_path", "?")
                    action_parts.append(f"  (edited: {p})")
                elif tname == "Grep":
                    pat = tinp.get("pattern", "?")
                    action_parts.append(f"  (grep: {pat})")
                elif tname == "Glob":
                    pat = tinp.get("pattern", "?")
                    action_parts.append(f"  (glob: {pat})")
                elif tname == "WebFetch" or tname == "WebSearch":
                    q = tinp.get("query", tinp.get("url", "?"))
                    action_parts.append(f"  ({tname}: {q})")
                else:
                    short = json.dumps(tinp)[:120]
                    action_parts.append(f"  ({tname}: {short})")
            elif btype == "tool_result":
                body = str(block.get("content", "")).strip()
                if body and any(m in body for m in _TS_OUTPUT_MARKERS):
                    continue  # teammate-sync corpus/echo result — skip recursion
                if body:
                    snippet = body[:200].replace("\n", " ")
                    action_parts.append(f"    -> {snippet}")

        body_text = "\n".join(t for t in text_parts if t.strip()).strip()
        if body_text:
            rendered_blocks.append(f"{speaker}: {body_text}")
        if action_parts:
            rendered_blocks.append("\n".join(action_parts))

    return "\n\n".join(rendered_blocks), max_epoch


def format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} seconds ago"
    if seconds < 3600:
        return f"{int(seconds / 60)} minutes ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)} hours ago"
    return f"{int(seconds / 86400)} days ago"


def format_active_sessions(raw: bytes, allowed_ids: set | None = None) -> str:
    """Render the .active-sessions.json registry as a readable section.

    If allowed_ids is given, only sessions whose id is in it are shown — used
    to scope the live snapshot to sessions actually shared with this reader."""
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
        if allowed_ids is not None and sid not in allowed_ids:
            continue
        cwd = s.get("cwd") or "?"
        last_epoch = s.get("last_activity_epoch")
        if isinstance(last_epoch, (int, float)):
            last_age = format_age(max(0, now - last_epoch))
        else:
            last_age = "unknown"
        lines.append(f"- session {sid}: cwd={cwd}, last active {last_age}")
    return "\n".join(lines)


def _get_active_session_id(backend: StorageBackend, allowed_ids: set | None = None) -> str | None:
    """
    Read .active-sessions.json and return the session_id of the most recently
    active session, used to flag the live session in the corpus. Scoped to
    allowed_ids (sessions shared with this reader) when given.
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
        if allowed_ids is not None and s.get("session_id") not in allowed_ids:
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

    # Sessions actually shared with this reader (the .jsonl ACL already scopes
    # these per-recipient). Used to scope the live snapshot below, so it can't
    # leak the existence/cwd of sessions that weren't /connect-ed.
    shared_ids = {
        k[:-len(".jsonl")].split("/")[-1]
        for k in keys if k.endswith(".jsonl")
    }

    # Active sessions (live state) — first so it's most salient
    active_bytes = backend.get_bytes(ACTIVE_SESSIONS_FILENAME)
    if active_bytes:
        formatted = format_active_sessions(active_bytes, shared_ids)
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
    active_session_id = _get_active_session_id(backend, shared_ids)

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


def load_live_transcript(backend: StorageBackend) -> str:
    """Just the LIVE session — what the teammate is working on right now.

    Powers `/ask`: returns ONLY the active (or, if none flagged live, the
    most-recent) session's rendered transcript. No CLAUDE.md, no older
    sessions, no notes, no metadata block — the present-tense view. For
    accumulated decisions across the team, /ask-all reads knowledge.md."""
    keys = backend.list_keys()
    jsonl_keys = [k for k in keys if k.endswith(".jsonl")]
    if not jsonl_keys:
        return "[Error: no live session visible]"

    shared_ids = {k[:-len(".jsonl")].split("/")[-1] for k in jsonl_keys}
    active_id = _get_active_session_id(backend, shared_ids)

    best_key, best_epoch, best_rendered = None, -1.0, ""
    for key in jsonl_keys:
        content = backend.get_bytes(key)
        if content is None:
            continue
        rendered, epoch = render_jsonl_session(key, content)
        if not rendered:
            continue
        uuid = key[:-len(".jsonl")].split("/")[-1]
        # Strongly prefer the flagged-live session; otherwise newest by time.
        score = epoch + (1e12 if active_id and uuid == active_id else 0)
        if score > best_epoch:
            best_key, best_epoch, best_rendered = key, score, rendered

    if not best_rendered:
        return "[Error: no live session visible]"
    sid = best_key[:-len(".jsonl")].split("/")[-1]
    return f"=== LIVE session {sid} ===\n{best_rendered}"


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
    the GitHub handles of all org members. To actually query someone, they
    must have a connection accepted with you AND have /share'd a session
    explicitly with you.

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
def get_teammate_context(teammate: str) -> str:
    """Fetch a teammate's raw Claude Code session context.

    Returns the literal assembled corpus of a connected teammate's shared
    sessions — their CLAUDE.md (if any), per-session transcripts (rendered
    from jsonl into readable text), and a live "active sessions" snapshot
    showing which session they're typing in right now. Sessions are
    annotated with [ACTIVE — LIVE NOW] / [MOST RECENT SESSION] /
    [older session #N] so the calling Claude can pick the right one.

    There is NO synthesis. The calling Claude must read this corpus and
    answer the user's question itself, citing by session ID.

    If the teammate hasn't `/connect`-ed any session with the caller (or
    the caller isn't trusted yet), the corpus will be empty and the tool
    returns a message explaining what's missing.

    Args:
        teammate: The teammate's GitHub handle, or a local alias the user
            set via `teammate-sync alias` (e.g. "om" → "om-divyatej").
            Aliases resolve automatically; unknown values are treated as
            handles.

    Returns:
        Multi-section text:
          - usage hint header (how to cite, how to handle missing data)
          - freshness stamp (how recent this teammate's last sync was)
          - === ACTIVE SESSIONS (live) === block
          - === CLAUDE.md === block (if shared)
          - === Session <id> [ACTIVE — LIVE NOW] — last message <ts> === blocks
          - === Note: <path> === blocks for any other shared .md files
        Capped at ~400KB; truncated with a marker if larger.
    """
    from .aliases import resolve as _resolve_alias
    teammate = _resolve_alias(teammate)
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

    corpus = load_live_transcript(backend)
    if corpus.startswith("[Error"):
        return (
            f"No live session visible from {teammate}.\n\n"
            f"They need an ACTIVE Claude Code session /connect-ed with you (and "
            f"the sync engine running) for a live view. For accumulated "
            f"decisions across the team — which work even when people are "
            f"offline — use /ask-all instead.\n\n"
            f"Raw backend error: {corpus}"
        )

    freshness = get_sync_freshness(backend)
    if freshness is None:
        freshness_line = "# Freshness: unknown\n"
    else:
        age_str = format_age(freshness["age_seconds"])
        warn = " ⚠️ STALE (>30 min)" if freshness["is_stale"] else ""
        freshness_line = f"# Freshness: synced {age_str}{warn}\n"

    header = (
        f"# teammate-sync LIVE view — teammate: {teammate}\n"
        f"{freshness_line}"
        f"#\n"
        f"# This is what {teammate} is working on RIGHT NOW (their live\n"
        f"# session). Answer the user's question using ONLY this, cite the\n"
        f"# session id. Say exactly 'Not found in shared context.' if absent.\n"
        f"# For past decisions / team knowledge, the user should use /ask-all.\n"
        f"\n"
    )
    return header + corpus


@mcp.tool()
def ask_teammate_live(teammate: str, question: str) -> str:
    """Ask a teammate's LIVE Claude session a question and get their answer.

    This is the primary `/ask` path. The question is sent to the teammate's
    machine, where THEIR Claude answers it from their real, current session —
    read-only — and posts back just the answer. Their raw transcript never
    leaves their machine; you only receive the answer. Works while they're
    online with the engine running.

    If they're offline / don't answer in time, this falls back to their
    recorded decisions (knowledge.md) and says so.

    Args:
        teammate: GitHub handle or local alias of the person to ask.
        question: The question to answer from their live session.

    Returns:
        The teammate's answer (+ a citation), or their recorded decisions as a
        fallback, or a clear "not reachable" message.
    """
    import time as _time
    from .aliases import resolve as _resolve_alias
    teammate = _resolve_alias(teammate)
    try:
        auth = read_auth()
    except (FileNotFoundError, ValueError) as e:
        return f"[Error: {e}]"
    base = auth["backend_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {auth['token']}"}
    import httpx
    # 1. Enqueue the question for the teammate's daemon.
    try:
        r = httpx.post(f"{base}/v1/query",
                       json={"org": auth["org"], "target": teammate, "question": question},
                       headers=headers, timeout=15)
    except httpx.HTTPError as e:
        return f"[Error reaching backend: {e}]"
    if r.status_code != 200:
        return f"[Error: backend returned {r.status_code}: {r.text[:200]}]"
    qid = r.json()["query_id"]

    # 2. Poll for the answer (their Claude takes a few seconds to tens of seconds).
    deadline = _time.time() + 55
    while _time.time() < deadline:
        _time.sleep(3)
        try:
            qr = httpx.get(f"{base}/v1/query/{qid}", params={"org": auth["org"]},
                           headers=headers, timeout=15)
        except httpx.HTTPError:
            continue
        if qr.status_code != 200:
            continue
        q = qr.json()
        if q.get("status") == "answered":
            cite = f" ({q['citation']})" if q.get("citation") else ""
            return f"{q.get('answer', '').strip()}\n\n— @{teammate}, live{cite}"

    # 3. Timed out → fall back to their recorded decisions (offline path).
    fallback = _knowledge_for(teammate, auth, headers, base)
    if fallback:
        return (f"@{teammate} didn't answer live (likely offline). From their "
                f"recorded decisions:\n\n{fallback}")
    return (f"@{teammate} isn't reachable live and has no recorded decisions yet. "
            f"Say exactly 'Not found in shared context.'")


def _knowledge_for(teammate: str, auth: dict, headers: dict, base: str) -> str | None:
    """Fetch just `teammate`'s knowledge.md from the durable store, for the
    offline fallback of a live query."""
    import httpx
    try:
        r = httpx.get(f"{base}/v1/knowledge", params={"org": auth["org"]},
                      headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        for d in r.json().get("docs", []):
            if d.get("engineer_handle") == teammate:
                return d.get("content")
    except httpx.HTTPError:
        return None
    return None


@mcp.tool()
def query_team_knowledge() -> str:
    """Fetch the team's accumulated decision knowledge across the whole org.

    Powers `/ask-all`. Returns every engineer's distilled knowledge.md (their
    decisions + the why, with dates/times), read from the durable server-side
    store — so it works even when those teammates are OFFLINE. Use this for
    "why did we decide X", "has anyone dealt with Y", "what's the state of Z"
    questions. For what someone is doing right now, use get_teammate_context
    (the live view) instead.

    Returns:
        Concatenated per-engineer knowledge docs, newest-updated first, each
        under a `=== @handle (updated <ts>) ===` header. The calling Claude
        reads these and answers, citing the engineer + decision.
    """
    try:
        auth = read_auth()
    except (FileNotFoundError, ValueError) as e:
        return f"[Error: {e}]"
    import httpx
    try:
        r = httpx.get(
            f"{auth['backend_url'].rstrip('/')}/v1/knowledge",
            params={"org": auth["org"]},
            headers={"Authorization": f"Bearer {auth['token']}"},
            timeout=20,
        )
    except httpx.HTTPError as e:
        return f"[Error reaching backend: {e}]"
    if r.status_code != 200:
        return f"[Error: backend returned {r.status_code}: {r.text[:200]}]"
    docs = r.json().get("docs", [])
    if not docs:
        return ("No team knowledge yet. Decisions appear here as teammates work "
                "with decision-capture enabled. Say exactly 'Not found in shared "
                "context.' to the user.")
    sections = [
        "# teammate-sync TEAM KNOWLEDGE (org-wide, offline-readable)\n"
        "# Each block is one engineer's distilled decisions. Answer the user's\n"
        "# question from these, cite the engineer + decision. Prefer entries\n"
        "# with the newest date/time. Say 'Not found in shared context.' if absent.\n"
    ]
    for d in docs:
        when = datetime.fromtimestamp(d["updated_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        sections.append(f"=== @{d['engineer_handle']} (updated {when} UTC) ===\n{d['content']}")
    return "\n\n".join(sections)


if __name__ == "__main__":
    mcp.run()
