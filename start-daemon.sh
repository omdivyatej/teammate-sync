#!/usr/bin/env bash
# Launch the teammate-sync daemon. No arguments needed — it watches
# ~/.teammate-sync/ for share state and ~/.claude/projects/ for session
# jsonls (filtered per Phase 5d, only /share'd sessions upload).
#
# Auth comes from ~/.teammate-sync/auth.json (written by `teammate-sync init`).

set -euo pipefail
cd "$(dirname "$0")"

echo "Starting teammate-sync daemon (cloud backend)"
echo "State dir: $HOME/.teammate-sync/"
echo "Watching:  $HOME/.claude/projects/"
echo "Ctrl+C to stop."
echo

exec .venv/bin/python daemon.py "$@"
