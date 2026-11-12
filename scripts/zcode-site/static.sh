#!/usr/bin/env bash
# ZCode Sites start.sh for a static site. Copy static-server.mjs next to this file.
# Runs in the foreground on 0.0.0.0:3000 (the runtime ignores the shebang and runs `bash start.sh`).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# ADJUST: directory holding index.html, relative to this file.
SITE_DIR=dist
# ADJUST: 1 for single-page apps (unknown paths fall back to index.html), 0 for multi-page sites.
export SPA_FALLBACK=1

export PORT="${PORT:-3000}"
# ADJUST: /data/* 实时反代上游最新快照（NAS 日更推 GitHub → 本站自动跟随）；
# 留空则只服务打包内数据。打包内 site-data 作为上游故障时的回退。
export DATA_PROXY_TARGET="${DATA_PROXY_TARGET:-https://codefuckee.github.io/honey-money/data}"
exec node static-server.mjs "$SITE_DIR"
