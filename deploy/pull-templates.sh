#!/usr/bin/env bash
# 把服务器上在 Discord 里标注的模板拉回仓库 server/data/templates/，之后自行决定是否提交。
# 用法：deploy/pull-templates.sh [user@host]（环境变量同 deploy.sh）
set -euo pipefail

HOST="${1:-${DEPLOY_HOST:-deploy@5.223.69.203}}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/jobseeker_deploy}"
DIR="${DEPLOY_DIR:-/opt/mrmeeseeks}"

cd "$(dirname "$0")/.."
rsync -av --ignore-existing -e "ssh -i $KEY" "$HOST:$DIR/state/templates/" server/data/templates/
git status --short server/data/templates/
