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


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _pick_session_for(asker: str) -> dict | None:
    """Pick which session to answer `asker` from — STRICTLY one the engineer
    explicitly /connect-ed with THIS asker. Never any other session.

    Consent rule: a session is answerable only if /connect was run in it for
    this person (it's in .shared-sessions.json with `asker` as a recipient).
    Among those, pick the most-recently-active. A session the engineer never
    /connect-ed (e.g. a personal project) is NEVER reachable, even if it's the
    one they're currently typing in. Returns {session_id, cwd, transcript_path}
    or None."""
    from .backend import SHARED_SESSIONS_FILENAME
    shared = _read_json(_state_dir() / SHARED_SESSIONS_FILENAME).get("sessions", [])
    # session_ids explicitly /connect-ed with this asker
    connected_ids = {
        s["session_id"] for s in shared
        if isinstance(s, dict) and asker in (s.get("recipients") or [])
    }
    if not connected_ids:
        return None

    active = _read_json(_state_dir() / ACTIVE_SESSIONS_FILENAME).get("sessions", [])
    # Only connected AND currently-active sessions are forkable; pick newest.
    candidates = [
        s for s in active
        if isinstance(s, dict) and s.get("session_id") in connected_ids
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda s: s.get("last_activity_epoch") or 0, reverse=True)
    return candidates[0]


def _session_cwd(transcript_path: str | None) -> str | None:
    """The directory the session was actually started in, read straight from the
    transcript JSONL (each entry records a "cwd"). This is ground truth — more
    reliable than the active-sessions registry and avoids decoding the dashed
    project-dir name (ambiguous: gmr-qc vs gmr/qc)."""
    if not transcript_path:
        return None
    try:
        with open(transcript_path) as fh:
            for _ in range(30):
                line = fh.readline()
                if not line:
                    break
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(o, dict) and o.get("cwd"):
                    return o["cwd"]
    except OSError:
        pass
    return None


def _build_cmd(claude_binary: str, resume_arg: str, wrapped: str, skip_perms: bool) -> list[str]:
    """Read-only fork-resume command. `--allowedTools` keeps it Read/Grep/Glob
    only; per Claude Code docs that allowlist still constrains tools even when
    `--dangerously-skip-permissions` is set (skip only suppresses prompts for
    already-allowed tools — it does NOT re-grant Edit/Write/Bash)."""
    cmd = [
        claude_binary, "-p", "-r", resume_arg, "--fork-session",
        "--output-format", "stream-json", "--verbose",
        "--allowedTools", "Read Grep Glob",
        "--disallowedTools", " ".join(_SECRET_DENY),
        "--strict-mcp-config",
    ]
    if skip_perms:
        # Clears the folder-trust gate that blocks headless resume (no TTY to
        # answer the "trust this directory?" prompt). Read-only still enforced
        # by --allowedTools above.
        cmd.append("--dangerously-skip-permissions")
    cmd.append(wrapped)
    return cmd


def _parse_answer(stdout: str) -> str:
    """Parse stream-json events: log the model's process (reads/greps/text) and
    return the final answer from the terminal 'result' event."""
    answer = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "assistant":
            for blk in ev.get("message", {}).get("content", []):
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "text" and blk.get("text", "").strip():
                    _log(f"    · thinking/text: {blk['text'].strip()[:300]}")
                elif blk.get("type") == "tool_use":
                    inp = blk.get("input", {}) or {}
                    tgt = inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
                    _log(f"    · {blk.get('name','tool')}: {str(tgt)[:120]}")
        elif etype == "result":
            answer = (ev.get("result") or "").strip()
    return answer


def _answer_one(question: str, asker: str, claude_binary: str, token: str) -> tuple[str, str]:
    """Produce (answer, citation) by forking a /connect-ed session read-only.

    Two attempts. Resume-by-session-id is scoped to the project dir and can be
    blocked by the headless folder-trust prompt; if it fails, fall back to the
    unambiguous full-transcript-path form with --dangerously-skip-permissions
    (still read-only via --allowedTools)."""
    sess = _pick_session_for(asker)
    if not sess:
        return (f"Not found in shared context — no session is /connect-ed with @{asker} "
                f"right now.", "")

    sid = sess["session_id"]
    transcript_path = sess.get("transcript_path")
    # Real launch dir from the transcript itself; fall back to the registry cwd.
    cwd = _session_cwd(transcript_path) or sess.get("cwd") or str(Path.home())
    project_dir = Path(transcript_path).parent if transcript_path else None
    citation = f"session {sid[:8]}"

    # Snapshot existing session files so we can delete the throwaway fork after.
    before = set(project_dir.glob("*.jsonl")) if project_dir and project_dir.exists() else set()

    env = dict(os.environ)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")

    import time as _time
    _log("")
    _log(f"════ Q from @{asker} @ {int(_time.time())} ════")
    _log(f"  question: {question}")
    _log(f"  answering from session {sid[:8]} (cwd={cwd})")

    wrapped = _WRAP.format(asker=asker, question=question)

    # Escalating attempts: (label, resume arg, skip_perms). skip-perms is the
    # genuine last resort. id-resume is the documented form but fails for the
    # currently-live/locked session; path-resume opens the transcript directly
    # and works where id-lookup misses. Try the cheap/safe forms first.
    attempts = [("session-id", sid, False)]
    if transcript_path:
        attempts.append(("transcript-path", transcript_path, False))
        attempts.append(("transcript-path", transcript_path, True))

    answer = ""
    try:
        for idx, (label, resume_arg, skip_perms) in enumerate(attempts):
            is_last = idx == len(attempts) - 1
            # Non-final attempts get a tighter cap so a folder-trust hang bails
            # quickly to the skip-perms last resort instead of eating 150s.
            to = 150 if is_last else 60
            _log(f"  → attempt [{label}] skip_perms={skip_perms} timeout={to}s")
            t0 = _time.time()
            cmd = _build_cmd(claude_binary, resume_arg, wrapped, skip_perms)
            try:
                res = subprocess.run(
                    cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=to,
                )
            except subprocess.TimeoutExpired:
                _log(f"    ✗ [{label}] timed out ({to}s)")
                continue
            if res.returncode != 0:
                detail = (res.stderr.strip() or res.stdout.strip())[:300]
                _log(f"    ✗ [{label}] claude failed rc={res.returncode}: {detail}")
                continue
            answer = _parse_answer(res.stdout)
            if answer:
                _log(f"  ✓ answer via [{label}] ({_time.time()-t0:.1f}s): {answer[:500]}")
                return (answer, citation)
            # rc==0 but no parsed result — maybe plain text slipped through.
            answer = res.stdout.strip()[:1000]
            if answer:
                _log(f"  ✓ answer via [{label}] (raw, {_time.time()-t0:.1f}s): {answer[:500]}")
                return (answer, citation)
            _log(f"    · [{label}] produced no answer; trying fallback")
    finally:
        if project_dir and project_dir.exists():
            for f in set(project_dir.glob("*.jsonl")) - before:
                try:
                    f.unlink()
                except OSError:
                    pass

    _log("  ✗ all attempts exhausted")
    return ("Couldn't generate an answer right now.", citation)


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
