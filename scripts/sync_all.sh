#!/usr/bin/env bash
# Renaiss full sync: open-monitor (pulls + pools) -> marketplace (all cards, then write pool_data)
# Primary data source is the tRPC / open-monitor API; no BNB RPC required.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
PY=/opt/anaconda3/bin/python3
echo "[$(date '+%F %T')] renaiss sync start"
"$PY" scripts/sync_open_monitor.py
"$PY" scripts/sync_renaiss_marketplace.py
# Rebuild the holdings master table (card info + current on-chain location) for dashboard /holdings
"$PY" scripts/build_holdings.py || echo "⚠️ build_holdings failed (does not block sync)"
# Limited pack tracking (weekly limited-open / new-pack detection) + on-chain pack address probing (new packs)
"$PY" scripts/track_limited_packs.py || echo "⚠️ track_limited_packs failed (does not block sync)"
"$PY" scripts/discover_pools.py || echo "⚠️ discover_pools failed (does not block sync)"
echo "[$(date '+%F %T')] renaiss sync done"
