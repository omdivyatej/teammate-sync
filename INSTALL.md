# teammate-sync — setup

Three commands, then type `/share <teammate-github-handle>` in any Claude
Code session.

## Prereqs

- Python 3.11+ (`python3.11 --version`)
- `pipx` (`brew install pipx && pipx ensurepath` — one-time, isolates the
  install in its own venv and puts the binary on your PATH)
- `claude` (Claude Code CLI)
- An Anthropic API key from https://console.anthropic.com/settings/keys
  (you'll paste it once during `init`, it gets stored at
  `~/.teammate-sync/auth.json`; no shell env vars needed)

## 1. Install (~15s)

```
pipx install teammate-sync
```

Puts a single `teammate-sync` binary on your PATH. Every other piece of
the system — hooks, MCP server, slash commands, daemon, dashboard —
dispatches through it. No checkout, no virtualenv to keep around.

## 2. Sign in + register (~1 min, interactive)

```
teammate-sync init
```

This one command:
1. Opens your browser to GitHub OAuth (authorize `teammate-sync`; on the
   org-access screen click **Grant** for the org you want as your workspace).
2. Captures the access token, saves it to `~/.teammate-sync/auth.json`
   (mode 0600).
3. Asks you to pick which GitHub org is your workspace.
4. Prompts for your Anthropic API key (stored in the same auth file).
5. Installs all slash commands into `~/.claude/commands/`.
6. Merges session hooks into `~/.claude/settings.json` (preserves
   anything else already there).
7. Registers the MCP server with Claude Code (user scope).

## 3. Start the daemon (~5s, leave running)

```
teammate-sync daemon
```

Output:
```
[sync] daemon starting
[sync] state dir: /Users/YOU/.teammate-sync/state
[sync] watching:  /Users/YOU/.claude/projects
[sync] backend:   HTTPBackend(url=https://teammate-sync-backend.fly.dev, ...)
[sync] share-mode INACTIVE — daemon idle until /share is run
```

Leave this terminal open.

---

## Try it

Restart any open Claude Code sessions so they pick up the hooks + MCP.

**Share with a specific teammate:**

```
/share saketh
```

If you're not yet connected to saketh, this sends them a pending invite.
Daemon log immediately prints `share-mode ACTIVATED → uploading`. Your
session's content uploads with `recipients=[saketh]`.

**They `/accept`:**

In any Claude Code session on their machine, saketh runs `/accept om`.
From now on, your shared content flows to them automatically.

**They query you:**

```
Use mcp__teammate-sync__query_teammate_context with
teammate=<your-handle> and question="..."
```

Or raw (no AI synthesis):

```
Use mcp__teammate-sync__dump_teammate_context with teammate=<your-handle>
```

**Audit:**

```
teammate-sync dashboard
```

Opens a localhost web view showing your sessions + their recipients,
sessions shared with you, pending invitations.

---

## Two-machine setup

Repeat steps 1–3 on the second machine. Same install, same `init`. Pick
the same workspace org on both.

Identity options:
- **Same GitHub account on both** — proves the architecture works across
  machines.
- **Different GitHub accounts in the same org** — proper two-engineer
  flow. Use `/share <other-handle>` to test directed sharing.

## Uninstall

```
pipx uninstall teammate-sync
claude mcp remove teammate-sync --scope user
rm -f ~/.claude/commands/share.md ~/.claude/commands/unshare.md \
      ~/.claude/commands/shared.md ~/.claude/commands/connections.md \
      ~/.claude/commands/accept.md ~/.claude/commands/decline.md \
      ~/.claude/commands/disconnect.md ~/.claude/commands/teammates.md \
      ~/.claude/commands/show.md
# Edit ~/.claude/settings.json — remove the "hooks" block
# Kill the daemon (Ctrl-C in its terminal)
rm -rf ~/.teammate-sync
```

## CLI reference

| | |
|---|---|
| `teammate-sync init` | First-run setup. Re-runnable to refresh hooks / key / slash commands. |
| `teammate-sync daemon` | Run the sync daemon (foreground). |
| `teammate-sync dashboard` | Launch the localhost dashboard. |
| `teammate-sync share <handle> ...` | Share this session with named teammates. |
| `teammate-sync unshare [<sid>\|--all]` | Unshare. |
| `teammate-sync shared` | List shared sessions and their recipients. |
| `teammate-sync connections` | List accepted + pending connections. |
| `teammate-sync accept <handle>` | Accept a pending invite. |
| `teammate-sync decline <handle>` | Decline a pending invite. |
| `teammate-sync disconnect <handle>` | Revoke trust + wipe shares. |
| `teammate-sync show <handle> [<sid>]` | Raw dump of a teammate's session. |
| `teammate-sync teammates` | List all members of your workspace org. |
| `teammate-sync whoami` | Identity check. |
| `teammate-sync logout` | Delete `~/.teammate-sync/auth.json`. |

## Troubleshooting

**`teammate-sync teammates` returns 403 or empty:**
The OAuth app needs org approval. Visit
https://github.com/settings/connections/applications and grant
`teammate-sync` access to your org.

**`/mcp` shows ✗ Failed to connect:**
The MCP entry was registered with a binary that's no longer on PATH.
Re-run `teammate-sync init` to refresh.

**`/share saketh` says "saketh not in your workspace":**
saketh must be in the same GitHub org. Either add them to your org, or
both of you switch to a shared org during `init`.

**Saketh accepts my invite but I still can't query him:**
He needs to run `/share om` to share specific sessions with you. Connection
trust by itself doesn't auto-share existing sessions.

**Daemon hangs on first call:**
Fly backend cold start takes 5-10s. First request slow, subsequent fast.

**Slash commands don't appear:**
Restart Claude Code. Custom commands load at session start.

## What's NOT hardcoded

- Slash commands work in any Claude Code session, any cwd
- MCP server works in any Claude Code session
- Hooks fire on every Claude Code session
- The daemon watches every project on this machine

Per-session, per-recipient ACL means even if you're in 5 orgs and share
one specific session with one specific person, nothing else leaks.
