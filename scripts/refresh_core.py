#!/usr/bin/env python3
"""
refresh_core.py — 定時增量刷新 collectiq_core.db 的導出層。

RAW 層（ledger_transfers）由 pool_live_monitor 即時雙寫；本檔負責把 RAW +
來源快照重算成 CURATED/MART，並更新獎勵與補資料佇列。全部冪等，可高頻排程。

流程：
  1) backfill_transfers      補進 onchain_pulls / live_pool.events 新增列（去重）
  2) backfill_dims           dim_card / dim_wallet
  3) backfill_holdings       fact_holding（由 RAW 摺疊）
  4) backfill_market         ledger_market（對當前掛牌快照做 diff）
  5) backfill_fmv_snapshots  FMV 時序
  6) rewards                 伊布全款 / 連號 / SBT
  7) enrich queue            Index API 優先佇列

排程：launchd ai.renaiss.corerefresh，StartInterval=900（每 15 分）。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ledger              # noqa: E402
import migrate_to_ledger as mig  # noqa: E402
import rewards             # noqa: E402
import enrich              # noqa: E402


def main() -> int:
    t0 = time.time()
    core = ledger.init_db()

    tr = mig.backfill_transfers(core)
    ds = mig.backfill_dims(core)
    hs = mig.backfill_holdings(core)
    ms = mig.backfill_market(core)
    fs = mig.backfill_fmv_snapshots(core)

    cf = rewards.classify_families(core)
    ev = rewards.compute_eevee_full(core)
    sr = rewards.compute_serial_runs(core)
    sb = rewards.compute_sbt(core)

    eq = enrich.build_enrich_queue(core)

    core.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('core_refreshed_at',?)",
                 (datetime.now(timezone.utc).isoformat(),))
    core.commit()

    dt = time.time() - t0
    print(f"[refresh] {datetime.now(timezone.utc):%F %T}Z "
          f"transfers(+live={tr['from_live']},+onchain={tr['from_onchain']}) "
          f"dims(card={ds['cards']},wallet={ds['wallets']}) "
          f"holdings={hs['holdings']} market(+{ms['events']}) fmv={fs['snaps']} "
          f"eevee={ev['holders']}/{ev['completed']} runs={sr['runs']} "
          f"sbt={sb['wallets']} queue={eq['queued']} ({dt:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
