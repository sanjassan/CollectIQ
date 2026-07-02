#!/usr/bin/env bash
# Renaiss 全量同步：open-monitor（pulls + 卡池）→ marketplace（全部卡牌，最後寫 pool_data）
# 主要資料來源為 tRPC / open-monitor API，無需 BNB RPC。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
PY=/opt/anaconda3/bin/python3
echo "[$(date '+%F %T')] renaiss sync start"
"$PY" scripts/sync_open_monitor.py
"$PY" scripts/sync_renaiss_marketplace.py
# 重建持有總表（卡片資訊 + 鏈上當前存放位置），供 dashboard /holdings 使用
"$PY" scripts/build_holdings.py || echo "⚠️ build_holdings 失敗（不阻斷 sync）"
# 限量卡機追蹤（每周限量開放/新增偵測）+ 鏈上卡機地址探測（新卡機）
"$PY" scripts/track_limited_packs.py || echo "⚠️ track_limited_packs 失敗（不阻斷 sync）"
"$PY" scripts/discover_pools.py || echo "⚠️ discover_pools 失敗（不阻斷 sync）"
echo "[$(date '+%F %T')] renaiss sync done"
