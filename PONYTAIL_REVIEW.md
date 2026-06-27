# Ponytail Review — CodeBaton / teammate-sync

> Review-only audit against the "Ponytail, lazy senior dev mode" rubric (YAGNI,
> deletion over addition, fewest files, no speculative abstraction; but NOT lazy
> about trust-boundary validation, data loss, security; non-trivial logic leaves
> one runnable check). Nothing implemented — captured for later action.
>
> Date: 2026-06-28. Codebase: ~8,550 lines Python + ~400 JS + ~1,074 docs HTML.
> Method: three parallel reviewers over core query path / CLI+UI / backend+support.

## Headline verdict

The bloat is **almost entirely pivot residue, not gratuitous cleverness.** The
product pivoted (upload/sync transcripts → federated: nothing leaves the machine,
the teammate's Claude answers locally). The pivot was correct, but the new path
was added **on top of** the old upload/sync/ACL/dump architecture **without
deleting it** — violating the rubric's top rule, "deletion over addition."

Result: roughly **20–30% of the Python (~1,500–2,500 lines) is dead-or-broken.**
Worse than plain dead code, several pieces are **dead-or-*broken*** — still wired
(`get_teammate_context`, dashboard session view, `/show`) but reading tables the
daemon no longer fills, so they silently return empty.

One-line: **the federated product is ~5,000 lines wearing an ~8,500-line coat, and
a few of the coat's pockets are unlocked.**

---

## A. Deletable map (ranked)

### Delete today — zero risk
- **Append-delta upload chain** — `FileAppendRequest` (main.py:133-140),
  `POST /v1/files/append` (main.py:510-544), `append_file` (storage.py:270-312),
  `get_file_size` (storage.py:315-323), `append_bytes` (backend.py:71-84, 294-331).
  **Zero callers anywhere.** ~150 lines. Speculative ("saves 95% bandwidth" — never wired).
- **daemon.py dead funcs** — `initial_sync_all` (134-197, never called),
  `filter_active_sessions` (117-131, only called by the dead one),
  `last_uploaded_size` bookkeeping (139,186-194,243), `is_share_mode_active`
  (112-114, never called), `read_shared_session_ids` (107-109, only used by dead). ~80+ lines.
- **Duplication** — `_pid_alive`/`_read_pid` copied 3× (cli.py:696-715,
  dashboard.py:113-128, macapp.py:92-108); `_resolve_self_binary` defined twice in
  cli.py (95-110 AND 539-560; first is dead); `_read_notify_prefs`/`_write_notify_prefs`
  duplicated dashboard.py↔macapp.py. Dashboard "Desktop notifications" toggle
  (dashboard.py:533-541, `/settings/notifications`) writes a pref **nothing in the
  dashboard ever reads** (only macapp.py consumes it) — effectively a no-op switch.

### Delete pending a product decision
- **macapp.py entirely (531 lines)** — redundant SECOND desktop UI (rumps menu bar)
  that shells out to the CLI and duplicates the dashboard, incl. its own
  `_poll_connections` notification engine (462-496). Biggest single-file deletion if
  the dashboard is the surface.
- **server.py corpus machinery (~350 lines)** — `render_jsonl_session` (55-311, 256
  lines), `load_corpus` (385-470, never called), `format_active_sessions` (324-351,
  only called by dead load_corpus), `_get_active_session_id`, `get_sync_freshness`,
  and likely `get_teammate_context` (549-625). Orphaned by the pivot.
  `_TEAMMATE_OUTPUT_MARKERS`/`_TS_OUTPUT_MARKERS` die with it.
  ⚠️ Confirm `get_teammate_context` is no longer a live tool path before deleting.
- **Backend file/ACL/dump subsystem (~350 of storage.py's 830 lines)** — tables
  `files`/`session_shares`/`sync_state` (storage.py:37-83); `put_file`, `list_paths`,
  `delete_file`, `get_file`, `can_read_file`, `list_visible_paths`, `share_session`,
  `unshare_session`, `list_sessions_shared_by/with`, `_session_id_from_path`,
  `dashboard_snapshot`; endpoints `POST/GET/DELETE /v1/files`, `/v1/files/get`,
  `/v1/dump`, `/v1/sessions/share|unshare`, `/v1/state`, `/v1/dashboard`; client
  `backend.py` upload half (`put_bytes`,`get_bytes`,`list_keys`,`delete_key`,
  `get_state`/`put_state`,`dump`,`dashboard`,`unshare_session`).
  ⚠️ Still *referenced* by server.py (`list_keys`/`get_state`) and dashboard.py
  (`/dump`,`dashboard`) — so they're dead-OR-broken (return empty post-pivot). Decide:
  drop `/show` + dashboard session-view → delete; keep → they're broken and need
  rewiring to knowledge/queries. `/v1/files/purge` (558): keep short-term (daemon
  cleanup uses it), then retire.
- **S3Backend + LocalBackend + StorageBackend ABC + make_s3_backend_for +
  list_s3_teammates** (backend.py:110-208, 496-533, ~150 lines) — "legacy, kept for
  hosted-self-deploy." With only `cloud` live, the 3-impl polymorphism earns nothing.
- **In-app self-update subsystem** — `cmd_self_update` (cli.py:903-972) staging-swap +
  `polluted` detection, `/update/check`+`/update/status`, 5-state banner
  (dashboard.py:903-941). For a pipx tool, `pipx upgrade` (already `cmd_upgrade`) is
  the boring path.
- **Second OAuth implementation** — dashboard in-app sign-in (signin-screen
  601-634 + JS 852-900 + `/auth/start|callback|orgs|finish`) duplicates `cmd_init`
  (cli.py:176-274).
- **share_cli.py orphaned commands** — `cmd_show` (526-560), `cmd_accept` (374-391),
  `cmd_decline` (393-407), `cmd_connections` (338-371), `cmd_unshare` (218-278),
  `remove_shared_session` (281-296, verify SessionEnd-hook caller in hook.py first).
  Their slash commands are explicitly retired (`_RETIRED_SLASH_COMMANDS`, cli.py:371);
  module docstring (8-17) still advertises them — at least fix the docstring.

### Minor
- federated.py escalation ladder: middle attempt (transcript-path, skip_perms=False)
  is least likely to ever succeed; likely droppable.
- `cmd_dashboard` `--browser` vs `--no-browser` redundant flag surface (cli.py:613-615).
- `MeResponse.email/name` second GitHub `/user/emails` round-trip (main.py:224-233) —
  used anywhere?
- `kind="list"` query branch (main.py:465, storage.py:182) — confirmed in use (picker).

---

## B. NOT-LAZY-ENOUGH gaps — on LIVE paths (fix regardless of cleanup)

These matter MORE than deleting code that's about to be deleted.

1. **`GITHUB_MEMBERSHIP_CACHE` never expires/evicts** (main.py:196-197) — a user
   removed from the org keeps access until process restart (authz staleness) AND it
   grows unbounded with token churn. Add TTL + size bound. **Real security bug.**
2. **No size cap on `/v1/knowledge`** (main.py:165-167, unbounded `content`) — the one
   live write path; any member can disk-fill the Fly volume. Cap at the pydantic model.
   (Same for the about-to-be-deleted file endpoints.)
3. **Local dashboard HTTP server unauthenticated, no Origin/CSRF check** — every
   mutating POST (`/disconnect`,`/daemon/stop`,`/logout`,`/settings/claude-token`,
   `/auth/finish`) is open to any local page; `/dump` (dashboard.py:1086-1095) returns
   **raw teammate session bytes** to anything guessing the random port. Sharpest hole
   for a consent-gated product. Add Origin/Host check and/or per-launch nonce.
4. **`poll_and_answer` has no per-query try/except around `_answer_one`**
   (federated.py:393) — a bad `cwd` (deleted dir) raises `FileNotFoundError`/
   `NotADirectoryError` (NOT caught by the per-attempt `except TimeoutExpired`,
   federated.py:320-326), escaping and **dropping every other pending answer that cycle.**
5. **Unbounded question length** (federated.py:372) interpolated into `_WRAP` and
   subprocess argv. Truncate (e.g. `question[:4000]`). Prompt-injection wrapping is
   present and good; size bound is missing.
6. **`org` / handles unvalidated before GitHub URL interpolation** (main.py:247,341,
   SSRF-ish) — validate `^[A-Za-z0-9-]+$` at the boundary. Same for target/peer/path.
7. **Secrets in error responses** — main.py:303 echoes raw GitHub token-exchange
   payload to client; claude_oauth.py:153 logs `r.text[:200]` of token responses.
8. **Fork-cleanup race** (federated.py:287,342-347) — snapshots `*.jsonl` before,
   deletes the set-difference after; a real session file created in the project dir
   during the ~150s window gets `unlink`ed. Data-loss category. Fix: capture the fork's
   own session id from the stream-json `init` event, delete only that file.
9. `create_query` doesn't re-verify `target` is still an org member (main.py:457-468) —
   minor given the connection gate, same staleness theme as #1.
10. `/auth/finish` blanket `except Exception` → 500 leaks backend error text (minor).

---

## C. Missing self-checks (rubric's "one runnable check" — essentially unmet)

No tests anywhere for load-bearing logic:
- Consent gate: `is_connected` / `answer_query` / `get_query` party-checks
  (storage.py:224,243) — security-load-bearing, no test.
- `_connected_active` (federated.py:91) — the consent boundary; no check that a
  non-connected asker yields `[]`.
- Distiller shrink-guard + supersession (distiller.py:177-186) — non-trivial heuristic.
- `connection_request` auto-accept-on-mutual-pending (storage.py:440-453) — subtle
  bidirectional state.
- `render_jsonl_session` (server.py) — most intricate logic in the repo (multi-state
  skip_mode/ask_pending machine); no test (moot if deleted).
- `extract_session_id_from_path` / `filter_active_sessions` (daemon.py:64,117) —
  the privacy gate deciding which sessions are reachable; security-load-bearing.
- `_stable_binary` shell-safety allowlist (cli.py:43-64).
- `cmd_self_update` staging-swap / `polluted` detection (cli.py:903-972) — can brick
  an install; no test.
- `_last_human_turn` / `_session_cwd` JSONL parsing (federated.py).

---

## D. Genuinely right-sized (leave alone)

- Federated answerer, consent model, hook — well-built, appropriately lazy.
- SQL fully parameterized (no string-built queries).
- ACL ownership token-bound (owner = `user["login"]`, never client-supplied) → A can't write as B.
- `answer_query` enforces `target_handle == caller` in SQL WHERE (storage.py:224);
  `get_query` checks requester is a party (storage.py:243).
- OAuth `state` CSRF check (claude_oauth.py:116); `auth.json`/claude-token 0600
  (auth.py:48,129).
- Distiller fail-safe + redacts secrets in prompt.
- `/settings/claude-token` regex validation (dashboard.py:1244) — appropriately not-lazy.

---

## E. Suggested action order (when we act)

1. Fix the live-path gaps in §B first (esp. #1 cache TTL, #2 size cap, #3 dashboard
   Origin/nonce, #4 per-query try, #5 question cap).
2. Delete the zero-caller append-delta chain + dead daemon funcs (§A delete-today).
3. Make the product call on macapp.py / get_teammate_context / `/dump` + dashboard
   session-view, then delete the corpus + file/ACL subsystem behind it.
4. Drop S3/LocalBackend/ABC and the in-app self-update + second OAuth.
5. Add the handful of self-checks in §C for the surviving security/data paths.

## F. Product decisions needed before deleting (don't delete blind)
- Is `get_teammate_context` (the raw-corpus tool) still a feature, or fully replaced
  by `ask_teammate` + the picker?
- Is `/show` (dump raw teammate session) dropped post-pivot?
- Is the dashboard's Sessions/raw-context view dropped, or rewired to knowledge/queries?
- One desktop UI (dashboard) or keep the menu-bar app?
- Keep in-app self-update, or rely on `pipx upgrade`?
