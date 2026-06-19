# teammate-sync — setup

Three commands. Then type `/connect <teammate>` in any Claude Code session.

## Prereqs

- Python 3.11+ (`python3.11 --version`)
- `pipx` (`brew install pipx && pipx ensurepath` — one-time)
- `claude` (Claude Code CLI)
- An Anthropic API key from https://console.anthropic.com/settings/keys
  (you'll paste it once during `init`; stored at `~/.teammate-sync/auth.json`,
  never in your shell env)

## 1. Install

```
pipx install teammate-sync
```

## 2. Sign in (interactive)

```
teammate-sync init
```

This one command:
1. Opens your browser to GitHub OAuth (authorize `teammate-sync`; on the
   org-access screen click **Grant** for the org you want as your workspace).
2. Captures the access token to `~/.teammate-sync/auth.json` (mode 0600).
3. Asks you to pick which GitHub org is your workspace.
4. Prompts for your Anthropic API key.
5. Installs the v0.3 slash commands into `~/.claude/commands/`.
6. Merges session hooks into `~/.claude/settings.json`.
7. Registers the MCP server with Claude Code (user scope).

## 3. Start the daemon — backgrounded

```
teammate-sync up
```

Output:
```
✓ daemon up (pid 12345)
  logs:      teammate-sync logs
  dashboard: teammate-sync dashboard
  stop:      teammate-sync down
```

No terminal-window to babysit. The daemon detaches and runs in the
background, logging to `~/.teammate-sync/state/daemon.log`. To stop it:
`teammate-sync down`. To inspect: `teammate-sync logs -f`.

---

## Try it

Restart any open Claude Code sessions so they pick up the hooks + MCP.

**Share with a specific teammate:**

```
/connect saketh
```

If you're not yet connected to saketh, this also sends them a pending
trust request. When saketh runs `/connect om` back in their own session,
trust is established and your shared session flows to them.

**See workspace status:**

```
/connect
```

Lists all org members and where each one stands: connected, you invited
them, they invited you, or no relationship yet.

**Query a teammate:**

```
/ask saketh what did you decide about pagination?
/ask saketh,marie what's the schema look like?
```

`/ask` calls the MCP under the hood. Multiple comma-separated handles get
queried in parallel and the answers presented grouped by person.

**Audit:**

```
/shared                       # in Claude Code — what's currently shareable
teammate-sync dashboard       # browser view of everything: yours + theirs
```

**Disconnect:**

```
/disconnect saketh            # remove just saketh
/disconnect                   # nuclear: every trust relationship, gone
```

When a Claude Code session ends, its shares auto-revoke. Trust between
people persists until `/disconnect`.

---

## Two-machine setup

Repeat steps 1–3 on the second machine. Same install, same `init`. Pick
the same workspace org on both.

Identity options:
- **Same GitHub account on both** — proves the architecture works across
  machines.
- **Different GitHub accounts in the same org** — proper two-engineer
  flow.

## Uninstall

```
teammate-sync down                        # stop the daemon
pipx uninstall teammate-sync
claude mcp remove teammate-sync --scope user
rm -f ~/.claude/commands/connect.md ~/.claude/commands/disconnect.md \
      ~/.claude/commands/shared.md ~/.claude/commands/ask.md
# Edit ~/.claude/settings.json — remove the "hooks" block
rm -rf ~/.teammate-sync
```

## CLI reference

| | |
|---|---|
| `teammate-sync init` | First-run setup. Re-runnable to refresh hooks / key / slash commands. |
| `teammate-sync up` | Start the daemon in the background. |
| `teammate-sync down` | Stop the daemon. |
| `teammate-sync logs [-f]` | Tail the daemon log. |
| `teammate-sync dashboard` | Launch the localhost dashboard. |
| `teammate-sync connect [<handle> ...]` | List status, or share this session. |
| `teammate-sync disconnect [<handle>]` | Disconnect one, or all. |
| `teammate-sync shared` | List shared sessions + recipients. |
| `teammate-sync daemon` | Foreground daemon (rare; use `up` instead). |
| `teammate-sync teammates` | List all members of your workspace org. |
| `teammate-sync whoami` | Identity check. |
| `teammate-sync logout` | Delete `~/.teammate-sync/auth.json`. |

## Troubleshooting

**`/connect` says "teammate not in your workspace":**
That teammate isn't in the same GitHub org. Either add them, or both
switch to a shared org during `init`.

**Daemon won't stay running:**
Check `teammate-sync logs` for the error. Common cause: stale auth or
network blip during startup. `teammate-sync down && teammate-sync up`
to bounce.

**`/mcp` shows ✗ Failed to connect:**
The MCP entry was registered with a binary that's no longer on PATH.
Re-run `teammate-sync init`.

**The other person `/connect`-ed me but I still can't query them:**
They need to `/connect <your-handle>` in a specific session for THAT
session's content to flow. Trust alone doesn't auto-share existing
sessions. Per-session opt-in is the privacy posture.

**Daemon hangs on first call:**
Fly backend cold start takes 5–10s. First request slow, subsequent fast.

**Slash commands don't appear:**
Restart Claude Code. Custom commands load at session start.

## Upgrading from v0.2

Old slash commands `/share`, `/unshare`, `/connections`, `/accept`,
`/decline`, `/teammates`, `/show` are retired. `teammate-sync
install-commands` removes their `.md` files automatically. The four
remaining commands (`/connect`, `/disconnect`, `/shared`, `/ask`) cover
everything the old set did.

Backend doesn't change between v0.2 and v0.3 — your existing connections
and trust relationships carry over.
