#!/usr/bin/env bash
# install.sh — one-command setup for a new teammate-sync engineer.
# Prereqs: python3.11+, claude code CLI, ANTHROPIC_API_KEY in env.
#
# What it does:
#   1. Create the project venv + install deps
#   2. Sign in with GitHub (browser OAuth)
#   3. Prompt for workspace dir, create it with a starter CLAUDE.md
#   4. Install /share, /unshare, /shared slash commands
#   5. Merge SessionStart/PostToolUse/SessionEnd hooks into ~/.claude/settings.json
#   6. Register the MCP server with Claude Code at user scope
#   7. Print "now run ./start-daemon.sh"

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo "==> teammate-sync install"
echo "    project: $HERE"
echo

# --- 0. Prereqs ---------------------------------------------------------------

if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: Claude Code CLI not found. Install it first, then re-run."
    echo "  brew install claude  (or use the official installer)"
    exit 1
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY not set. Export it first."
    echo "  export ANTHROPIC_API_KEY=sk-ant-..."
    echo "  Get a key at https://console.anthropic.com/settings/keys"
    exit 1
fi

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: no python3 found."
    exit 1
fi
echo "==> using $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"

# --- 1. Venv + deps -----------------------------------------------------------

if [ ! -d .venv ]; then
    echo "==> creating .venv"
    "$PYTHON_BIN" -m venv .venv
fi
echo "==> installing deps"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# --- 2. GitHub OAuth ----------------------------------------------------------

echo
if [ -f "$HOME/.teammate-sync/auth.json" ]; then
    echo "==> already signed in:"
    ./teammate-sync whoami | sed 's/^/    /'
    read -p "Re-run OAuth? [y/N]: " REDO_OAUTH
    if [[ "${REDO_OAUTH,,}" == y* ]]; then
        ./teammate-sync logout
        ./teammate-sync init
    fi
else
    echo "==> sign in with GitHub (browser will open)"
    ./teammate-sync init
fi

# --- 3. Workspace dir ---------------------------------------------------------

echo
DEFAULT_WS="$HOME/teammate-workspace/.claude"
read -p "Workspace dir [$DEFAULT_WS]: " WS_INPUT
WS="${WS_INPUT:-$DEFAULT_WS}"
WS="${WS/#\~/$HOME}"   # expand leading ~
mkdir -p "$WS"

if [ ! -f "$WS/CLAUDE.md" ]; then
    HOSTNAME_SHORT="$(hostname -s)"
    cat > "$WS/CLAUDE.md" <<EOF
# Workspace on $HOSTNAME_SHORT

Edit this file with whatever project notes you want shared via teammate-sync.
Anything in this directory gets mirrored to the team's cloud store when
you \`/share\` a Claude Code session.

## Notes
EOF
    echo "    wrote starter CLAUDE.md at $WS/CLAUDE.md"
fi

# --- 4. Slash commands --------------------------------------------------------

echo
echo "==> installing slash commands"
./teammate-sync install-commands --workspace "$WS" | sed 's/^/    /'

# --- 5. Hooks (merge into existing ~/.claude/settings.json) -------------------

echo
echo "==> registering Claude Code hooks in ~/.claude/settings.json"

PY_BIN="$HERE/.venv/bin/python"
HOOK_SCRIPT="$HERE/hook.py"
ACTIVE_FILE="$WS/.active-sessions.json"

# Use the project's venv python to do the JSON merge safely (no jq dependency).
PY_BIN_ENV="$PY_BIN" HOOK_SCRIPT_ENV="$HOOK_SCRIPT" ACTIVE_FILE_ENV="$ACTIVE_FILE" \
"$PY_BIN" <<'PYEOF'
import json, os
from pathlib import Path

py_bin = os.environ["PY_BIN_ENV"]
hook = os.environ["HOOK_SCRIPT_ENV"]
active = os.environ["ACTIVE_FILE_ENV"]

settings_path = Path("~/.claude/settings.json").expanduser()
settings_path.parent.mkdir(parents=True, exist_ok=True)

if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        settings = {}
else:
    settings = {}

hooks = settings.setdefault("hooks", {})
for event, op in [("SessionStart", "start"),
                  ("PostToolUse", "heartbeat"),
                  ("SessionEnd", "end")]:
    cmd = f"TEAMMATE_ACTIVE_SESSIONS_FILE={active} {py_bin} {hook} {op}"
    hooks[event] = [{"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}]

settings_path.write_text(json.dumps(settings, indent=2))
print(f"    wrote {settings_path}")
PYEOF

# --- 6. MCP registration -----------------------------------------------------

echo
echo "==> registering MCP server with Claude Code (user scope)"
claude mcp remove teammate-sync --scope user >/dev/null 2>&1 || true
claude mcp add \
    -e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
    --scope user \
    teammate-sync \
    -- "$PY_BIN" "$HERE/server.py" 2>&1 | sed 's/^/    /'

# --- 7. Done -----------------------------------------------------------------

echo
echo "==================================================="
echo "✓ Setup complete."
echo "==================================================="
echo
./teammate-sync whoami | sed 's/^/  /'
echo
echo "Workspace:  $WS"
echo
echo "Next steps:"
echo "  1. Start the daemon:        ./start-daemon.sh $WS"
echo "  2. Restart Claude Code so it picks up the new hooks + MCP"
echo "  3. In a Claude session:     /share"
echo "  4. Teammates' MCPs can now query your context"
