#!/bin/bash
set -euo pipefail

REMOTE="oracle-main"
REMOTE_DIR="/opt/custom-mcp"

RSYNC_EXCLUDE=(
    --exclude='.git'
    --exclude='.venv'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='.env'
)
# uv.lock 必须同步:Dockerfile 用 uv sync --frozen 锁定依赖版本

# 渲染 settings.yml:模板入库,真实文件(含密钥)从 .env 渲染生成,不入库
# (.env 提供 BRAVE_API_KEY 等,占位符语法 ${VAR} 由 envsubst 展开)
# 密钥清单与恢复路径见 CLAUDE.md;备份在 oracle:/root/secrets/custom-mcp.env
ENV_FILE="$(dirname "$0")/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "错误: 找不到 $ENV_FILE,从备份恢复: scp oracle:/root/secrets/custom-mcp.env .env" >&2
    exit 1
fi
for var in BRAVE_API_KEY MARGINALIA_API_KEY MCP_TOKEN; do
    if ! grep -q "^${var}=." "$ENV_FILE"; then
        echo "错误: .env 缺少 ${var},渲染会产出空 key 导致引擎初始化失败" >&2
        exit 1
    fi
done
echo "==> rendering searxng/settings.yml from template ..."
set -a; source "$ENV_FILE"; set +a
envsubst < "$(dirname "$0")/searxng/settings.yml.example" > "$(dirname "$0")/searxng/settings.yml"

echo "==> syncing to ${REMOTE}:${REMOTE_DIR} ..."
rsync -avz --delete "${RSYNC_EXCLUDE[@]}" "$(dirname "$0")/" "${REMOTE}:${REMOTE_DIR}/"

if [[ "${1:-}" == "--rebuild" ]]; then
    echo "==> rebuilding docker ..."
    ssh "$REMOTE" "cd ${REMOTE_DIR} && sudo docker compose up -d --build"
elif [[ "${1:-}" == "--restart" ]]; then
    echo "==> restarting mcp-server ..."
    ssh "$REMOTE" "cd ${REMOTE_DIR} && sudo docker compose restart mcp-server"
elif [[ "${1:-}" == "--searxng" ]]; then
    # 换 .env 里的引擎 key 后用:settings.yml 是挂载文件,内容变化 compose
    # 不可见,必须重启 searxng 才生效
    echo "==> restarting searxng ..."
    ssh "$REMOTE" "cd ${REMOTE_DIR} && sudo docker compose restart searxng"
fi

echo "==> done"
