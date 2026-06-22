# teammate-sync — Architecture

As of v0.3.1 — last updated 2026-06-22.

This doc explains how teammate-sync actually works. Read top-to-bottom on
first look. The most useful section if you're skimming is **"How a Query
Works"** at the bottom — that's the magic moment of the product.

---

## 1. The Product, in One Sentence

A teammate types `/connect <your-handle>` in their Claude Code session;
you `/connect <their-handle>` back. Both your Claude sessions become
queryable by each other via `/ask <handle> <question>` — your local
Claude calls a synthesis layer that reads their session and returns a
cited answer. Sessions are per-machine, per-session opt-in. Trust is
persistent until either party `/disconnect`s.

---

## 2. System Component Map

```
   ┌───────────── MACHINE A (engineer Alice) ──────────────┐                       ┌───────── MACHINE B (engineer Bob) ──────────┐
   │                                                       │                       │                                             │
   │  ┌──────────────────┐         ┌───────────────────┐   │                       │   ┌──────────────────┐   ┌──────────────┐  │
   │  │  Claude Code TUI │◀────────│ MCP server (stdio)│   │                       │   │  Claude Code TUI │──▶│ MCP server   │  │
   │  │  + slash cmds    │  tool   │ teammate-sync     │   │                       │   │  + slash cmds    │   │ teammate-sync│  │
   │  │  + hooks         │  call   │  - query_teammate │   │                       │   │  + hooks         │   │              │  │
   │  └────────┬─────────┘         │  - dump_teammate  │   │                       │   └────────┬─────────┘   └──────┬───────┘  │
   │           │                   └──────────┬────────┘   │                       │            │                    │          │
   │           │ writes session jsonl         │ HTTPS      │                       │            │                    │ HTTPS    │
   │           ▼                              │            │                       │            ▼                    │          │
   │  ~/.claude/projects/<cwd>/<sid>.jsonl    │            │                       │  ~/.claude/projects/<cwd>/...   │          │
   │           │                              │            │                       │            │                    │          │
   │           │ filesystem-watched by        │            │                       │            │ watched by         │          │
   │           ▼                              │            │                       │            ▼                    │          │
   │  ┌──────────────────┐                    │            │                       │   ┌──────────────────┐          │          │
   │  │  daemon          │────HTTPS────┐      │            │                       │   │  daemon          │──HTTPS──┐│          │
   │  │  (background)    │   uploads   │      │            │                       │   │  (background)    │ uploads ││          │
   │  └──────────────────┘             │      │            │                       │   └──────────────────┘         ││          │
   │           ▲                       │      │            │                       │            ▲                   ││          │
   │  reads    │                       │      │            │                       │  reads     │                   ││          │
   │  ~/.teammate-sync/state/          │      │            │                       │  ~/.teammate-sync/state/       ││          │
   │   - .shared-sessions.json         │      │            │                       │   - .shared-sessions.json      ││          │
   │   - .active-sessions.json         │      │            │                       │   - .active-sessions.json      ││          │
   │  ~/.teammate-sync/auth.json       │      │            │                       │  ~/.teammate-sync/auth.json    ││          │
   │   - github_token, anthropic_key   │      │            │                       │   - github_token, anthropic_key││          │
   │                                   ▼      ▼            │                       │                                ▼▼          │
   └───────────────────────────────────┼──────┼────────────┘                       └────────────────────────────────┼┼──────────┘
                                       │      │                                                                     ││
                                       │      │                                                                     ││
                                       ▼      ▼                                                                     ▼▼
                                ┌───────────────────────────── CLOUD BACKEND (Fly.io, Singapore) ─────────────────────────────┐
                                │                                                                                             │
                                │   FastAPI app — main.py                                                                     │
                                │      Endpoints:                                                                             │
                                │         /v1/me              — verify GitHub OAuth token                                     │
                                │         /v1/teammates       — list org members                                              │
                                │         /v1/connections     — list/request/accept/decline/disconnect                        │
                                │         /v1/files           — list / get / put / delete / purge                             │
                                │         /v1/sessions/share  — per-session ACL writes                                        │
                                │         /v1/dump            — raw session content fetch                                     │
                                │         /v1/dashboard       — aggregated state for dashboard UI                             │
                                │                                                                                             │
                                │   SQLite on Fly Volume (/data/teammate.db) — storage.py                                     │
                                │      Tables:                                                                                │
                                │         files            (workspace_org, owner, path, content, updated_at)                  │
                                │         connections      (workspace_org, requester, recipient, status, requested_at, ...)   │
                                │         session_shares   (workspace_org, session_id, owner, recipient, shared_at)           │
                                │         sync_state       (workspace_org, owner, last_sync_epoch)                            │
                                │                                                                                             │
                                │   Authentication: every request validates Authorization: Bearer <github-oauth-token>        │
                                │   via api.github.com/user (cached). Workspace membership via api.github.com/orgs/X/members. │
                                └─────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. What Lives Where, Per Machine

After `teammate-sync init`:

```
~/.local/bin/teammate-sync              <- pipx-installed binary; everything dispatches through it
~/.local/pipx/venvs/teammate-sync/      <- isolated Python venv (pipx-managed, you never touch it)

~/.teammate-sync/                       <- this tool's per-machine state
  auth.json                             <- mode 0600. Contains:
                                              { "token":         "gho_...",        GitHub OAuth access token
                                                "org":           "Incent-AI",      workspace = GitHub org
                                                "backend_url":   "https://...",
                                                "anthropic_key": "sk-ant-..." }    used by MCP synthesis
  state/                                <- watched by the daemon
    .shared-sessions.json               <- LOCAL gate, never uploaded.
                                           Format: { sessions: [{session_id, cwd, shared_at, recipients[]}] }
                                           Daemon uses this to decide what to upload.
    .active-sessions.json               <- live state, written by hooks, IS uploaded.
                                           Format: { sessions: [{session_id, cwd, last_activity, ...}] }
                                           So teammates can see "this person is alive right now" + which session they're in.
    daemon.pid                          <- written by `teammate-sync up`; cleaned by `down`
    daemon.log                          <- daemon's stdout/stderr, tail with `teammate-sync logs -f`

~/.claude/commands/                     <- Claude Code reads slash commands from here
  connect.md                            <- /connect template; tells Claude to bash `teammate-sync connect $ARGS`
  disconnect.md                         <- /disconnect template
  shared.md                             <- /shared template
  ask.md                                <- /ask template — tells Claude to call the MCP tool directly

~/.claude/settings.json                 <- Claude Code reads hooks from here. We MERGE in three:
  hooks:
    SessionStart  -> teammate-sync hook start        (registers session, prints pending-invites banner)
    PostToolUse   -> teammate-sync hook heartbeat    (updates last_activity)
    SessionEnd    -> teammate-sync hook end          (removes session from .active and .shared registries)

~/.claude.json (Claude Code's own MCP config)        <- We register our MCP server here via `claude mcp add`:
  mcpServers:
    teammate-sync:
      command: /Users/.../bin/teammate-sync
      args:    [mcp-server]

~/.claude/projects/<encoded-cwd>/<sid>.jsonl         <- Claude Code's own session transcripts.
                                                       Daemon watches this tree; uploads only the jsonls
                                                       whose sid is in .shared-sessions.json.
```

The daemon, the MCP server, the dashboard, and the slash-command CLI all
dispatch through the same `~/.local/bin/teammate-sync` binary with
different subcommands.

---

## 4. Backend Data Model + ACL

SQLite at `/data/teammate.db` on the Fly volume.

### `files`
| col | example |
|---|---|
| workspace_org | "Incent-AI" |
| owner_handle | "om-divyatej" |
| path | "-Users-omdivyatej/f4ef20c6-...jsonl" |
| content | (bytes) |
| updated_at | 1758291234.5 |

PRIMARY KEY: (workspace_org, owner_handle, path). Idempotent upserts.

### `connections`
| col | example |
|---|---|
| workspace_org | "Incent-AI" |
| requester_handle | "om-divyatej" |
| recipient_handle | "omdivyatej" |
| status | 'pending' / 'accepted' / 'declined' |
| requested_at | 1758291234.5 |
| decided_at | 1758291300.0 (or NULL) |

PRIMARY KEY: (workspace_org, requester_handle, recipient_handle). One row
PER DIRECTION. Mutual interest auto-accepts: if A requests B and B's row
already requests A, B's row flips to 'accepted'.

### `session_shares`
| col | example |
|---|---|
| workspace_org | "Incent-AI" |
| session_id | "f4ef20c6-b710-..." |
| owner_handle | "om-divyatej" |
| recipient_handle | "omdivyatej" |
| shared_at | 1758291234.5 |

PRIMARY KEY: (workspace_org, session_id, recipient_handle). Idempotent.

### `sync_state`
| col |
|---|
| workspace_org, owner_handle, last_sync_epoch, updated_at |

Per-engineer freshness stamp for "X synced N seconds ago" in queries.

### The ACL — `can_read_file(workspace, requester, owner, path)`

```
1. If requester == owner: allow.   (you can always read your own files)
2. Look up connections between requester and owner — either direction.
   If no row has status='accepted', deny.
3. Path classification:
   a. If path is "<uuid>.jsonl" (session transcript): allow only if
      session_shares contains (workspace, sid, owner, requester).
   b. Else (CLAUDE.md, .active-sessions.json, scratch notes): allow if
      ANY session_shares row exists where owner_handle=owner AND
      recipient_handle=requester. "If they trust me with at least one
      session, they trust me with their project context too."
```

This means: even if you're connected to someone, you only see the
SPECIFIC sessions they explicitly shared with you. Other sessions of
theirs stay private.

---

## 5. Onboarding (`teammate-sync init`)

```
                                ┌────────────────────────────────────────────────────────────────┐
  $ teammate-sync init     ────▶│  1. Local HTTP listener on 127.0.0.1:<random>                  │
                                │     opens browser → backend/auth/github/login                  │
                                └──────────────────────────────┬─────────────────────────────────┘
                                                               │
                                                               ▼
                                ┌────────────────────────────────────────────────────────────────┐
                                │  2. GitHub OAuth flow (in browser) → backend gets code →       │
                                │     exchanges with GitHub for gho_ access_token →              │
                                │     redirects to 127.0.0.1:<port>/callback?access_token=...    │
                                └──────────────────────────────┬─────────────────────────────────┘
                                                               │
                                                               ▼
                                ┌────────────────────────────────────────────────────────────────┐
                                │  3. CLI captures the token, verifies via /v1/me                │
                                │     lists user's GitHub orgs, prompts user to pick workspace   │
                                └──────────────────────────────┬─────────────────────────────────┘
                                                               │
                                                               ▼
                                ┌────────────────────────────────────────────────────────────────┐
                                │  4. Prompts for Anthropic API key (used by MCP server's        │
                                │     synthesis Claude call) — stored alongside in auth.json     │
                                └──────────────────────────────┬─────────────────────────────────┘
                                                               │
                                                               ▼
                                ┌────────────────────────────────────────────────────────────────┐
                                │  5. Writes ~/.teammate-sync/auth.json (mode 0600)              │
                                │  6. Writes 4 slash-command .md files → ~/.claude/commands/     │
                                │  7. Merges hooks into ~/.claude/settings.json                  │
                                │  8. Calls `claude mcp add --scope user teammate-sync ...`      │
                                └────────────────────────────────────────────────────────────────┘
```

After init, the user runs `teammate-sync up` to start the daemon in the
background (writes daemon.pid, redirects logs to daemon.log, returns
immediately so the user's terminal is free).

---

## 6. Share Lifecycle — what happens when Alice types `/connect bob`

```
ALICE'S MACHINE:

  Claude Code      Slash cmd reads ~/.claude/commands/connect.md → tells Claude:
       │           "shell out: teammate-sync connect bob"
       │
       ▼
  bash subprocess  teammate-sync connect bob
       │           CLAUDE_CODE_SESSION_ID env var = "abc-123-..." (inherited from Claude Code)
       │
       ▼
  teammate-sync CLI:
       │  ┌─────────────────────────────────────────────────────────────────┐
       │  │ a. Read ~/.teammate-sync/auth.json → token, org, backend_url    │
       │  │ b. Resolve self via /v1/me → "alice"                            │
       │  │ c. POST /v1/connections/request {org: "Incent-AI", peer: "bob"} │
       │  │    → backend inserts (alice→bob, status='pending') OR detects   │
       │  │      reverse (bob→alice 'pending') and auto-accepts             │
       │  │ d. Append entry to ~/.teammate-sync/state/.shared-sessions.json │
       │  │    { session_id: "abc-123",                                     │
       │  │      cwd: $CLAUDE_PROJECT_DIR,                                  │
       │  │      shared_at: "2026-06-22T...",                               │
       │  │      recipients: ["bob"] }                                      │
       │  │ e. Print "✓ Session abc-123 now shared with: bob" to stdout     │
       │  └─────────────────────────────────────────────────────────────────┘
       │
       ▼
  filesystem write to .shared-sessions.json
       │
       ▼
  DAEMON (background) — watchdog detects the change
       │  ┌─────────────────────────────────────────────────────────────────┐
       │  │ DaemonState.reconcile_shared_sessions():                        │
       │  │   - old_set = previous shared session IDs                       │
       │  │   - new_set = {"abc-123": ["bob"]}                              │
       │  │   - State transition: idle → active                             │
       │  │     prints "share-mode ACTIVATED → uploading"                   │
       │  │   - Calls initial_sync_all() — walks both watched dirs:         │
       │  │     · ~/.teammate-sync/state/  → uploads .active-sessions.json  │
       │  │       (workspace files, no per-session ACL)                     │
       │  │     · ~/.claude/projects/      → uploads only abc-123.jsonl     │
       │  │       (because abc-123 is in shared registry)                   │
       │  │   - Each upload: POST /v1/files {org, path, content_b64,        │
       │  │                   session_id="abc-123", recipients=["bob"]}     │
       │  │     → backend stores file under owner="alice", path=path        │
       │  │     → backend ALSO creates session_shares row                   │
       │  │       ("Incent-AI", "abc-123", "alice", "bob")                  │
       │  └─────────────────────────────────────────────────────────────────┘
       │
       ▼
  Subsequent edits to abc-123.jsonl (Claude is still writing to it as
  Alice keeps typing) trigger watchdog → individual file upload, NOT a
  full re-sync. Recipients/session_id are re-derived from the registry
  and re-sent with each upload, so the ACL stays in sync if Alice
  /connect-s a new recipient mid-session.

BOB'S MACHINE:

  Bob's SessionStart hook on his next claude open fetches /v1/connections
  → sees a pending request from alice → prints a banner in the new
  Claude Code session: "1 pending connection request: /connect alice"

  Bob runs /connect alice:
  → POST /v1/connections/request {peer: "alice"}
  → backend checks reverse (alice→bob, 'pending'), flips to 'accepted'
  → both directions now status='accepted'
  → Bob's daemon ALSO starts uploading Bob's current session jsonl
    with recipients=["alice"], creating session_shares for Bob's session

  Now both sides have:
    connections:    accepted both directions
    session_shares: alice's session→bob, bob's session→alice
  Either side can query the other.

WHEN ALICE'S CLAUDE SESSION ENDS (she closes Claude or types /exit):

  SessionEnd hook fires:
  → updates .active-sessions.json (removes session from active list)
  → ALSO calls share_cli.remove_shared_session("abc-123")
    which removes the session from .shared-sessions.json
  → daemon's watchdog notices: registry is now empty
  → State transition: active → idle
    prints "share-mode DEACTIVATED → cleaning backend"
  → calls purge_owner: DELETE /v1/files/purge?org=Incent-AI
    backend wipes EVERY file owned by alice in that workspace
    (also wipes alice's session_shares rows for this org)
  → Bob can no longer see anything from alice.

  This is the privacy-by-default posture: share state never outlives
  the Claude Code session it belonged to. To re-share, alice runs
  /connect bob in a new Claude session, and her daemon re-uploads.

  Note: connections SURVIVE the session end. Alice and bob are still
  mutually trusted. Only the per-session shares get wiped.
```

---

## 7. How a Query Works — `/ask saketh what did you decide?`

This is the highest-magic moment of the product. Walking it end-to-end.

```
                          (1) USER TYPES THE SLASH COMMAND
  Alice opens her Claude Code session and types:
      /ask saketh what did you decide about the pagination question?

                                       │
                                       ▼

                       (2) CLAUDE CODE LOADS THE SLASH TEMPLATE
  Claude Code reads ~/.claude/commands/ask.md, which contains:
      "The user wants to query teammate-sync. Parse $ARGUMENTS as
       <handle> <question>. Call mcp__teammate-sync__query_teammate_context."
  Then it substitutes $ARGUMENTS = "saketh what did you decide about the
  pagination question?" and sends this prompt to Alice's Claude (the
  main TUI Claude — Sonnet, whatever model she's using).

                                       │
                                       ▼

                            (3) CLAUDE CALLS THE MCP TOOL
  Alice's Claude reads the instructions, parses out:
      handle   = "saketh"
      question = "what did you decide about the pagination question?"
  Then it invokes the MCP tool: mcp__teammate-sync__query_teammate_context

                                       │
                                       ▼

                  (4) TOOL DISPATCH TO THE TEAMMATE-SYNC MCP SERVER
  The MCP server is a subprocess that Claude Code spawned at session
  start (registered via `claude mcp add`). It speaks the MCP protocol
  over stdio. Process is `teammate-sync mcp-server` — Python.
  This process loaded ~/.teammate-sync/auth.json on startup:
      anthropic_client = Anthropic(api_key=auth.anthropic_key)
  Now Claude Code sends the tool call to it.

                                       │
                                       ▼

                    (5) MCP CONSTRUCTS THE BACKEND CLIENT
  Inside the MCP server:
      auth = read_auth()
      backend = HTTPBackend(
          backend_url = auth.backend_url,           # https://teammate-sync-backend.fly.dev
          token       = auth.token,                 # Alice's GitHub OAuth token
          org         = auth.org,                   # "Incent-AI"
          teammate    = "saketh",                   # the QUERIED engineer
      )
  This HTTPBackend instance is a CLIENT that will hit the backend with
  Alice's token in the Authorization header, asking for saketh's files.
  The backend will identify the REQUESTER (alice) from the token and
  apply ACL with owner=saketh.

                                       │
                                       ▼

                       (6) LIST SAKETH'S VISIBLE FILES
  backend.list_keys() makes:
      GET /v1/files?org=Incent-AI&teammate=saketh
      Authorization: Bearer <alice's gho_ token>

  On the backend:
      a. Validate token via api.github.com/user → user["login"] = "alice"
         (cached for performance)
      b. require_workspace_member(alice, Incent-AI) →
         GET api.github.com/orgs/Incent-AI/members/alice → 204 (member)
         (cached)
      c. storage.list_visible_paths("Incent-AI", requester="alice",
                                                owner="saketh"):
         - is_connected("Incent-AI", "alice", "saketh")? Yes, 'accepted'.
         - SELECT session_id FROM session_shares WHERE
              owner_handle="saketh" AND recipient_handle="alice"
           → {"xyz-789-..."}   (the session saketh /connect-ed to alice)
         - SELECT path FROM files WHERE owner_handle="saketh"
           → [".active-sessions.json", "CLAUDE.md",
              "-Users-saketh/xyz-789-...jsonl",
              "-Users-saketh/older-555-...jsonl"]
         - For each: if it's a .jsonl, only include if sid in shared set.
                     Otherwise (non-session file), include because at
                     least one share row exists (any-share-unlocks).
         - Returns: [".active-sessions.json", "CLAUDE.md",
                     "-Users-saketh/xyz-789-...jsonl"]
           (older-555 is filtered out — not shared with alice)

  Backend responds 200 with that file list.

                                       │
                                       ▼

                     (7) MCP READS EACH FILE'S BYTES
  For each path in the list (minus the control files in CONTROL_FILES,
  which are filtered client-side by HTTPBackend.list_keys), the MCP calls:
      backend.get_bytes(path)
      → GET /v1/files/get?org=Incent-AI&teammate=saketh&path=...
      → backend re-checks can_read_file (ACL again) → returns bytes or 403
      → 403 is treated as "not visible, skip" by client

  Additionally, MCP explicitly fetches .active-sessions.json (which the
  CONTROL_FILES filter normally hides from list_keys) — used for the
  "ACTIVE — LIVE NOW" annotation later.

                                       │
                                       ▼

                          (8) ASSEMBLE THE CORPUS
  MCP's load_corpus() builds a single text blob with sections:
      === ACTIVE SESSIONS (live) ===
      (parsed from saketh's .active-sessions.json — which session he's
       in right now, how recently he typed)

      === CLAUDE.md ===
      (his project instructions, decoded utf-8)

      === Session xyz-789 [ACTIVE — LIVE NOW] — last message <ts> ===
      (the session jsonl, rendered as a readable transcript;
       each message stripped of metadata, just role + text)

      === Session older-XYZ [older session #2] — last message <ts> ===
      (if there are more shared sessions, sorted by recency)

      === Note: scratch.md ===
      (any other .md files in his corpus)

  Caps at 400 KB; truncates with a "[corpus truncated]" marker.

                                       │
                                       ▼

                       (9) CALL ANTHROPIC FOR SYNTHESIS
  MCP makes a single Claude API call using Alice's Anthropic key:
      anthropic_client.messages.create(
          model = "claude-sonnet-4-6",
          messages = [{"role": "user", "content": SYNTHESIS_PROMPT.format(
              question = "what did you decide about pagination?",
              corpus   = <the assembled blob>
          )}],
      )
  The synthesis prompt forces Claude to:
      - answer ONLY from the corpus (no hallucination)
      - cite sources (session ID, file name, with [ACTIVE — LIVE NOW] /
        [MOST RECENT] markers so the answer is grounded in time)
      - say "Not found in shared context." if the answer isn't there

                                       │
                                       ▼

                       (10) FRESHNESS STAMP + RETURN
  MCP gets the answer text. Reads saketh's /v1/state freshness:
      GET /v1/state?org=Incent-AI&teammate=saketh
      → { last_sync_epoch: 1758291234.5 }
  Computes age. If > 30 minutes, prefixes a "⚠️ Stale sync warning."
  Otherwise suffixes "— saketh's context as of 12 seconds ago".

  Returns the final string to Alice's Claude as the tool result.

                                       │
                                       ▼

                          (11) ALICE'S CLAUDE PRESENTS IT
  Claude Code shows the tool output. The ask.md slash command tells
  Claude not to add commentary, so Alice sees the cited synthesis
  verbatim:

      [from saketh]
      Saketh decided to use cursor-based pagination over offset because
      offset hit consistency issues at high concurrency. Reasoning in
      session xyz-789 [ACTIVE — LIVE NOW], around 3 min ago: he tried
      offset first, observed dupe rows in a stress test, switched.
      — saketh's context as of 12 seconds ago
```

### What the backend ACL actually prevents

- Alice queries saketh but they're not connected → returns empty corpus → "Not found in shared context."
- Alice queries saketh, they're connected, but saketh hasn't `/connect`-ed alice with any sessions → empty corpus → same.
- Alice queries saketh, connected, saketh shared one session with her — alice sees ONLY that session, not his other open sessions. Per-session ACL working as intended.
- A third party (carol) in the same org queries saketh — no connection, sees nothing.

### Why an Anthropic key is required per machine

The synthesis Claude call (step 9) is initiated by the MCP server process. MCP servers don't currently have a way to ask their host (Claude Code) to do the call on their behalf — Anthropic hasn't shipped MCP sampling yet (open at anthropics/claude-code#1785). Until they do, each user provides their own Anthropic API key, captured during `teammate-sync init` and stored in `auth.json`.

---

## 8. Trust Formation (`/connect`) — Mutual Auto-Accept

```
Time   Alice                          Bob                          Backend rows
T+0    /connect bob                   (nothing yet)                connections:
                                                                    (Incent-AI, alice, bob, 'pending')

T+1    (alice waits)                  (sees pending banner          (unchanged)
                                       on next session start)

T+2    (alice waits)                  /connect alice               connection_request fires.
                                                                   Reverse direction (alice, bob)
                                                                   exists as 'pending'.
                                                                   Backend UPDATEs that row to
                                                                   'accepted' instead of inserting
                                                                   a new one. Result:
                                                                    (Incent-AI, alice, bob, 'accepted')

T+3    Both can now share + query each other freely.
```

Two engineers who independently want to /connect each other never see a
pending state — the second /connect auto-accepts. If only one /connects,
the other sees a banner asking them to /connect back.

`/disconnect` (no args) iterates all accepted connections and deletes
both rows + all session_shares rows between them. `/disconnect <handle>`
does the same for one peer. Local share registry is scrubbed of that
handle.

---

## 9. Daemon Lifecycle — `up` / `down` / `logs`

```
teammate-sync up:
  - Read ~/.teammate-sync/state/daemon.pid
  - If alive: print "already running" and exit
  - Else: Popen(["teammate-sync", "daemon"], start_new_session=True,
                stdout=daemon.log, stderr=STDOUT, stdin=DEVNULL)
  - Write proc.pid → daemon.pid
  - Sleep 1.5s, check alive — if dead, print last 2KB of log and exit 1

teammate-sync down:
  - Read pid file. If not alive: clean stale pid, exit
  - kill(pid, SIGTERM). Wait up to 5s.
  - If still alive: kill(pid, SIGKILL).
  - Unlink pid file.

teammate-sync logs [-f] [-n N]:
  - tail [-f] -n N ~/.teammate-sync/state/daemon.log
```

The daemon itself is the LONG-LIVED process. It:

1. Boots, reads auth.json, resolves own handle via /v1/me, constructs HTTPBackend(teammate=self).
2. Reads .shared-sessions.json into memory (DaemonState).
3. Starts a watchdog Observer over both ~/.teammate-sync/state/ and ~/.claude/projects/.
4. Loops forever responding to filesystem events. Two kinds:
   - `.shared-sessions.json` changed → reconcile_shared_sessions (state transitions: idle ↔ active)
   - Any other watched file changed → if active and file passes per-session filter, upload it

Background-mode quirks (known bugs to fix in 0.3.x):
- `up`'s 1.5s alive check is too short — backend cold-start (Fly auto-stops idle apps) can take 10s, and the daemon dies during init. Fix: poll for a ready marker.
- Daemon doesn't retry transient ReadTimeout from backend during boot. A single hiccup kills it. Fix: exponential backoff.

---

## 10. Dashboard

`teammate-sync dashboard` spawns a localhost HTTP server (random port),
opens the user's browser. Single-page web view that polls `/v1/dashboard`
on the cloud backend every 3 seconds.

Visualizes:
- Accepted connections (with /disconnect inline)
- Pending incoming/outgoing invites
- Your shared sessions (with recipients per session)
- Sessions teammates have shared with you (with "dump" button to fetch raw bytes)

Auth: dashboard reads local auth.json, uses the token for all backend
calls. Binds 127.0.0.1 only, so only-this-machine. No remote hosting.

---

## 11. Failure Modes — debugging cheatsheet

| Symptom | Most likely cause | First thing to check |
|---|---|---|
| `/ask` returns "Not found in shared context" | Other side hasn't /connect-ed back, or their session ended (auto-revoked) | Their `teammate-sync shared` |
| `/ask` returns "backend has no readable files" | Other side's daemon never uploaded — likely died on startup | Other side's `teammate-sync logs -n 30` |
| `/mcp` shows ✗ Failed to connect | MCP entry registered against a binary no longer at that path | `teammate-sync init` to re-register |
| Daemon "alive" per `up` but no uploads | Backend ReadTimeout during init killed daemon AFTER `up`'s 1.5s alive check | `teammate-sync logs` for the traceback |
| Two different PIDs after consecutive `up` | First daemon died silently; second is the live one | `ps aux | grep teammate-sync daemon` |
| Dashboard shows "no recipients — not actually shared" on "From teammates" cards | Cosmetic UI bug — the data API for "from teammates" doesn't carry recipients, dashboard wrongly renders "empty" as "bad" | Ignore — it IS shared (otherwise it wouldn't show up to you) |
| /connect saketh says "already connected" but no content flowing | Connection accepted in backend but daemon never re-ran initial_sync — possibly because session_id wasn't in registry at daemon start | Restart daemon: `down && up` |
| Old shared sessions appearing in /shared with empty recipients | v0.1.x legacy entries (no recipients field) | `/disconnect` to nuke registry, re-`/connect` |

---

## 12. What Each File in `teammate_sync/` Does

```
teammate_sync/
  __init__.py            empty marker
  cli.py                 the entrypoint. argparse + all subcommand dispatch
  auth.py                read/write ~/.teammate-sync/auth.json
  backend.py             StorageBackend abstract + LocalBackend + S3Backend +
                         HTTPBackend (the one in use). All API calls live here.
  daemon.py              the background sync process. Watchdog observers,
                         per-session ACL, reconcile logic.
  server.py              MCP server entrypoint. Exposes query_teammate_context
                         and dump_teammate_context tools. Runs the synthesis call.
  share_cli.py           backs /connect, /disconnect, /shared. Maintains
                         ~/.teammate-sync/state/.shared-sessions.json.
  hook.py                backs SessionStart/PostToolUse/SessionEnd hooks.
                         Maintains .active-sessions.json. SessionStart also
                         fetches pending invites and prints the banner.
  dashboard.py           localhost web UI + JSON proxy to backend.

backend/
  main.py                FastAPI app. All HTTP endpoints.
  storage.py             SQLite layer. All ORM-like queries + the ACL.
  fly.toml + Dockerfile  deployment config
```

---

## 13. What's NOT in the System (yet)

- No team-wide search (Year 2 from the vision doc)
- No cross-AI-tool support (Cursor, Aider, etc. — Year 3)
- No SSO / SAML / enterprise admin (need before serious B2B)
- No audit log of who queried whom (compliance ask, near-term roadmap)
- No quota / cost controls (each user's Anthropic key spends freely)
- No backend HA (single Fly machine in Singapore, SQLite on volume)
- No mobile / web client (must be on the machine with the daemon)
- No daemon supervision (no launchd / systemd plist) — `up` works but if
  the Mac reboots, you re-run `up` manually

---

## 14. Versions Shipped

- 0.1.0 (initial pipx-installable)
- 0.1.1 — relative import bug in backend.py
- 0.1.2 — Anthropic key moves into auth.json (no env var required)
- 0.1.3 — SessionEnd hook auto-unshares
- 0.1.4 — /unshare accepts session-id or --all
- 0.2.0 — directed share + consent flow + dashboard
- 0.3.0 — collapse to 4 slash commands + backgrounded daemon
- 0.3.1 — daemon crash on startup when sessions already shared (TypeError)

Live URLs:
- PyPI: https://pypi.org/project/teammate-sync/
- Source: https://github.com/omdivyatej/teammate-sync
- Landing: https://omdivyatej.github.io/teammate-sync/
- Backend: https://teammate-sync-backend.fly.dev/health
