#!/usr/bin/env bash
# 定投回测快照发布：导出最新数据 → 重建静态站 → 部署 zcode site。
# 每日由 cron 调用（23:50，晚于 NAS 回测日更 23:00-23:25）；也可手动执行。
#
# 用法: publish_zcode_site.sh [--commit-site-data]
#   默认只更新 zcode site；
#   --commit-site-data 额外把 site-data/ 提交并推送 origin main
#   （触发 GitHub Pages workflow 重建公开快照页）。
#
# 环境要求（机器相关，cron 行内显式传入）:
#   DATABASE_URL      回测库连接串
#   ZCODE_SITE_CLI    zcode-site.mjs 脚本路径（缺省用插件缓存路径）
#   NODE_BIN/NPM_BIN  node/npm 绝对路径（cron 的 PATH 无 nvm）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ZCODE_SITE_CLI="${ZCODE_SITE_CLI:-/home/ckd/.zcode/cli/plugins/cache/zcode-plugins-official/zcode-site/0.4.2/scripts/zcode-site.mjs}"
NODE_BIN="${NODE_BIN:-$(command -v node)}"
NPM_BIN="${NPM_BIN:-$(command -v npm)}"
STAGE=".zcode-site/stage"

# 1) 导出最新快照（site-data/）
export PATH="/home/ckd/.local/share/mise/shims:$PATH"   # cron 无 nvm/mise 激活，shims 自包含
"$ROOT/.venv/bin/python" scripts/export_static_api.py

# 2) 重建静态产物（根路径 base）并注入数据；start.sh/静态服务器用仓库
#    内收编的模板（scripts/zcode-site/，源自 site-deploy 技能 templates）
(cd frontend && "$NPM_BIN" run build >/dev/null)
rm -rf "$STAGE"
mkdir -p "$STAGE/dist/data"
cp -r frontend/dist/* "$STAGE/dist/"
cp -r site-data/* "$STAGE/dist/data/"
cp scripts/zcode-site/static.sh "$STAGE/start.sh"
cp scripts/zcode-site/static-server.mjs "$STAGE/static-server.mjs"

# 3) 部署 zcode site（覆盖式更新同一站点）
"$NODE_BIN" "$ZCODE_SITE_CLI" deploy "$STAGE"

# 4) 可选：提交 site-data 刷新 GitHub Pages
if [[ "${1:-}" == "--commit-site-data" ]]; then
  git add site-data
  if git diff --cached --quiet; then
    echo "site-data 无变更，跳过提交"
  else
    git commit -m "chore: 定投回测快照日更（自动发布）"
    git push origin main
  fi
fi
echo "publish done $(date '+%F %T')"
