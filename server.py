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

from anthropic import Anthropic
from mcp.server.fastmcp import FastMCP

from backend import ACTIVE_SESSIONS_FILENAME, StorageBackend, make_backend_from_env


SYNTHESIS_MODEL = os.environ.get("TEAMMATE_SYNTHESIS_MODEL", "claude-sonnet-4-6")
# Soft cap on corpus bytes passed to the synthesis call. Claude Sonnet 4.6 has
# a large context window, but we keep this conservative for cost + latency.
MAX_CORPUS_BYTES = 400_000
STALE_THRESHOLD_SECONDS = 30 * 60  # 30 minutes


mcp = FastMCP("teammate-sync")
anthropic_client = Anthropic()
_BACKEND: StorageBackend = make_backend_from_env()
print(f"[server] backend: {_BACKEND!r}", flush=True)


SYNTHESIS_PROMPT_TEMPLATE = """You are reading a teammate's Claude Code working corpus to answer a coworker's question.

A coworker asks: {question}

Below is the teammate's corpus, in two parts:
  1. ACTIVE SESSIONS — live state from the teammate's currently-running Claude Code processes (cwd, last activity time). Use this for questions about what they are doing right NOW.
  2. PERSISTENT CORPUS — their CLAUDE.md, session transcripts, and scratch notes. Use this for historical or factual questions.

CORPUS:
{corpus}

Instructions:
- Answer ONLY the asked question. Do not summarize the corpus broadly.
- Be concise — under 500 words.
- CITE the source for every factual claim, e.g. "(CLAUDE.md)", "(session abc-123, message about the recursive trigger)", or "(active sessions: session abc-123)".
- If the corpus does not contain the answer, say exactly: "Not found in shared context."
- Do not speculate beyond what is written in the corpus.
- Do not include preamble like "Based on the corpus..." — just answer.

Answer:"""


def render_jsonl_session(filename: str, content: bytes) -> str:
    """
    Render a Claude Code session jsonl file (as bytes) into readable text.
    Extracts user prompts, assistant text, and a compact summary of tool
    use / tool results.
    """
    rendered_lines: list[str] = []
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception as e:
        return f"[Error decoding {filename}: {e}]"

    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

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

    return "\n\n".join(rendered_lines)


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

    # Session jsonl files
    jsonl_keys = [k for k in keys if k.endswith(".jsonl")]
    for key in sorted(jsonl_keys):
        content = backend.get_bytes(key)
        if content is None:
            continue
        rendered = render_jsonl_session(key, content)
        if rendered:
            sections.append(f"=== Session: {key} ===\n{rendered}")

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
def query_teammate_context(question: str) -> str:
    """Query a teammate's Claude Code working context.

    Use this tool when you need to know what a teammate has decided, discovered,
    or is working on in their Claude Code sessions. The tool reads their
    CLAUDE.md, session transcripts, and scratch notes, then returns a
    synthesized, cited answer (~500 words) drawn ONLY from their actual corpus.

    If the answer is not in the corpus, the tool returns:
    "Not found in shared context."

    Args:
        question: A natural-language question about the teammate's work.
                  Be specific — e.g., "What did they decide about cursor-based
                  pagination?" rather than "Tell me everything they did."

    Returns:
        A cited, synthesized answer string with a freshness stamp.
    """
    corpus = load_corpus(_BACKEND)
    if corpus.startswith("[Error"):
        return corpus

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
        return "[Error: synthesis returned no text]"

    freshness = get_sync_freshness(_BACKEND)
    if freshness is None:
        return answer

    age_str = format_age(freshness["age_seconds"])
    if freshness["is_stale"]:
        prefix = (
            f"⚠️  Stale sync warning: teammate's corpus was last "
            f"updated {age_str}. Information below may be outdated.\n\n"
        )
        suffix = f"\n\n— teammate's context as of {age_str}"
        return prefix + answer + suffix

    return f"{answer}\n\n— teammate's context as of {age_str}"


if __name__ == "__main__":
    mcp.run()
