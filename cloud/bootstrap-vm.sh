#!/usr/bin/env bash
# Bootstrap script for a Lightsail VM acting as a second engineer for the
# end-to-end backend-mediated test. NO AWS, NO S3 — talks to the cloud
# backend over HTTPS using a GitHub OAuth token.
#
# Expects these env vars (set by the launcher when invoking via SSH):
#   GITHUB_TOKEN         GitHub OAuth access token (gho_...)
#   GITHUB_ORG           Workspace handle (a GitHub org you're in)
#   BACKEND_URL          e.g. https://teammate-sync-backend.fly.dev
#   ANTHROPIC_API_KEY    For the MCP server's synthesis calls
#
# Idempotent — safe to re-run.

set -euo pipefail

PROJECT_DIR="$HOME/teammate-sync"
WORKSPACE_DIR="$HOME/saketh-workspace/.claude"
BACKEND_URL="${BACKEND_URL:-https://teammate-sync-backend.fly.dev}"

echo "==> [1/8] Installing system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-venv python3-pip \
    curl screen rsync ca-certificates >/dev/null

if ! command -v node >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null 2>&1
    sudo apt-get install -y -qq nodejs >/dev/null
fi

echo "==> [2/8] Installing Claude Code CLI"
if ! command -v claude >/dev/null 2>&1; then
    if curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1; then
        echo "    installed via native installer"
    else
        sudo npm install -g @anthropic-ai/claude-code >/dev/null 2>&1
        echo "    installed via npm"
    fi
fi
grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null || \
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
export PATH="$HOME/.local/bin:$PATH"

echo "==> [3/8] Setting up Python venv + deps"
cd "$PROJECT_DIR"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> [4/8] Writing teammate-sync auth (skipping browser OAuth, using the token the launcher passed in)"
mkdir -p "$HOME/.teammate-sync"
chmod 700 "$HOME/.teammate-sync"
cat > "$HOME/.teammate-sync/auth.json" <<EOF
{
  "token": "$GITHUB_TOKEN",
  "org": "$GITHUB_ORG",
  "backend_url": "$BACKEND_URL"
}
EOF
chmod 600 "$HOME/.teammate-sync/auth.json"

echo "    verifying with /v1/me..."
.venv/bin/python -c "
from auth import read_auth
import httpx
a = read_auth()
r = httpx.get(f\"{a['backend_url']}/v1/me\", headers={'Authorization': f'Bearer {a[\"token\"]}'}, timeout=15)
r.raise_for_status()
print(f'    authenticated as {r.json()[\"github_handle\"]} in workspace {a[\"org\"]}')
"

echo "==> [5/8] Creating workspace"
mkdir -p "$WORKSPACE_DIR"
if [ ! -f "$WORKSPACE_DIR/CLAUDE.md" ]; then
    cat > "$WORKSPACE_DIR/CLAUDE.md" <<'EOF'
# Saketh's cloud-VM workspace

Lives on a Lightsail VM. Represents Saketh's "second machine" for the
end-to-end backend-mediated test.

## Currently working on

OpportunityLineItem migration from Penguin → Stratus. Applying patterns
from the earlier Account migration:

- Pagination: cursor-based on Id (NOT offset).
- External ID: preserving Source_Id__c.
- Bulk API: using Salesforce Bulk API since this is >100k rows.

## Gotchas

- OpportunityLineItem has no native History trigger, so no recursive-
  trigger workaround needed here.
- PricebookEntry FK is required — must be loaded BEFORE
  OpportunityLineItem or inserts fail.
EOF
fi

echo "==> [6/8] Installing slash commands"
.venv/bin/python cli.py install-commands --workspace "$WORKSPACE_DIR" >/dev/null

echo "==> [7/8] Registering Claude Code hooks + MCP"
PY_BIN="$PROJECT_DIR/.venv/bin/python"
HOOK_SCRIPT="$PROJECT_DIR/hook.py"
ACTIVE_FILE="$WORKSPACE_DIR/.active-sessions.json"

mkdir -p "$HOME/.claude"
cat > "$HOME/.claude/settings.json" <<EOF
{
  "env": {
    "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY:-}"
  },
  "hooks": {
    "SessionStart": [{"hooks": [{"type":"command","command":"TEAMMATE_ACTIVE_SESSIONS_FILE=$ACTIVE_FILE $PY_BIN $HOOK_SCRIPT start","timeout":5}]}],
    "PostToolUse":  [{"hooks": [{"type":"command","command":"TEAMMATE_ACTIVE_SESSIONS_FILE=$ACTIVE_FILE $PY_BIN $HOOK_SCRIPT heartbeat","timeout":5}]}],
    "SessionEnd":   [{"hooks": [{"type":"command","command":"TEAMMATE_ACTIVE_SESSIONS_FILE=$ACTIVE_FILE $PY_BIN $HOOK_SCRIPT end","timeout":5}]}]
  }
}
EOF

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    claude mcp remove teammate-sync --scope user 2>/dev/null || true
    claude mcp add \
        -e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
        --scope user \
        teammate-sync \
        -- "$PY_BIN" "$PROJECT_DIR/server.py" 2>&1 | tail -2
fi

echo "==> [8/8] Starting daemon in screen session"
pkill -f "daemon.py" 2>/dev/null || true
screen -wipe >/dev/null 2>&1 || true

SESSIONS_DIR="$HOME/.claude/projects"
mkdir -p "$SESSIONS_DIR"

screen -dmS teammate-daemon bash -c "
  cd $PROJECT_DIR
  exec .venv/bin/python daemon.py $WORKSPACE_DIR $SESSIONS_DIR 2>&1 | tee /tmp/daemon.log
"

sleep 3
echo ""
echo "=== Daemon log (first lines) ==="
head -8 /tmp/daemon.log 2>/dev/null || echo "(no log yet)"
echo ""
echo "==> Bootstrap complete."
echo "    Workspace:    $WORKSPACE_DIR"
echo "    Daemon log:   /tmp/daemon.log (or: screen -r teammate-daemon)"
echo "    Start Claude: cd ~/saketh-workspace && claude"
echo "    Then:         /share inside the Claude session"
