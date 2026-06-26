"""
Federated live-query answerer (target side).

When a teammate asks Om a live question, Om's daemon answers it HERE — locally,
from Om's real session context — and posts back only the answer. Om's raw
transcript never leaves his machine; only the answer transits the backend.

Mechanism (all flags verified against Claude Code docs):
    claude -p -r <session-id> --fork-session
        --allowedTools "Read Grep Glob"   # no Edit/Write/Bash → no mutation
        --disallowedTools "<secret paths>" # exfiltration guard
        --strict-mcp-config                # don't load teammate-sync's own MCP
        "<read-only wrapped question>"
with CLAUDE_CODE_OAUTH_TOKEN set and cwd = the session's project dir.

Guardrails:
  - --fork-session: answers from an isolated copy, never corrupts the live session.
  - read-only tools only: editing/commands are structurally impossible.
  - secret-path deny + prompt wrapper: limit what a crafted question can exfiltrate.
  - hard timeout: the daemon never hangs.
"""

import json
import os
import subprocess
from pathlib import Path

import httpx

from .backend import ACTIVE_SESSIONS_FILENAME

# Reads of these are denied even though reading is otherwise allowed — a crafted
# question must not be able to exfiltrate credentials via the answer.
_SECRET_DENY = [
    "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.gcp/**)",
    "Read(~/.config/**)", "Read(~/.teammate-sync/**)",
    "Read(**/.env)", "Read(**/.env.*)", "Read(**/*.pem)", "Read(**/credentials*)",
]

_WRAP = """\
A teammate (@{asker}) is asking you a READ-ONLY question about what you're \
currently working on, answerable from THIS Claude Code session's context and \
the project files.

Rules:
- Lead with the answer, 1-4 sentences, concrete. No preamble.
- You MAY read files to inform the answer. Do NOT modify anything or run commands.
- If it's not answerable from this session/project, say exactly: "Not enough context to answer."

Question: {question}
"""


def _log(msg: str) -> None:
    p = Path("~/.teammate-sync/state/federated.log").expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(msg.rstrip() + "\n")


def _state_dir() -> Path:
    return Path("~/.teammate-sync/state").expanduser()


def _pick_live_session() -> dict | None:
    """Most-recently-active session from the registry: {session_id, cwd,
    transcript_path}. None if no active session to answer from."""
    raw_path = _state_dir() / ACTIVE_SESSIONS_FILENAME
    if not raw_path.exists():
        return None
    try:
        data = json.loads(raw_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    sessions = [s for s in data.get("sessions", []) if isinstance(s, dict) and s.get("session_id")]
    if not sessions:
        return None
    sessions.sort(key=lambda s: s.get("last_activity_epoch") or 0, reverse=True)
    return sessions[0]


def _answer_one(question: str, asker: str, claude_binary: str, token: str) -> tuple[str, str]:
    """Produce (answer, citation) by forking the live session read-only."""
    sess = _pick_live_session()
    if not sess:
        return ("Not enough context to answer — no live session right now.", "")

    sid = sess["session_id"]
    cwd = sess.get("cwd") or str(Path.home())
    project_dir = Path(sess["transcript_path"]).parent if sess.get("transcript_path") else None

    # Snapshot existing session files so we can delete the throwaway fork after.
    before = set(project_dir.glob("*.jsonl")) if project_dir and project_dir.exists() else set()

    env = dict(os.environ)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")

    cmd = [
        claude_binary, "-p", "-r", sid, "--fork-session",
        "--allowedTools", "Read Grep Glob",
        "--disallowedTools", " ".join(_SECRET_DENY),
        "--strict-mcp-config",
        _WRAP.format(asker=asker, question=question),
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=150,
        )
    except subprocess.TimeoutExpired:
        return ("Timed out generating an answer.", f"session {sid[:8]}")
    finally:
        # Delete the forked session jsonl(s) — cruft, not shared/distilled anyway.
        if project_dir and project_dir.exists():
            for f in set(project_dir.glob("*.jsonl")) - before:
                try:
                    f.unlink()
                except OSError:
                    pass

    if res.returncode != 0:
        detail = (res.stderr.strip() or res.stdout.strip())[:200]
        _log(f"[federated] claude failed rc={res.returncode}: {detail}")
        return ("Couldn't generate an answer right now.", f"session {sid[:8]}")
    answer = res.stdout.strip() or "Not enough context to answer."
    return (answer, f"session {sid[:8]}")


def poll_and_answer(token_getter, org: str, backend_url: str, claude_binary_getter) -> None:
    """One poll cycle: fetch pending queries addressed to me, answer each from
    my live session, post the answers back. Fail-safe — logs and continues."""
    token = token_getter()
    base = backend_url.rstrip("/")
    try:
        from .auth import read_auth
        auth = read_auth()
        headers = {"Authorization": f"Bearer {auth['token']}"}
        r = httpx.get(f"{base}/v1/query/pending", params={"org": org}, headers=headers, timeout=15)
        if r.status_code != 200:
            return
        pending = r.json().get("queries", [])
    except (httpx.HTTPError, FileNotFoundError, ValueError, KeyError):
        return
    if not pending:
        return

    for q in pending:
        qid, asker, question = q["id"], q.get("asker", "a teammate"), q["question"]
        if not token:
            answer, citation = (
                "Decision capture / live answering isn't authorized on this "
                "teammate's machine yet.", "")
        else:
            try:
                claude_binary = claude_binary_getter()
            except Exception:
                answer, citation = ("Claude CLI not available to answer.", "")
            else:
                answer, citation = _answer_one(question, asker, claude_binary, token)
        try:
            from .auth import read_auth
            auth = read_auth()
            httpx.post(
                f"{base}/v1/query/{qid}/answer",
                json={"org": org, "answer": answer, "citation": citation},
                headers={"Authorization": f"Bearer {auth['token']}"},
                timeout=15,
            )
            _log(f"[federated] answered query {qid[:8]} from @{asker}")
        except (httpx.HTTPError, FileNotFoundError, ValueError, KeyError) as e:
            _log(f"[federated] failed to post answer for {qid[:8]}: {e}")
