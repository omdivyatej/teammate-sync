# teammate-sync — manual setup

A 5–10 minute setup for engineers joining a teammate-sync workspace. After
this, your Claude Code can query any of your teammates' Claude context, and
they can query yours — without anyone pasting transcripts.

> This guide is intentionally manual. Each step shows you exactly what's
> happening. There's an `install.sh` for the one-command path, but doing it
> by hand the first time teaches you what each piece does.

## Prerequisites

Run these checks before you start. Fix any failures before continuing.

| Check | Command | What you need |
| ----- | ------- | ------------- |
| Python | `python3.11 --version` (or 3.12 / 3.13) | 3.11 or newer |
| Claude Code | `claude --version` | any recent version |
| GitHub CLI | `gh --version` | any version |
| GitHub auth | `gh auth status` | logged in |
| Anthropic key | `echo $ANTHROPIC_API_KEY` | a real key (get one at console.anthropic.com/settings/keys) |

If you don't have `claude` or `gh`, install them:

```
brew install claude gh
gh auth login
```

Then export your Anthropic key in the shell you'll use for setup:

```
export ANTHROPIC_API_KEY=sk-ant-...
```

Add it to your `~/.zshrc` (or shell rc) so it persists across terminals.

## 1. Clone the repo

Pick a parent directory that's NOT under `~/Downloads/`, `~/Documents/`,
or `~/Desktop/` — those are TCC-protected on macOS and Claude Code's
subprocesses can't read them. `~/Code/` or `~/dev/` works.

```
mkdir -p ~/Code
cd ~/Code
gh repo clone omdivyatej/teammate-sync
cd teammate-sync
```

## 2. Create the venv and install dependencies

```
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

This installs `mcp`, `anthropic`, `watchdog`, `httpx`. Takes about 30 seconds.

## 3. Sign in with GitHub

```
./teammate-sync init
```

This will:
1. Open your browser to GitHub's OAuth consent screen
2. Wait for you to authorize the `teammate-sync` app
3. Capture the access token back to a local listener
4. Ask you to pick which GitHub organization is your workspace

**Important:** On GitHub's consent screen, scroll down to "Organization
access." For any org you want to use as a teammate-sync workspace, click
**Grant** (if you're admin) or **Request approval** (if you're a member,
then ask your admin). If you skip this, the app won't see your org and
the next step will return an empty list.

Once you pick an org in the terminal, your auth gets saved to
`~/.teammate-sync/auth.json` (mode 0600).

Verify:

```
./teammate-sync whoami
./teammate-sync teammates
```

The first should show your GitHub handle + email + workspace.
The second should list everyone in that org (your "teammates").

## 4. Create your workspace directory

This is where your `CLAUDE.md` and scratch notes will live. Files in this
directory get mirrored to the cloud backend when you `/share` a session.

```
mkdir -p ~/my-workspace/.claude
cat > ~/my-workspace/.claude/CLAUDE.md <<'EOF'
# My workspace

Notes and decisions get recorded here. Anything I add here becomes
queryable by teammates after I /share a session.

## Currently working on

(your project notes go here)
EOF
```

You can pick any path. If you're setting this up on a second laptop for
the same GitHub account, use a different path so it's clear which machine
owns what (e.g. `~/laptop-b-workspace/.claude`).

## 5. Install the slash commands

```
./teammate-sync install-commands --workspace ~/my-workspace/.claude
```

This writes three Markdown files to `~/.claude/commands/`:
- `share.md` → `/share` — mark current session shareable
- `unshare.md` → `/unshare` — revoke + purge from cloud
- `shared.md` → `/shared` — list currently shared sessions

Each one has absolute paths baked in for this specific install + workspace.
If you ever move the project or change workspaces, re-run this command.

## 6. Add hooks to Claude Code's settings

This is the only step you do by editing JSON.

```
open ~/.claude/settings.json
```

If the file doesn't exist yet, create it with `{}`. Inside the top-level
object, add a `"hooks"` block (or merge into an existing one):

```json
{
  "...other settings...": "...",
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "TEAMMATE_ACTIVE_SESSIONS_FILE=/Users/YOU/my-workspace/.claude/.active-sessions.json /Users/YOU/Code/teammate-sync/.venv/bin/python /Users/YOU/Code/teammate-sync/hook.py start",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "TEAMMATE_ACTIVE_SESSIONS_FILE=/Users/YOU/my-workspace/.claude/.active-sessions.json /Users/YOU/Code/teammate-sync/.venv/bin/python /Users/YOU/Code/teammate-sync/hook.py heartbeat",
            "timeout": 5
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "TEAMMATE_ACTIVE_SESSIONS_FILE=/Users/YOU/my-workspace/.claude/.active-sessions.json /Users/YOU/Code/teammate-sync/.venv/bin/python /Users/YOU/Code/teammate-sync/hook.py end",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Replace `/Users/YOU/` (3 places per event) with your actual home path. Run
`echo $HOME` to get it. Also replace `my-workspace` if you picked a
different name in step 4.

Save and close.

## 7. Register the MCP server with Claude Code

```
claude mcp add \
  -e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
  --scope user \
  teammate-sync \
  -- ~/Code/teammate-sync/.venv/bin/python ~/Code/teammate-sync/server.py
```

Verify:

```
claude mcp list
```

You should see:
```
teammate-sync: ... ✓ Connected
```

## 8. Start the daemon

In a dedicated terminal that you'll leave open while you work:

```
cd ~/Code/teammate-sync
./start-daemon.sh ~/my-workspace/.claude
```

You should see:
```
[sync] daemon starting
[sync] workspace: /Users/YOU/my-workspace/.claude
[sync] backend:   HTTPBackend(url=https://teammate-sync-backend.fly.dev, ...)
[sync] share-mode INACTIVE — daemon idle until /share is run
```

The daemon is now watching your workspace but won't upload anything until
you opt in.

## 9. Restart Claude Code

Quit any open Claude Code sessions and start a new one. This is necessary
so they pick up the new hooks and the newly-registered MCP server.

```
claude --dangerously-skip-permissions
```

Inside, run `/mcp` and confirm `teammate-sync` shows ✓.

## 10. Try it

Inside a Claude Code session, type:

```
/share
```

Daemon should immediately log:
```
[sync] share-mode ACTIVATED → uploading workspace + shared sessions
[sync] initial sync complete: N files uploaded
```

Edit your workspace's CLAUDE.md in another terminal:

```
echo "## Decision $(date +%H:%M)" >> ~/my-workspace/.claude/CLAUDE.md
echo "Using PostgreSQL JSONB for user profiles." >> ~/my-workspace/.claude/CLAUDE.md
```

Daemon log:
```
[sync] modified → CLAUDE.md
```

In a fresh Claude Code session, ask:

```
Use mcp__teammate-sync__list_teammates first, then 
mcp__teammate-sync__query_teammate_context with teammate=<your-github-handle>
and question="what's the most recent decision in my workspace?"
```

You should get a cited answer quoting your edit, with a freshness stamp.

Then `/unshare` to revoke. Daemon log shows `cleaning backend`. Cloud goes
empty for your handle.

## Two-machine test

Repeat steps 1–10 on a second machine. Choices:

- **Same GitHub account on both:** both machines publish under your handle.
  Either machine's MCP can query your handle and see the union. Proves
  the architecture works across physical machines but doesn't tell a
  "two engineers" story visually.
- **Different GitHub accounts in the same org:** classic two-engineer
  demo. Machine A queries `query_teammate_context(teammate="account-B-handle", ...)`
  and gets B's content. Most realistic.

Whichever you pick, make sure both machines are members of the same
GitHub organization (your workspace).

## Uninstall

```
./teammate-sync logout                          # delete auth.json
claude mcp remove teammate-sync --scope user    # unregister MCP
rm -f ~/.claude/commands/share.md ~/.claude/commands/unshare.md ~/.claude/commands/shared.md
# Edit ~/.claude/settings.json and remove the "hooks" block
# Kill the daemon (Ctrl-C in its terminal)
rm -rf ~/.teammate-sync
```

## Troubleshooting

**`teammate-sync teammates` returns 403 or empty list:**
The OAuth app needs org approval. Visit
https://github.com/settings/connections/applications and grant
`teammate-sync` access to your org.

**`/mcp` shows ✗ Failed to connect:**
Check that `~/Code/teammate-sync/.venv/bin/python` exists (the venv didn't
get created or got copied between machines). Re-run step 2.

**Daemon logs "Operation not permitted" reading files:**
The project lives under `~/Downloads/` (or similar TCC-protected dir).
Move it to `~/Code/` and update the paths in steps 6 and 7.

**Daemon hangs on first run:**
The Fly backend takes 5-10s to wake from cold start. Be patient on the
first request; subsequent requests are fast.

**Slash commands don't appear in Claude Code:**
Did you restart Claude Code after step 5? Custom commands are loaded at
session start.

**Cross-machine queries return "Not found in shared context":**
Make sure the other machine's daemon is running AND you ran `/share` in
a Claude Code session there. The cloud backend stores nothing for an
engineer until they explicitly share.
