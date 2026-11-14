#!/usr/bin/env bash
# 定投回测快照日更发布：导出最新数据 → 提交推送 origin → GitHub Pages
# workflow 自动重建（约 2-3 分钟）；ZCode Sites（zai.site）的 /data/* 由其
# 静态服务器实时透传 Pages 最新数据，本脚本无需部署 zai.site。
#
# 用法: publish_snapshot.sh [--deploy-zcode]
#   默认：导出 + 提交推送（Pages 日更）；
#   --deploy-zcode：额外重建并部署 zai.site（仅前端代码变更时需要）。
#
# cron：每日 23:50（晚于 NAS 回测日更 23:00-23:25）。
# 环境：DATABASE_URL（cron 行显式传入，见机器 crontab）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ZCODE_SITE_CLI="${ZCODE_SITE_CLI:-/home/ckd/.zcode/cli/plugins/cache/zcode-plugins-official/zcode-site/0.4.2/scripts/zcode-site.mjs}"
STAGE=".zcode-site/stage"

# mise shims 提前导出：cron 最小 PATH（/usr/bin:/bin）下无 node/npm，
# 且 set -e 下 ${:-$(command -v npm)} 失败会静默终止脚本（2026-11-05 踩坑）
export PATH="/home/ckd/.local/share/mise/shims:$PATH"
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
NPM_BIN="${NPM_BIN:-$(command -v npm || true)}"
if [ -z "$NODE_BIN" ] || [ -z "$NPM_BIN" ]; then
  echo "FATAL: 找不到 node/npm——PATH 无 mise shims 且未显式指定 NODE_BIN/NPM_BIN" >&2
  exit 1
fi

# 1) 导出最新快照（site-data/）
"$ROOT/.venv/bin/python" scripts/export_static_api.py

# 2) 提交推送 → Pages workflow 自动重建；zai.site 数据透传自动跟随
git add site-data
if git diff --cached --quiet; then
  echo "快照无变更，跳过提交"
else
  git commit -m "chore: 定投回测快照日更（自动发布）"
  git push origin main
fi

# 3) 可选：--deploy-zcode 重建并部署 zai.site（仅前端代码变更时需要；
#    日常数据更新由其 /data 透传自动跟随，无需此步）
if [[ "${1:-}" == "--deploy-zcode" ]]; then
  (cd frontend && VITE_STATIC=1 "$NPM_BIN" run build >/dev/null)
  rm -rf "$STAGE"
  mkdir -p "$STAGE/dist/data/funds/dca"
  cp -r frontend/dist/* "$STAGE/dist/"
  # 打包合同限 20000 文件（2026-11-14 navs 上线后 site-data 共 39.9k 超限）：
  # 包内数据只放排行+散点作 Pages 故障回退；rounds/navs 单基金明细
  # （13.3k+26.6k 文件）一律走 /data 运行时反代 Pages，不进包。
  cp site-data/funds/dca.json "$STAGE/dist/data/funds/dca/"
  cp site-data/funds/dca/scatter.*.json "$STAGE/dist/data/funds/dca/"
  cp scripts/zcode-site/static.sh "$STAGE/start.sh"
  cp scripts/zcode-site/static-server.mjs "$STAGE/static-server.mjs"
  "$NODE_BIN" "$ZCODE_SITE_CLI" deploy "$STAGE"
fi
echo "publish done $(date '+%F %T')"
