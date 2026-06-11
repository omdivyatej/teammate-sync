#!/usr/bin/env bash
# Launch the teammate-sync daemon with the S3 backend pointed at our test bucket.
# Run from anywhere: bash start-daemon.sh (or ./start-daemon.sh after chmod +x).

set -euo pipefail
cd "$(dirname "$0")"

export TEAMMATE_BACKEND=s3
export TEAMMATE_S3_BUCKET=teammate-sync-omdivyatej
export TEAMMATE_S3_PREFIX=saketh/
export AWS_REGION=ap-southeast-1

SOURCE_DIR="${1:-$HOME/penguin-sim/.claude}"

echo "Starting daemon with S3 backend → s3://${TEAMMATE_S3_BUCKET}/${TEAMMATE_S3_PREFIX}"
echo "Watching: ${SOURCE_DIR}"
echo "Ctrl+C to stop."
echo

exec .venv/bin/python daemon.py "${SOURCE_DIR}"
