# teammate-sync

**Cross-engineer Claude Code context sharing.** Your teammate types
`/connect <your-handle>` in their Claude Code session. You `/connect <their-handle>`
back. From your own terminal, you `/ask saketh what did you decide?` and
your Claude returns a cited answer in seconds.

Four slash commands. Per-session, explicit. Local dashboard for inspection.
No pasted transcripts, no GitHub pushes, no shared bucket setup.

- Landing: https://omdivyatej.github.io/teammate-sync/
- Source: https://github.com/omdivyatej/teammate-sync

---

## The problem

You and a teammate are both using Claude Code on the same project. You
need to know what they decided about the database schema, which file they
touched, or why they picked one library over another.

Today, two bad options:

1. **Ping them on Slack and wait** — 45 minutes of context-switching.
2. **Get them to send their session transcript** — 50k lines of `.jsonl`
   pasted into your Claude, instantly blowing your 200k-token context
   window.

`teammate-sync` is the third option: their Claude becomes a queryable
context server. Your Claude calls it, gets back a cited synthesis (or the
raw bytes), your context stays clean.

## Install

```
pipx install teammate-sync
teammate-sync init
teammate-sync up
```

Full walkthrough: [INSTALL.md](INSTALL.md).

`init` opens GitHub OAuth (your workspace = your GitHub org), prompts for
your Anthropic API key (used by the MCP synthesis call), installs the
slash commands into `~/.claude/commands/`, wires session hooks, and
registers the MCP server. `up` starts the sync daemon in the background —
no terminal window to babysit. `down` to stop, `logs` to inspect.

## How sharing works (v0.3 — four slash commands, that's the whole surface)

```
/connect                   list workspace members + who you're connected to
/connect saketh            share THIS session with saketh
                           (also requests trust if you haven't connected yet —
                           when saketh also runs /connect om, the trust links
                           and content flows)
/connect saketh marie      share with multiple in this session

/disconnect                nuke everything: every connection + local shares
/disconnect saketh         remove just saketh

/ask saketh what did X?    query saketh's shared sessions, get a cited answer
/ask saketh,om question    query multiple at once, grouped by handle

/shared                    audit what's in your shareable pool right now
```

Sharing is **per session**. Each new Claude Code session starts un-shared
even if you're connected to teammates — you re-`/connect <handle>` in
every session you want to share. When a session ends, its share
auto-revokes. Trust between people persists until `/disconnect`.

## How teammates query you

From any Claude Code session, your teammate types:

```
/ask <your-handle> <natural-language question>
```

That's it. No MCP-tool-name incantation. The slash command tells Claude
to call the MCP under the hood and present the cited answer.

For raw transcript inspection (no AI synthesis), use `teammate-sync
dashboard` to view content side-by-side in a browser.

## Local dashboard

```
teammate-sync dashboard
```

Launches a localhost web view that shows side-by-side:

- Your own shared sessions and who each is shared with
- Sessions teammates have shared with you (raw transcript previewable)
- Pending connection requests (with `/connect <handle>` hints)
- Accepted connections (disconnect inline)

Useful for verifying you're sharing the right session with the right
person, and for debugging "is this query actually returning the latest
content?" without round-tripping through Claude.

## Privacy model

Default: **nothing is shared.** The daemon runs but is idle.

- **Per-session, per-recipient.** Sharing is per (session, recipient)
  pair. Other org members can't see what you didn't share with them.
- **Per-session explicit.** Every new Claude Code session starts un-shared.
  You re-`/connect <handle>` in each session you want to share.
- **Mutual trust.** Connection is bidirectional — A `/connect`s B, then B
  `/connect`s A, then trust is established. After that, future content
  flows automatically until either side `/disconnect`s.
- **Auto-revoke.** When a Claude Code session ends, its share is wiped.
- **GitHub OAuth token** sits in `~/.teammate-sync/auth.json` (mode 0600)
  and is **never** in the synced tree.
- **Workspace = GitHub org.** Discovery via GitHub API. No new accounts.

## What syncs where

| Path | What it holds | Synced to cloud? |
| ---- | ------------- | --------------- |
| `~/.teammate-sync/auth.json` | GitHub token + Anthropic key + workspace | **NEVER** (local only) |
| `~/.teammate-sync/state/.shared-sessions.json` | Local gate: session → recipients | No (local only) |
| `~/.teammate-sync/state/.active-sessions.json` | Live session state (cwd, last activity) | Yes |
| `~/.claude/projects/<encoded-cwd>/<sid>.jsonl` | Claude Code session transcripts | Only `/connect`'d ones, only to named recipients |

## CLI reference

| | |
|---|---|
| `teammate-sync init` | First-run setup. Re-runnable. |
| `teammate-sync up` | Start the daemon in the background. |
| `teammate-sync down` | Stop the daemon. |
| `teammate-sync logs [-f]` | Tail the daemon log. |
| `teammate-sync dashboard` | Open the localhost dashboard. |
| `teammate-sync connect [<handle> ...]` | Same as `/connect`. No args = list mode. |
| `teammate-sync disconnect [<handle>]` | Same as `/disconnect`. No args = nuke all. |
| `teammate-sync shared` | Same as `/shared`. |
| `teammate-sync daemon` | Foreground daemon (rare; use `up` instead). |
| `teammate-sync teammates` | List org members (debugging). |
| `teammate-sync whoami` | Show your identity + workspace. |
| `teammate-sync logout` | Delete `~/.teammate-sync/auth.json`. |

## Requirements

- Python 3.11+
- [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI
- pipx (`brew install pipx && pipx ensurepath`)

## Status

Public beta. Backend on Fly.io (Singapore, single-region — synthesis
latency is dominated by the Anthropic API, not by storage). Per-session
directed sharing with explicit consent is the privacy model; there is no
"everything sync" mode and there won't be.

## License

MIT — see `pyproject.toml`.
