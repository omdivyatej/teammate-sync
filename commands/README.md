# Slash commands

These three commands (`/share`, `/unshare`, `/shared`) get installed into
`~/.claude/commands/` by `teammate-sync install-commands`. They contain
absolute paths to your install + workspace, which is why we don't commit
the rendered `.md` files to the repo — they're generated per-install.

To install (after `teammate-sync init`):

```bash
./teammate-sync install-commands --workspace ~/my-project/.claude
```

This writes:
- `~/.claude/commands/share.md`
- `~/.claude/commands/unshare.md`
- `~/.claude/commands/shared.md`

Each one calls `share-cli.py` with the right Python interpreter and the
right `.shared-sessions.json` path baked in.

## What each command does

- `/share` — adds your current Claude Code session's `$CLAUDE_CODE_SESSION_ID`
  to `.shared-sessions.json`. Once at least one session is shared, the
  daemon starts mirroring your workspace to the cloud backend.

- `/unshare` — removes your current session. If it was the last one,
  the daemon purges your corpus from the team store in a single API call.

- `/shared` — lists currently shared session IDs (with shared-at timestamps)
  so you can audit what's exposed.

## Refreshing

If you ever move the project or change your workspace dir, re-run
`teammate-sync install-commands --workspace …` to regenerate them.
