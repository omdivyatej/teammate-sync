# teammate-sync

**Cross-engineer Claude Code context sharing.** Your teammate types
`/share <your-handle>` in their Claude Code session. You `/accept` them.
From your own terminal, your Claude can query theirs and get a cited
answer (or a raw transcript dump) in seconds.

Directed sharing. Per-session ACL. Explicit consent. Local dashboard for
inspection. No pasted transcripts, no GitHub pushes, no shared bucket
setup.

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
teammate-sync daemon
```

Full walkthrough: [INSTALL.md](INSTALL.md).

`init` opens GitHub OAuth (your workspace = your GitHub org), prompts for
your Anthropic API key (used by the MCP synthesis call), installs the
slash commands into `~/.claude/commands/`, wires session hooks, and
registers the MCP server. `daemon` runs in the foreground in a terminal
you leave open.

## How sharing works (v0.2 — directed share + consent)

```
/share saketh              share this session with saketh specifically
                           (sends saketh a pending connection invite if
                           you're not yet connected)

/share saketh marie        share with both at once

/share                     share with everyone you're already connected to

/unshare                   unshare THIS session
/unshare <session-id>      unshare a specific session
/unshare --all             nuke everything

/shared                    audit what you're currently sharing and with whom

/connections               see all connections: accepted + pending in + out
/accept saketh             accept a pending invite from saketh
/decline saketh            decline a pending invite
/disconnect saketh         revoke trust + wipe all shares between you

/teammates                 list everyone in your GitHub org
/show saketh               raw dump (no AI) of what saketh has shared with you
/show saketh <session-id>  raw dump of one specific session
```

When a Claude Code session ends, its share automatically revokes and the
cloud copy is purged. Share state never outlives the conversation.

## How teammates query you

From any Claude Code session, your teammate types:

> Use `mcp__teammate-sync__query_teammate_context` with
> `teammate=<your-github-handle>` and `question="..."`

Or, to skip the AI synthesis and read your transcript directly:

> Use `mcp__teammate-sync__dump_teammate_context` with `teammate=<your-handle>`

## Local dashboard

```
teammate-sync dashboard
```

Launches a localhost web view that shows side-by-side:

- Your own shared sessions and who each is shared with
- Sessions teammates have shared with you (raw transcript previewable)
- Pending invitations (accept/decline inline)
- Accepted connections (disconnect inline)

Useful for verifying you're sharing the right session with the right
person, and for debugging "is this query actually returning the latest
content?" without round-tripping through Claude.

## Privacy model

Default: **nothing is shared.** The daemon runs but is idle.

- **Directed share, not org broadcast.** Sharing is per (session, recipient)
  pair. Other org members can't see what you didn't share with them.
- **Explicit consent.** Recipients must `/accept` your connection before
  content flows. They can `/disconnect` at any time.
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
| `~/.claude/projects/<encoded-cwd>/<sid>.jsonl` | Claude Code session transcripts | Only `/share`'d ones, only to named recipients |

## CLI reference

| | |
|---|---|
| `teammate-sync init` | First-run setup. Re-runnable. |
| `teammate-sync daemon` | Run the sync daemon (foreground). |
| `teammate-sync dashboard` | Open the localhost dashboard. |
| `teammate-sync share <handle>...` | Same as `/share`, from any shell. |
| `teammate-sync unshare [<sid>\|--all]` | Same as `/unshare`. |
| `teammate-sync shared` | Same as `/shared`. |
| `teammate-sync connections` | Same as `/connections`. |
| `teammate-sync accept <handle>` | Same as `/accept`. |
| `teammate-sync decline <handle>` | Same as `/decline`. |
| `teammate-sync disconnect <handle>` | Same as `/disconnect`. |
| `teammate-sync show <handle> [<sid>]` | Raw dump of a teammate's session. |
| `teammate-sync teammates` | List org members. |
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
