"""
Silent decision distiller.

Turns a raw Claude Code session into a living, cited `knowledge.md` of
DECISIONS and findings — the "why" that git and Slack lose. Runs entirely in
the background (spawned detached by the daemon), using the engineer's own
`claude` CLI (free, already authed). Never touches the user's interactive
session: no blocking, no UI, no terminal output.

Design constraints (all load-bearing):
  - SILENT: invoked detached by the daemon; all output goes to a log file.
  - NON-BLOCKING: the daemon never waits on it.
  - RECURSION-SAFE: the `claude -p` call runs in a neutral working dir, so the
    session it creates isn't inside a shared project and won't itself be
    distilled or synced.
  - FAIL-SAFE: any error is swallowed + logged; sync is never affected.
"""

import os
import subprocess
from pathlib import Path

# The maintenance prompt — this is the product. It tells the engineer's own
# Claude to fold the latest session turns into knowledge.md, superseding
# (never deleting) reversed decisions, and citing every entry.
_DISTILL_PROMPT = """\
You maintain a living knowledge.md for ONE engineer on ONE project — the
durable record of DECISIONS and hard-won findings from their coding sessions
(the "why" that git and Slack lose). A teammate's AI reads it to understand the
work without interrupting anyone.

You are given the CURRENT knowledge.md (may be empty) and the engineer's most
recent session transcript. Output ONLY the updated knowledge.md — nothing else,
no preamble, no code fences around the whole thing.

CAPTURE (decisions + durable findings, NOT activity):
- Decisions: a choice between alternatives, with the reason ("chose X over Y because Z").
- Findings/gotchas: a non-obvious fact learned the hard way.
- Current state a teammate would need to know.
- SKIP routine edits, files merely read, dead ends, anything git already records.

SUPERSESSION (use judgement):
- New decision REVERSES an old one -> do NOT delete it. Move the old entry to
  `## Superseded`, strike it through, stamp the date it changed, one line on
  what replaced it. Put the new decision under `## Current decisions`.
- New turn REFINES an existing decision -> update that entry in place.
- Be conservative: "superseded" means genuinely reversed, not elaborated.

FORMAT — keep the existing structure; if empty, start with:
## Current decisions
### <short title>
**Decision:** ...
**Why:** ...
**Touches:** <files / systems>
_(session {session_id}, {date})_

## Superseded
(struck-through past decisions, each with the date it changed)

INVARIANTS:
- Cite every entry with its source session id + date. Never invent a citation.
- No secrets: write [redacted] for any API key, token, or .env value.
- Stay tight — a teammate skims this. No duplicates. Preserve everything you
  are not actively changing.

=== CURRENT knowledge.md ===
{current_knowledge}

=== RECENT SESSION (id {session_id}, {date}) ===
{session_text}
"""


def _work_dir() -> Path:
    """Neutral cwd for the distiller's own claude run — OUTSIDE any shared
    project, so the session it spawns isn't watched, shared, or re-distilled."""
    d = Path("~/.teammate-sync/distill-work").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log(msg: str) -> None:
    log = Path("~/.teammate-sync/state/distiller.log").expanduser()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(msg.rstrip() + "\n")


def build_prompt(session_text: str, current_knowledge: str, session_id: str, date: str) -> str:
    return _DISTILL_PROMPT.format(
        current_knowledge=current_knowledge or "(empty — this is the first entry)",
        session_text=session_text,
        session_id=session_id,
        date=date,
    )


def distill_session(
    session_jsonl: Path,
    knowledge_path: Path,
    session_id: str,
    date: str,
    claude_binary: str,
    max_chars: int = 60000,
) -> bool:
    """Read a session, fold it into knowledge.md via the engineer's own Claude.
    Returns True if knowledge.md was updated. Fail-safe: returns False on any
    error (logged), never raises — the daemon must never crash on distill."""
    try:
        from .server import render_jsonl_session  # reuse the noise-stripping renderer
        raw = session_jsonl.read_bytes()
        session_text, _ = render_jsonl_session(session_jsonl.name, raw)
        if not session_text.strip():
            return False
        session_text = session_text[-max_chars:]  # cap to recent context

        current = knowledge_path.read_text() if knowledge_path.exists() else ""
        prompt = build_prompt(session_text, current, session_id, date)

        # The engineer's own Claude, headless, in a neutral cwd. -p = one-shot
        # print mode (no interactive UI). Prompt on stdin so it can't blow the
        # arg-length limit.
        env = dict(os.environ)
        env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
        res = subprocess.run(
            [claude_binary, "-p", "--permission-mode", "bypassPermissions"],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(_work_dir()),
            env=env,
            timeout=180,
        )
        if res.returncode != 0:
            _log(f"[distill] claude failed rc={res.returncode}: {res.stderr.strip()[:300]}")
            return False
        updated = res.stdout.strip()
        if not updated or "## Current decisions" not in updated:
            _log(f"[distill] unexpected output (len={len(updated)}); skipping write")
            return False

        knowledge_path.parent.mkdir(parents=True, exist_ok=True)
        knowledge_path.write_text(updated + "\n")
        _log(f"[distill] updated {knowledge_path.name} from session {session_id} ({len(updated)}B)")
        return True
    except Exception as e:  # fail-safe: distillation must never break the daemon
        _log(f"[distill] error: {e}")
        return False
