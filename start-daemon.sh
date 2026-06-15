#!/usr/bin/env bash
# Launch the teammate-sync daemon (cloud backend by default).
#
# Auth comes from ~/.teammate-sync/auth.json (written by `teammate-sync init`).
# No env vars required, no AWS creds anywhere.
#
# Usage:
#   ./start-daemon.sh                 # uses defaults (~/penguin-sim/.claude, ~/.claude/projects)
#   ./start-daemon.sh <workspace>     # override workspace dir
#   ./start-daemon.sh <workspace> <sessions>   # also override sessions dir

set -euo pipefail
cd "$(dirname "$0")"

WORKSPACE_DIR="${1:-$HOME/om-sim/.claude}"
SESSIONS_DIR="${2:-$HOME/.claude/projects}"

mkdir -p "${WORKSPACE_DIR}" "${SESSIONS_DIR}"

echo "Starting teammate-sync daemon (cloud backend)"
echo "Workspace: ${WORKSPACE_DIR}"
echo "Sessions:  ${SESSIONS_DIR}"
echo "Auth:      ${TEAMMATE_AUTH_FILE:-$HOME/.teammate-sync/auth.json}"
echo "Ctrl+C to stop."
echo

exec .venv/bin/python daemon.py "${WORKSPACE_DIR}" "${SESSIONS_DIR}"
