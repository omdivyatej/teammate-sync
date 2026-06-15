# teammate-sync — install + first run

A 5-minute setup for engineers joining a teammate-sync workspace. After this,
your Claude Code can query any of your teammates' Claude context, and they
can query yours — without anyone pasting transcripts.

## Prerequisites

- macOS or Linux
- Python 3.10+
- A GitHub account that's a member of the workspace's GitHub Org
  (your teammate who already set things up will have invited you)
- Claude Code CLI installed (`brew install claude` or via npm)

## Step 1 — Clone + install

```bash
git clone https://github.com/omdivyatej/teammate-sync
cd teammate-sync
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Step 2 — Sign in

```bash
./teammate-sync init
```

A browser tab will open asking you to authorize teammate-sync against your
GitHub account. **Important:** in the "Organization access" section, click
**Grant** (or "Request approval") for the workspace org you want to join.

After you authorize, the browser will say "you can close this tab" and your
terminal will list your visible orgs. Pick yours — that becomes your workspace.

Your auth token is saved at `~/.teammate-sync/auth.json` (mode 600).

## Step 3 — Register the MCP with Claude Code

```bash
claude mcp add \
  --scope user \
  -e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
  teammate-sync \
  -- "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

(Replace `$ANTHROPIC_API_KEY` with your Anthropic key, or set it in your shell first.)

Verify:

```bash
claude mcp list | grep teammate
# → teammate-sync: ...server.py - ✓ Connected
```

## Step 4 — Install the slash commands

The `/share`, `/unshare`, and `/shared` slash commands live in Claude Code's
user-level commands dir. Drop them in:

```bash
mkdir -p ~/.claude/commands
cp commands/*.md ~/.claude/commands/   # if shipped in the repo
```

(If the slash commands aren't in the repo yet, see the README's Phase 3d-A
section for the markdown to copy.)

## Step 5 — Start the daemon

In a dedicated terminal that you keep open while you work:

```bash
./start-daemon.sh
```

It starts in **idle** mode — nothing syncs until you explicitly `/share` a
session. That's the privacy default.

## Step 6 — Use it

In any Claude Code session:

- **Mark a session as shareable:** `/share`
- **Stop sharing (purges your corpus from the team store):** `/unshare`
- **Audit:** `/shared`
- **Discover teammates:** ask Claude *"Use mcp__teammate-sync__list_teammates"*
- **Query a teammate:** ask Claude *"Use mcp__teammate-sync__query_teammate_context with teammate=<their-github-handle> and question=..."*

Or just describe what you want, e.g.: *"My teammate saketh is working on the Account migration. Use teammate-sync to figure out his pagination decisions and any gotchas he hit."* Claude will figure out which tools to call.

## CLI commands

| | |
|---|---|
| `teammate-sync init` | Sign in + pick workspace (run once) |
| `teammate-sync whoami` | Show your identity + workspace + backend |
| `teammate-sync teammates` | List members of your workspace |
| `teammate-sync logout` | Delete `~/.teammate-sync/auth.json` |

## How sharing actually works

- Default: nothing syncs anywhere.
- `/share` in a session → daemon starts mirroring your workspace files (CLAUDE.md, scratch notes, session jsonl) to the cloud backend over HTTPS.
- Backend stores per-workspace, per-engineer. Only members of the same GitHub org can read each other's content.
- `/unshare` → backend wipes everything you own in that workspace. Effective immediately.
- All requests pass through `https://teammate-sync-backend.fly.dev` and require your verified GitHub identity. No AWS keys, no shared bucket, no static credentials.

## Troubleshooting

**"Auth file not found" when running daemon:**
You haven't run `teammate-sync init` yet, or you ran it on a different user account. Re-run init.

**`teammate-sync teammates` returns empty:**
The teammate-sync OAuth app hasn't been approved by your GitHub org. Visit
https://github.com/settings/connections/applications and click "Grant" for your org.

**Daemon log shows "share-mode INACTIVE" but I ran `/share`:**
Check `~/.claude/commands/share.md` exists and the path inside points to your `share-cli.py`. Try running the share command directly:
```bash
CLAUDE_CODE_SESSION_ID=$CLAUDE_CODE_SESSION_ID \
TEAMMATE_SHARED_SESSIONS_FILE=$HOME/my-workspace/.claude/.shared-sessions.json \
  ./.venv/bin/python share-cli.py share
```

**"Stale sync" warning in query results:**
The teammate's daemon hasn't sent a heartbeat in >30 min. They've stopped working or their machine is asleep.

## What gets stored where

- `~/.teammate-sync/auth.json` — your GitHub OAuth token + chosen workspace. Mode 600. Local only.
- `~/<workspace>/.claude/.shared-sessions.json` — list of session IDs you've marked shareable. Local only, never uploaded.
- `~/<workspace>/.claude/.active-sessions.json` — live session registry written by Claude Code hooks. Synced to backend (so teammates can see what you're working on).
- Cloud backend (Fly.io) — your workspace files keyed by `(workspace, your-github-handle, path)`. Wiped on `/unshare`.

## Uninstall

```bash
teammate-sync logout                          # delete auth.json
claude mcp remove teammate-sync --scope user  # unregister MCP
rm -rf ~/.teammate-sync                       # remove all local state
# the daemon stops on Ctrl+C in its terminal
```
