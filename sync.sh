#!/bin/bash
set -euo pipefail

REMOTE="oracle-main"
REMOTE_DIR="/opt/custom-mcp"

RSYNC_EXCLUDE=(
    --exclude='.git'
    --exclude='.venv'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='uv.lock'
    --exclude='.env'
)

echo "==> syncing to ${REMOTE}:${REMOTE_DIR} ..."
rsync -avz --delete "${RSYNC_EXCLUDE[@]}" "$(dirname "$0")/" "${REMOTE}:${REMOTE_DIR}/"

if [[ "${1:-}" == "--rebuild" ]]; then
    echo "==> rebuilding docker ..."
    ssh "$REMOTE" "cd ${REMOTE_DIR} && sudo docker compose up -d --build"
elif [[ "${1:-}" == "--restart" ]]; then
    echo "==> restarting mcp-server ..."
    ssh "$REMOTE" "cd ${REMOTE_DIR} && sudo docker compose restart mcp-server"
fi

echo "==> done"
