# teammate-sync (Phase 1 prototype)

A proof-of-concept MCP server that lets your Claude Code query a teammate's
working corpus — their `CLAUDE.md`, session transcripts, and scratch notes —
and get back a **cited synthesis** instead of pasting the whole thing into
your session and blowing past context limits.

## Phase 1 scope (what this is and isn't)

- ✅ Single machine, single directory
- ✅ One MCP tool: `query_teammate_context(question)`
- ✅ Reads `CLAUDE.md`, `*.jsonl` Claude Code sessions, and `*.md` scratch notes
- ✅ Synthesizes a cited answer (~500 words) via a Claude call
- ✅ Ships with synthetic example data so you can test immediately

Not in Phase 1 (intentionally):
- ❌ Sync daemon
- ❌ Hooks for active-session detection
- ❌ Per-session `/share` UX
- ❌ Multi-machine wiring
- ❌ Auth / permissions

The point of Phase 1 is to verify the **synthesis itself returns useful, cited
answers** before we build the surrounding plumbing.

## Setup

Requires Python 3.10+ (confirmed on 3.11).

```bash
cd /Users/omdivyatej/Downloads/Code/teammate-sync
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set your Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Register with Claude Code

Add this block to `~/.claude/settings.json` (merge with any existing
`mcpServers` you already have):

```json
{
  "mcpServers": {
    "teammate-sync": {
      "type": "stdio",
      "command": "/Users/omdivyatej/Downloads/Code/teammate-sync/.venv/bin/python",
      "args": ["/Users/omdivyatej/Downloads/Code/teammate-sync/server.py"],
      "env": {
        "TEAMMATE_CORPUS_DIR": "/Users/omdivyatej/Downloads/Code/teammate-sync/example_data",
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

Replace:
- The corpus dir with your real teammate folder when you have one (Phase 2 wires this automatically)
- The API key with your real one

Restart Claude Code so it picks up the new MCP server.

## Test it

In any Claude Code session, just ask Claude to query the teammate context.
For example:

```
Use the teammate-sync MCP. Ask it: what did the teammate decide about
pagination for the Account migration, and why?
```

Claude should call `query_teammate_context` and return a synthesized,
cited answer drawn from `example_data/`.

### Sample questions (all answerable from the example data)

1. What did the teammate decide about pagination for the Account migration,
   and why?
2. What recursive-trigger gotcha did they hit, and how did they fix it?
3. Which files did they touch during the migration?
4. Did they disable any validation rules? If so, which and why?
5. Are there any open questions or unresolved decisions?
6. What's the policy on rolling back AccountTeamMember? Why?

### What "passing Phase 1" looks like

- Answers are concise (~500 words or less)
- Every factual claim is cited (file or session reference)
- "Not found in shared context" returned when the corpus doesn't have an answer
- No hallucinations — answers only contain what's in the example data

If those four hold, Phase 1 is validated and we move to Phase 2.

## Config

| Env var | Description | Default |
|---|---|---|
| `TEAMMATE_CORPUS_DIR` | Path to the teammate's corpus folder | `./example_data` |
| `ANTHROPIC_API_KEY` | Your Anthropic key (for the synthesis Claude call) | **required** |
| `TEAMMATE_SYNTHESIS_MODEL` | Claude model used for synthesis | `claude-sonnet-4-6` |

## Phase 2 — sync daemon (single-machine simulation)

Phase 2 adds a file-watcher daemon (`daemon.py`) that mirrors a "teammate's"
working directory to a sync target in real time. The MCP server reads from
the synced target and adds a freshness stamp to every answer
(*"teammate's context as of N seconds ago"*).

### Layout

```
~/penguin-sim/.claude/         <- pretend "Saketh's machine" (you edit here)
~/teammate-sync-store/saketh/  <- sync target (daemon writes here, MCP reads here)
```

### Start the daemon

```bash
cd /Users/omdivyatej/Downloads/Code/teammate-sync
.venv/bin/python daemon.py ~/penguin-sim/.claude ~/teammate-sync-store/saketh
```

Leave it running. Watch for `[sync]` log lines.

### Test loop

1. In one terminal: daemon is running (above).
2. In another: edit `~/penguin-sim/.claude/CLAUDE.md` — add a new fact,
   e.g. *"Decided to use bulk API for >100k rows"*.
3. Within ~1 second the daemon logs the modification and syncs it.
4. In a Claude Code session, query the MCP about the new fact — it should
   appear in the answer, and the answer will be tagged with the freshness
   stamp.

### Sample Phase 2 questions

- *"What's the latest decision the teammate added about the bulk API?"*
  (after editing CLAUDE.md to add it)
- *"What did the teammate decide about pagination?"* (from original corpus,
  should now include freshness)

### What "passing Phase 2" looks like

- Edits to the source dir propagate to the target within ~2 seconds
- Every MCP answer includes a freshness stamp
- If the daemon stops, queries continue to work but freshness ages naturally
- After 30 minutes without a sync, queries prepend a stale-sync warning

## Phase 3a — hosted sync via S3

Phase 3a swaps the local sync target for an S3 bucket. The daemon now
uploads to S3 on every file change; the MCP server reads directly from S3
on every query. **Architecture is identical to Phase 2**, with one
indirection layer (`backend.py`) so adding more backends later (R2, GCS)
is a small isolated change.

### What changed code-wise

- New `backend.py` — `StorageBackend` interface plus `LocalBackend` and
  `S3Backend` implementations. Selected via `TEAMMATE_BACKEND` env var.
- `daemon.py` — no longer takes a target dir; backend comes from env.
  Usage: `daemon.py <source-dir>`
- `server.py` — same. Backend constructed at server startup, reused.
- `requirements.txt` — adds `boto3`.

### AWS prerequisites

- AWS account with IAM credentials that can read/write S3
- `~/.aws/credentials` with `[default]` profile (or env var auth)
- `~/.aws/config` with region set
- An S3 bucket you own (this project uses `teammate-sync-omdivyatej` in
  `ap-southeast-1`)

### Env vars for S3 backend

| Env var | Description | Example |
|---|---|---|
| `TEAMMATE_BACKEND` | Backend selector | `s3` |
| `TEAMMATE_S3_BUCKET` | Bucket name | `teammate-sync-omdivyatej` |
| `TEAMMATE_S3_PREFIX` | Key prefix inside bucket | `saketh/` |
| `AWS_REGION` | Bucket region (optional if in `~/.aws/config`) | `ap-southeast-1` |
| `ANTHROPIC_API_KEY` | For the synthesis Claude call | `sk-ant-...` |

AWS credentials are picked up from `~/.aws/credentials` by boto3 — they
are **not** stored in the MCP config (`~/.claude.json`).

### Start the daemon with S3

```bash
cd /Users/omdivyatej/Downloads/Code/teammate-sync
TEAMMATE_BACKEND=s3 \
TEAMMATE_S3_BUCKET=teammate-sync-omdivyatej \
TEAMMATE_S3_PREFIX=saketh/ \
AWS_REGION=ap-southeast-1 \
.venv/bin/python daemon.py ~/penguin-sim/.claude
```

Verify uploads with:

```bash
.venv/bin/python -c "
import boto3
s3 = boto3.client('s3', region_name='ap-southeast-1')
for o in s3.list_objects_v2(Bucket='teammate-sync-omdivyatej', Prefix='saketh/').get('Contents', []):
    print(f\"{o['Key']}  ({o['Size']} bytes)\")
"
```

### Test loop (Phase 3a)

1. Daemon running with S3 backend (above).
2. Edit `~/penguin-sim/.claude/CLAUDE.md` — add a new fact.
3. Daemon log shows `[sync] modified → CLAUDE.md`, S3 upload happens.
4. In a new Claude Code session, query the MCP — answer reflects the new
   fact, freshness stamp shows recent timestamp, no local target dir
   involved.

### What "passing Phase 3a" looks like

- Daemon uploads to S3 on file changes (verify via `aws s3 ls` or boto3)
- MCP server queries S3 directly on each request — no local cache
- End-to-end latency stays roughly the same as Phase 2 (~1-3s sync, ~5s synthesis)
- Switching `TEAMMATE_BACKEND=local` ↔ `TEAMMATE_BACKEND=s3` requires only
  env var changes — no code changes

## Phase 3c — active-session detection via Claude Code hooks

Phase 3c adds *live* session awareness on top of the persistent corpus.
Three Claude Code hooks (`SessionStart`, `PostToolUse`, `SessionEnd`) fire
on the teammate's machine and maintain a small registry at
`.active-sessions.json` inside the synced dir. The daemon picks it up,
mirrors it to S3, and the MCP surfaces it as a separate "ACTIVE SESSIONS"
section in synthesis prompts.

Net effect: the MCP can answer *"what is the teammate doing right now?"*
with live data — not just historical session transcripts.

### What got installed

- `hook.py` — atomic-writing, lock-coordinated handler. Takes one arg
  (`start` / `heartbeat` / `end`) and reads the Claude Code hook JSON
  payload from stdin. Writes to `~/penguin-sim/.claude/.active-sessions.json`
  by default (override with `TEAMMATE_ACTIVE_SESSIONS_FILE`).
- `~/.claude/settings.json` — `hooks` block registering all three events
  to call `hook.py`.
- `backend.py` — `.active-sessions.json` (and `.sync-state.json`, `.lock`,
  `.tmp`) excluded from `list_keys()` so they never get rendered as corpus
  content.
- `server.py` — reads `.active-sessions.json` from the backend, formats
  each entry as one line, prepends an `=== ACTIVE SESSIONS (live) ===`
  section to the synthesis prompt. Synthesis prompt instructs Claude to
  prefer this section for "right now" questions.

### Active-session entry shape

```json
{
  "sessions": [
    {
      "session_id": "abc-123",
      "cwd": "/Users/saketh/penguin",
      "transcript_path": "/Users/saketh/.claude/projects/-Users-saketh-penguin/abc-123.jsonl",
      "started_at": "2026-06-09T...",
      "last_activity": "2026-06-09T...",
      "last_activity_epoch": 1780...
    }
  ],
  "updated_at_epoch": 1780...
}
```

### Test loop (Phase 3c)

1. Daemon running with S3 backend (see Phase 3a — `./start-daemon.sh`).
2. Open a **new** Claude Code session somewhere (e.g. `cd ~/penguin-sim && claude`).
   This fires `SessionStart` → hook writes the session into
   `.active-sessions.json` → daemon syncs to S3 within ~1s.
3. Confirm the file appears:
   ```bash
   cat ~/penguin-sim/.claude/.active-sessions.json
   ```
4. In any Claude Code session, ask:
   > *"Use mcp__teammate-sync__query_teammate_context: what is the teammate
   > currently working on right now? Which project?"*

   Synthesis should return live cwd + time-since-activity from the
   ACTIVE SESSIONS section, not just the historical corpus.

### What "passing Phase 3c" looks like

- New Claude Code sessions register within a second
- `PostToolUse` heartbeats (debounced to one update per 5s by default)
  keep `last_activity` fresh
- `SessionEnd` removes the entry
- MCP queries about "right now" reflect actual live state
- Concurrent sessions don't corrupt the file (fcntl lock + atomic rename)
- Lock and tempfile artifacts never reach S3 (daemon + backend both filter)

### Tuning

| Env var | Default | Purpose |
|---|---|---|
| `TEAMMATE_ACTIVE_SESSIONS_FILE` | `~/penguin-sim/.claude/.active-sessions.json` | Where hook.py writes the registry |
| `HEARTBEAT_MIN_INTERVAL_SECONDS` | `5` | Skip heartbeats more frequent than this (avoid daemon thrash) |

## Phase 3b — two-machine wiring (validated on Lightsail)

A `cloud/` directory holds `launch-saketh-vm.py` + `bootstrap-vm.sh`.
The launcher provisions a Lightsail micro instance (1GB RAM — nano OOMs)
in `ap-southeast-1`, rsyncs the project, installs the Claude Code CLI,
configures hooks, and starts the daemon in a screen session. AWS creds
come from `~/.aws/credentials`; the Anthropic key is read out of the
local MCP config (`~/.claude.json`).

Validated end-to-end on 2026-06-10: a session run on the VM correctly
showed up in queries from the Mac's MCP, including live cwd
(`/home/ubuntu/saketh-workspace`) and the VM's persistent CLAUDE.md
content (OpportunityLineItem migration). No direct Mac↔VM connection at
any point — only S3 in between.

Teardown when done:

```bash
.venv/bin/python -c "import boto3; boto3.client('lightsail', region_name='ap-southeast-1').delete_instance(instanceName='teammate-sync-saketh')"
```

## Phase 3d-A — per-session `/share` UX

Until 3d-A, the daemon would mirror the whole workspace whenever it ran —
fine for sim, but a privacy disaster for real client work. 3d-A flips
the default: **nothing is synced unless a session explicitly opts in.**

### Three slash commands

Defined as markdown in `~/.claude/commands/`:

| Command | Effect |
|---|---|
| `/share` | Adds the current `CLAUDE_CODE_SESSION_ID` to `.shared-sessions.json`. If this was the first shared session, the daemon starts syncing the workspace to S3. |
| `/unshare` | Removes the current session. If it was the last shared session, the daemon **wipes** the team's S3 store. |
| `/shared` | Lists currently shared sessions (which UUIDs, when shared). |

The slash commands invoke `share-cli.py` via the Bash tool. Session id
comes from the `CLAUDE_CODE_SESSION_ID` env var that Claude Code injects
into all subprocesses.

### `.shared-sessions.json` is a local-only permission gate

- Lives in the same workspace dir as `.active-sessions.json`
- Added to `CONTROL_FILES` in `backend.py` so it's never rendered into the corpus
- Never uploaded to the backend by the daemon (`initial_sync` explicitly skips it)
- Daemon reads it locally to decide whether to operate

### Daemon state machine

```
       /share (first)
INACTIVE ──────────────────► ACTIVE
   ▲                            │
   │                            │ file events upload to S3
   │                            │ normally
   │   /unshare (last)          │
   │   wipes S3                 │
   └────────────────────────────┘
```

Transitions are detected by watching `.shared-sessions.json`. The daemon
holds a `_was_active` flag and compares each event against the current
on-disk state — when it transitions:

- `INACTIVE → ACTIVE`: call `initial_sync()` to upload the workspace
- `ACTIVE → INACTIVE`: call `cleanup_backend()` to wipe everything in S3

While `ACTIVE`, file events flow through `_upload` as before. While
`INACTIVE`, all non-state events are silently dropped.

### Test loop (Phase 3d-A)

1. Start daemon — it should log `share-mode INACTIVE — daemon idle`
2. Edit a file in the workspace — should NOT show up in S3
3. In a Claude Code session, type `/share` — daemon logs
   `share-mode ACTIVATED → uploading workspace to backend`, S3 populates
4. Edit another file — daemon logs `[sync] modified → ...`, S3 reflects it
5. `/unshare` — daemon logs `share-mode DEACTIVATED → cleaning backend`,
   S3 wipes
6. `/shared` — lists currently shared sessions in your terminal

### What "passing Phase 3d-A" looks like

- No upload happens until at least one `/share` is issued
- Last `/unshare` triggers a full S3 wipe within a couple seconds
- Multiple `/share`s in different sessions are independent — only the
  *last* `/unshare` triggers cleanup
- Slash command output flows through Claude Code unchanged (the user
  sees the script's stdout verbatim)

## Phase 3d-B+ (not built)

- Install CLI: `teammate-sync init / status / pause` — eliminates the
  manual hook config + daemon launch dance
- Multi-teammate routing: `team.query(teammate="saketh")` — currently
  the MCP is hardcoded to one S3 prefix; for real team use it needs to
  accept the teammate as a parameter and route accordingly
- Real auth + ACLs: per-engineer S3 prefixes with signed URLs, IAM
  policies that scope who can read whose context
- Topic auto-detection: extract first user message from the live session
  transcript so the active-sessions registry includes a one-liner topic
  per session (better synthesis)
- Bigger sync intelligence: only sync session JSONLs for *shared*
  sessions, not every file in the workspace — finer-grained privacy
