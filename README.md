# teammate-sync

**Cross-engineer Claude Code context sharing.** Your teammate types `/share`
in their Claude Code session. From your own terminal, your Claude can ask
theirs anything and gets back a cited answer in seconds.

No pasted transcripts. No GitHub pushes. No shared bucket setup.

- Landing: https://omdivyatej.github.io/teammate-sync/
- Source: https://github.com/omdivyatej/teammate-sync

---

## The problem

You and your teammate are both using Claude Code on the same project. You
need to know what they decided about the database schema, or which file
they touched, or why they picked one library over another.

Today, two bad options:

1. **Ping them on Slack and wait.** Forty-five minutes of context-switching.
2. **Get them to send their session transcript.** Their `.jsonl` is 50k
   lines. You paste it into your Claude and immediately blow past the
   200k token context window.

`teammate-sync` is the third option: their Claude becomes a queryable
context server. Your Claude calls it, gets back a cited synthesis, your
context window stays clean.

## Install

```
pipx install teammate-sync
teammate-sync init
teammate-sync daemon
```

That's it. Full setup walkthrough: [INSTALL.md](INSTALL.md).

(Don't have pipx? `brew install pipx && pipx ensurepath`, one-time. We
recommend pipx over plain pip because it isolates the install in its own
venv and puts the binary on your PATH — plain `pip install` fails on
modern macOS Python with PEP 668.)

`init` opens GitHub OAuth (your workspace = your GitHub org, no separate
account), installs the `/share` slash commands into `~/.claude/commands/`,
wires session lifecycle hooks into `~/.claude/settings.json`, and registers
the MCP server. `daemon` runs in the foreground in a terminal you leave open.

## How sharing works

```
/share                              in any Claude Code session
```

Daemon log immediately prints `share-mode ACTIVATED -> uploading`. That
session's `.jsonl` plus your `CLAUDE.md` start syncing to your team's
backend in real time as you work.

```
/unshare                            same session
```

Daemon wipes that session's content from the backend immediately.

```
/shared                             any session
```

Lists which sessions are currently shared.

## How teammates query you

From any Claude Code session in your org, your teammate types:

> Use `mcp__teammate-sync__query_teammate_context` with
> `teammate=<your-github-handle>` and `question="..."`

The MCP server pulls your active-session state + your shared session
transcripts, hands them to a Claude Sonnet synthesis call, and returns a
cited answer. Citations point to specific session IDs and file names so
they can verify.

## Privacy model

Default: **nothing is shared.** The daemon is running but idle.

- **Sessions** are opted in one at a time via `/share`. Unshared sessions
  never leave your machine, no matter how many you have running.
- **Your GitHub OAuth token** sits in `~/.teammate-sync/auth.json` (mode
  0600) and is **never** in the synced tree.
- **Your org** is the unit of trust. Only members of your GitHub org can
  query you. Discovery happens via the GitHub API, no manual invites.

## What syncs where

| Path | What it holds | Synced to cloud? |
| ---- | ------------- | --------------- |
| `~/.teammate-sync/auth.json` | Your GitHub OAuth token | **NEVER** (local only) |
| `~/.teammate-sync/state/.shared-sessions.json` | List of currently-shared session IDs | No (local gate) |
| `~/.teammate-sync/state/.active-sessions.json` | Which sessions are active right now (cwd, last activity) | Yes |
| `~/.claude/projects/<encoded-cwd>/<sid>.jsonl` | Claude Code session transcripts | Only `/share`'d ones |

## CLI

| | |
|---|---|
| `teammate-sync init` | First-run setup. Re-runnable to refresh hooks / slash commands. |
| `teammate-sync daemon` | Run the sync daemon (foreground). |
| `teammate-sync share` / `unshare` / `shared` | Same as the slash commands, from any shell. |
| `teammate-sync whoami` | Show your identity + workspace. |
| `teammate-sync teammates` | List members of your workspace org. |
| `teammate-sync logout` | Delete `~/.teammate-sync/auth.json`. |

## Requirements

- Python 3.11+
- [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI
- `ANTHROPIC_API_KEY` exported in your shell (used by the MCP server's
  synthesis calls)

## Status

Public beta. Backend runs on Fly.io (Singapore region, single-region by
design — synthesis latency is dominated by Anthropic API not by storage).
Per-session opt-in (`/share` / `/unshare`) is the privacy model; there is
no "everything sync" mode and there won't be.

## License

MIT — see [LICENSE](LICENSE) if present, or `pyproject.toml`.
