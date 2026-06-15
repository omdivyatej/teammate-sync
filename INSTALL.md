# teammate-sync — setup

Three steps. After this, your Claude Code can query any of your teammates'
Claude Code context, and they can query yours — without anyone pasting
transcripts.

## Prereqs

- Python 3.11+
- `claude` (Claude Code CLI)
- `gh` (GitHub CLI), already logged in via `gh auth login`
- `ANTHROPIC_API_KEY` exported in your shell

## 1. Clone + install (~30s)

Pick any parent directory that is NOT `~/Downloads/`, `~/Documents/`, or
`~/Desktop/` (those are TCC-protected on macOS and Claude Code's
subprocesses can't read them).

```
mkdir -p ~/Code && cd ~/Code
gh repo clone omdivyatej/teammate-sync
cd teammate-sync
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 2. Initialize (~1 min, interactive)

```
./teammate-sync init
```

This single command:
1. Opens your browser to GitHub OAuth (authorize `teammate-sync`; on the
   org-access screen click **Grant** for the org you want as your workspace).
2. Captures the access token back via a local listener and saves it to
   `~/.teammate-sync/auth.json` (mode 0600).
3. Asks you to pick which GitHub org is your workspace.
4. Asks for a workspace directory (default `~/teammate-workspace/.claude`);
   creates it and writes a starter `CLAUDE.md` if missing.
5. Installs `/share`, `/unshare`, `/shared` slash commands into
   `~/.claude/commands/` with paths resolved for this install.
6. Merges `SessionStart`, `PostToolUse`, `SessionEnd` hooks into
   `~/.claude/settings.json` (preserves anything else already there).
7. Registers the MCP server with Claude Code (user scope) using your
   `ANTHROPIC_API_KEY` from the shell.

When done, it prints the exact command for step 3.

## 3. Start the daemon (~5s, leave running)

```
./start-daemon.sh ~/teammate-workspace/.claude
```

(Substitute the workspace dir you picked in step 2.)

You should see:
```
[sync] daemon starting
[sync] workspace: ...
[sync] backend:   HTTPBackend(url=https://teammate-sync-backend.fly.dev, ...)
[sync] share-mode INACTIVE — daemon idle until /share is run
```

Leave this terminal open while you work.

---

## Try it

Open a fresh Claude Code session (existing ones won't have the new hooks
or MCP loaded):

```
claude --dangerously-skip-permissions
```

Inside, type `/share`. The daemon log immediately prints
`share-mode ACTIVATED → uploading workspace`.

Edit your workspace's `CLAUDE.md`:

```
echo "## Decision $(date +%H:%M)" >> ~/teammate-workspace/.claude/CLAUDE.md
echo "Going with PostgreSQL JSONB instead of separate columns." >> ~/teammate-workspace/.claude/CLAUDE.md
```

In another Claude Code session, ask:

```
Use mcp__teammate-sync__query_teammate_context with teammate=<your-github-handle>
and question="what's the most recent decision I added?"
```

You should get a cited synthesis quoting your edit, with a freshness stamp
in seconds.

Then `/unshare` — daemon logs `cleaning backend`, the cloud copy is wiped.

## Two-machine setup

Repeat steps 1–3 on the second machine. Same `init` flow. Picking a
different workspace dir (e.g. `~/laptop-b-workspace/.claude`) keeps the
two machines visually distinct.

Two identity choices:
- **Same GitHub account on both** — both machines publish under your
  handle. Proves architecture across physical machines.
- **Different GitHub account in the same org** — proper two-engineer demo.
  Each machine's MCP can query the other's content by handle.

## Uninstall

```
./teammate-sync logout                           # deletes auth.json
claude mcp remove teammate-sync --scope user     # unregister MCP
rm -f ~/.claude/commands/share.md ~/.claude/commands/unshare.md ~/.claude/commands/shared.md
# Edit ~/.claude/settings.json by hand and remove the "hooks" block
# Kill the daemon (Ctrl-C in its terminal)
rm -rf ~/.teammate-sync
```

## CLI reference

| | |
|---|---|
| `./teammate-sync init` | Full first-run setup (steps 4-7 above). Idempotent — safe to re-run if you change workspace. |
| `./teammate-sync whoami` | Show your identity + workspace + auth file. |
| `./teammate-sync teammates` | List all members of your workspace org. |
| `./teammate-sync install-commands --workspace DIR` | Re-install slash commands pointing at a different workspace dir. |
| `./teammate-sync logout` | Delete `~/.teammate-sync/auth.json`. |

## Troubleshooting

**`teammate-sync teammates` returns 403 or empty:**
The OAuth app needs org approval. Visit
https://github.com/settings/connections/applications and grant
`teammate-sync` access to your org.

**`/mcp` shows ✗ Failed to connect:**
Project lives somewhere Claude Code can't read (TCC). Move it out of
`~/Downloads/`, `~/Documents/`, `~/Desktop/`.

**Daemon hangs on first call:**
Fly backend cold start takes 5-10s. First request slow, subsequent fast.

**Slash commands don't appear:**
Restart Claude Code. Custom commands load at session start.

**Cross-machine queries return "Not found":**
The other machine's daemon must be running AND `/share` must have been
run there. Default is nothing-shared.

## Note on the workspace model

Today, `teammate-sync` ties your install to a single workspace directory
chosen at `init` time. Slash commands work in any Claude Code session
regardless of cwd — but they always reference that one configured workspace.

Per-project workspaces (auto-detect from cwd, separate shared-state per
project) is a planned feature. For the current product, one workspace
per engineer is the assumption.
