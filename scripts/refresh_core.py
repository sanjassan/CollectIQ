#!/usr/bin/env python3
"""
refresh_core.py — periodic incremental refresh of collectiq_core.db's derived layer.

The RAW layer (ledger_transfers) is dual-written in real time by pool_live_monitor; this file
recomputes RAW + source snapshots into CURATED/MART and updates the rewards and enrichment queues.
Everything is idempotent and safe to schedule at high frequency.

Steps:
  1) backfill_transfers      add new rows into onchain_pulls / live_pool.events (deduplicated)
  2) backfill_dims           dim_card / dim_wallet
  3) backfill_holdings       fact_holding (folded from RAW)
  4) backfill_market         ledger_market (diffed against the current listing snapshot)
  5) backfill_fmv_snapshots  FMV time series
  6) rewards                 Eeveelution full set / serial runs / SBT
  7) enrich queue            Index API priority queue

Schedule: launchd ai.renaiss.corerefresh, StartInterval=900 (every 15 min).
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
