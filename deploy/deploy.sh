#!/usr/bin/env bash
# 把服务器端同步到云服务器并重建容器。
# 用法：deploy/deploy.sh [user@host]
# 可用环境变量覆盖：DEPLOY_HOST、DEPLOY_KEY、DEPLOY_DIR
set -euo pipefail

HOST="${1:-${DEPLOY_HOST:-deploy@5.223.69.203}}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/jobseeker_deploy}"
DIR="${DEPLOY_DIR:-/opt/mrmeeseeks}"
SSH=(ssh -i "$KEY" "$HOST")

cd "$(dirname "$0")/.."

"${SSH[@]}" "sudo mkdir -p '$DIR' && sudo chown \"\$(id -un):\$(id -gn)\" '$DIR'"
rsync -az --delete -e "ssh -i $KEY" \
  --exclude .env --exclude __pycache__ \
  server/ "$HOST:$DIR/server/"
rsync -az -e "ssh -i $KEY" Dockerfile .dockerignore compose.yml "$HOST:$DIR/"

"${SSH[@]}" "cd '$DIR' && if [ ! -s .env ]; then
  cp server/.env.example .env && chmod 600 .env
  echo '已创建 $DIR/.env，请先填写 DISCORD_TOKEN 再重新运行本脚本。'
  exit 1
fi
docker compose up -d --build && sleep 8 && docker compose ps && (docker compose logs --since 15s | grep -v 'voice will NOT' || true)"
