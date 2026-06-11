#!/usr/bin/env bash
# Bootstrap script for the "Saketh" Lightsail VM.
# Runs on the VM after the project tree has been rsynced over.
#
# Expects these env vars to be set when invoked:
#   AWS_ACCESS_KEY_ID
#   AWS_SECRET_ACCESS_KEY
#   ANTHROPIC_API_KEY
#
# Idempotent — safe to re-run.

set -euo pipefail

PROJECT_DIR="$HOME/teammate-sync"
WORKSPACE_DIR="$HOME/saketh-workspace/.claude"
S3_BUCKET="teammate-sync-omdivyatej"
S3_PREFIX="saketh/"
AWS_REGION="ap-southeast-1"

echo "==> [1/7] Installing system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-venv python3-pip \
    curl screen rsync ca-certificates >/dev/null

# Node.js + npm (needed for Claude Code CLI install via npm fallback)
if ! command -v node >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null 2>&1
    sudo apt-get install -y -qq nodejs >/dev/null
fi

echo "==> [2/7] Configuring AWS credentials"
mkdir -p "$HOME/.aws"
chmod 700 "$HOME/.aws"
cat > "$HOME/.aws/credentials" <<EOF
[default]
aws_access_key_id = $AWS_ACCESS_KEY_ID
aws_secret_access_key = $AWS_SECRET_ACCESS_KEY
EOF
cat > "$HOME/.aws/config" <<EOF
[default]
region = $AWS_REGION
EOF
chmod 600 "$HOME/.aws/credentials" "$HOME/.aws/config"

echo "==> [3/7] Setting up Python venv + deps"
cd "$PROJECT_DIR"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> [4/7] Installing Claude Code CLI"
if ! command -v claude >/dev/null 2>&1; then
    # Try official installer first; fall back to npm if needed
    if curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1; then
        echo "    installed via native installer"
    else
        sudo npm install -g @anthropic-ai/claude-code >/dev/null 2>&1
        echo "    installed via npm"
    fi
    # Ensure PATH picks it up
    export PATH="$HOME/.local/bin:$PATH"
fi
# Persist PATH update for future SSH sessions
grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null || \
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"

echo "==> [5/7] Creating Saketh's pretend workspace"
mkdir -p "$WORKSPACE_DIR"
if [ ! -f "$WORKSPACE_DIR/CLAUDE.md" ]; then
    cat > "$WORKSPACE_DIR/CLAUDE.md" <<'EOF'
# Saketh's cloud-VM workspace (Phase 3b end-to-end test)

This workspace lives on a Lightsail VM in ap-southeast-1. It represents
Saketh's "second machine" for proving the cross-machine sync architecture.

## Currently working on

OpportunityLineItem migration from Penguin → Stratus. Applying the same
patterns from Account migration:

- **Pagination:** cursor-based on Id (NOT offset).
- **External ID:** preserving Source_Id__c (downstream lookups depend on it).
- **Bulk API:** using Salesforce Bulk API since this is >100k rows.

## Gotchas discovered so far

- **OpportunityLineItem has no native History trigger** (unlike Account),
  so no recursive-trigger workaround needed here.
- **PricebookEntry FK is required** — must be loaded BEFORE
  OpportunityLineItem or inserts fail. Pre-loaded successfully.

## Open questions for Om

- For the rate-limit decision on `/api/opportunity-line-items`: I lean
  toward its own bucket too, matching the Account precedent. But that's
  three endpoints now with their own buckets — worth revisiting whether
  we just standardize.
- Should we soft-delete OpportunityLineItem on rollback like
  AccountTeamMember, or is hard-delete safe here?

## Files touched

- `api/opportunity_line_items/routes.py` (new)
- `api/opportunity_line_items/serializers.py` (new)
- `migrations/0051_opportunity_line_item_migration.sql` (new)
EOF
fi

echo "==> [6/8] Registering Claude Code hooks"
mkdir -p "$HOME/.claude"
PY_BIN="$PROJECT_DIR/.venv/bin/python"
HOOK_SCRIPT="$PROJECT_DIR/hook.py"
ACTIVE_FILE="$WORKSPACE_DIR/.active-sessions.json"
SHARED_FILE="$WORKSPACE_DIR/.shared-sessions.json"

cat > "$HOME/.claude/settings.json" <<EOF
{
  "env": {
    "ANTHROPIC_API_KEY": "$ANTHROPIC_API_KEY"
  },
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "TEAMMATE_ACTIVE_SESSIONS_FILE=$ACTIVE_FILE $PY_BIN $HOOK_SCRIPT start",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "TEAMMATE_ACTIVE_SESSIONS_FILE=$ACTIVE_FILE $PY_BIN $HOOK_SCRIPT heartbeat",
            "timeout": 5
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "TEAMMATE_ACTIVE_SESSIONS_FILE=$ACTIVE_FILE $PY_BIN $HOOK_SCRIPT end",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
EOF

echo "==> [7/8] Installing /share /unshare /shared slash commands"
mkdir -p "$HOME/.claude/commands"
SHARE_CLI="$PROJECT_DIR/share-cli.py"

cat > "$HOME/.claude/commands/share.md" <<EOF
---
description: Mark this Claude Code session as shareable with teammates via teammate-sync
allowed-tools: Bash($PY_BIN:*)
---

Execute this exact command via the Bash tool and show its full stdout
output to the user verbatim (preserve newlines, do not summarize):

\`\`\`
TEAMMATE_SHARED_SESSIONS_FILE=$SHARED_FILE $PY_BIN $SHARE_CLI share
\`\`\`

The CLAUDE_CODE_SESSION_ID env var is set automatically by Claude Code
for Bash subprocesses — the script reads it from there.

After showing the output, do NOT add commentary. Just show the script's output.
EOF

cat > "$HOME/.claude/commands/unshare.md" <<EOF
---
description: Remove this Claude Code session from teammate-sync sharing
allowed-tools: Bash($PY_BIN:*)
---

Execute this exact command via the Bash tool and show its full stdout
output to the user verbatim:

\`\`\`
TEAMMATE_SHARED_SESSIONS_FILE=$SHARED_FILE $PY_BIN $SHARE_CLI unshare
\`\`\`

After showing the output, do NOT add commentary.
EOF

cat > "$HOME/.claude/commands/shared.md" <<EOF
---
description: List which Claude Code sessions are currently shared with teammates via teammate-sync
allowed-tools: Bash($PY_BIN:*)
---

Execute this exact command via the Bash tool and show its full stdout
output to the user verbatim:

\`\`\`
TEAMMATE_SHARED_SESSIONS_FILE=$SHARED_FILE $PY_BIN $SHARE_CLI list
\`\`\`

After showing the output, do NOT add commentary.
EOF

echo "==> [8/8] Starting daemon in screen session"
# Kill any existing daemon first
pkill -f "daemon.py" 2>/dev/null || true
screen -wipe >/dev/null 2>&1 || true

screen -dmS teammate-daemon bash -c "
  cd $PROJECT_DIR
  export TEAMMATE_BACKEND=s3
  export TEAMMATE_S3_BUCKET=$S3_BUCKET
  export TEAMMATE_S3_PREFIX=$S3_PREFIX
  export AWS_REGION=$AWS_REGION
  exec .venv/bin/python daemon.py $WORKSPACE_DIR 2>&1 | tee /tmp/daemon.log
"

sleep 3
echo ""
echo "=== Daemon log (first lines) ==="
head -20 /tmp/daemon.log 2>/dev/null || echo "(no log yet)"
echo ""
echo "=== Active screens ==="
screen -ls 2>/dev/null || true

echo ""
echo "==> Bootstrap complete."
echo "    Workspace: $WORKSPACE_DIR"
echo "    Daemon:    screen -r teammate-daemon (Ctrl-A then D to detach)"
echo "    Start Claude Code: claude"
