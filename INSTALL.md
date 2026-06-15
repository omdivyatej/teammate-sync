# teammate-sync — setup

Two steps. After this, type `/share` from any Claude Code session in any
directory, and your context becomes queryable by your teammates.

## Prereqs

- Python 3.11+
- `claude` (Claude Code CLI)
- `gh` (GitHub CLI), logged in via `gh auth login`
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

This one command:
1. Opens your browser to GitHub OAuth (authorize `teammate-sync`; on the
   org-access screen click **Grant** for the org you want as your workspace).
2. Captures the access token via a local listener, saves it to
   `~/.teammate-sync/auth.json` (mode 0600).
3. Asks you to pick which GitHub org is your team.
4. Installs `/share`, `/unshare`, `/shared` slash commands into
   `~/.claude/commands/`.
5. Merges `SessionStart` + `PostToolUse` + `SessionEnd` hooks into
   `~/.claude/settings.json` (preserves anything else already there).
6. Registers the MCP server with Claude Code (user scope) using your
   `ANTHROPIC_API_KEY` from the shell.

When it finishes, it prints the daemon command.

## 3. Start the daemon (~5s, leave running)

```
./start-daemon.sh
```

You should see:
```
[sync] daemon starting
[sync] state dir: /Users/YOU/.teammate-sync/state
[sync] watching:  /Users/YOU/.claude/projects
[sync] backend:   HTTPBackend(url=https://teammate-sync-backend.fly.dev, ...)
[sync] share-mode INACTIVE — daemon idle until /share is run
```

No args needed. The daemon watches every Claude Code project on this machine
but only uploads sessions you explicitly `/share`. Leave the terminal open.

---

## Try it

Restart any open Claude Code sessions (so they pick up the new hooks + MCP),
then in any new Claude session, anywhere:

```
/share
```

Daemon log immediately prints `share-mode ACTIVATED → uploading`.

Now do some work in that Claude session — ask it questions, have it edit files,
whatever. Every turn writes to the session jsonl. Every write triggers the
daemon to push the latest version to the cloud.

In another Claude Code session (same machine or a teammate's), ask:

```
Use mcp__teammate-sync__query_teammate_context with teammate=<your-github-handle>
and question="what am I currently working on?"
```

You'll get a cited synthesis of your session's recent activity. The
freshness stamp shows seconds-old.

`/unshare` to revoke — daemon wipes your stuff from the cloud immediately.

## What's where

| Path | What it holds | Synced to cloud? |
| ---- | ------------- | --------------- |
| `~/.teammate-sync/auth.json` | Your GitHub OAuth token | **NEVER** (local only) |
| `~/.teammate-sync/state/.shared-sessions.json` | List of currently-shared session IDs | No (local gate) |
| `~/.teammate-sync/state/.active-sessions.json` | Live state: which sessions are active, cwd, last activity | Yes |
| `~/.claude/projects/<encoded-cwd>/<sid>.jsonl` | Claude Code's session transcripts | Only `/share`'d ones |
| `~/.claude/settings.json` | Hook config (we add a `hooks` block) | No (local) |
| `~/.claude/commands/{share,unshare,shared}.md` | Slash commands | No (local) |

## Two-machine setup

Repeat steps 1-3 on the second machine. Same flow.

Identity choices:
- **Same GitHub account on both** — proves the architecture works across
  machines.
- **Different GitHub accounts in the same org** — proper two-engineer demo.
  Each machine's MCP can query the other's content by handle.

## Uninstall

```
./teammate-sync logout                           # deletes ~/.teammate-sync/auth.json
claude mcp remove teammate-sync --scope user     # unregister MCP
rm -f ~/.claude/commands/share.md ~/.claude/commands/unshare.md ~/.claude/commands/shared.md
# Edit ~/.claude/settings.json — remove the "hooks" block
# Kill the daemon (Ctrl-C in its terminal)
rm -rf ~/.teammate-sync
```

## CLI reference

| | |
|---|---|
| `./teammate-sync init` | First-run setup (steps in section 2). Re-runnable to refresh hooks / slash commands. |
| `./teammate-sync whoami` | Show your identity + workspace. |
| `./teammate-sync teammates` | List all members of your workspace org. |
| `./teammate-sync install-commands` | Re-install slash commands (e.g. after moving the project). |
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

## What's NOT hardcoded

- `/share` works in any Claude Code session, any cwd
- The MCP server works in any Claude Code session
- Hooks fire on every Claude Code session
- The daemon watches every project on this machine

Per-session sharing: each `/share` records the session's `cwd` alongside the
session id. The daemon's per-session filter (Phase 5d) means only the
sessions you've explicitly shared upload — your unrelated client work
stays local forever.
