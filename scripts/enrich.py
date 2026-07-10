#!/usr/bin/env python3
"""
enrich.py — enrichment pipeline: Index API priority queue + local card-image cache.

This file addresses two problems, one half each:
  1) The Index API allows only 100 anonymous requests per day. The existing
     build_universe.enrich_from_index sorts only by renaiss_fmv and has no idea
     "which cards are currently in an active pool / just got pulled / are grand prizes".
     build_enrich_queue() computes a priority from pool context so the daily quota
     is spent on the most valuable cards first.
  2) dim_card only stores image_url, so images are lost when the original URL breaks.
     cache_images() saves them to data/img/{token_id}.jpg and backfills dim_card.image_local.

Usage:
  python3 enrich.py queue                 # rebuild the priority queue
  python3 enrich.py images [--limit N]    # download the first N pending images to cache (by priority)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ledger  # noqa: E402

IMG_DIR = ROOT / "data" / "img"
BIG_FMV = 300.0


def build_enrich_queue(core) -> dict:
    """Rebuild enrich_queue: find cards missing a real Index price, ordered by pool context.

    priority score (higher = queried sooner):
      + 2000  grand prize (renaiss_fmv >= BIG_FMV)
      + 1000  currently in an active pool (fact_holding.status='in_pool')
      +  500  currently held by a wallet (possibly just pulled)
      + min(renaiss_fmv, 900)  value weighting
    """
    # Whether each token's latest fmv snapshot already has an index price
    have_idx = {str(t) for (t,) in core.execute(
        "SELECT DISTINCT token_id FROM fmv_snapshots "
        "WHERE index_price_usd IS NOT NULL").fetchall()}

    # Candidates: dim_card has an image or a renaiss baseline, and no index price yet
    rows = core.execute("""
        SELECT d.token_id, d.tier, d.image_url,
               fh.status,
               (SELECT renaiss_fmv FROM fmv_snapshots s
                WHERE s.token_id = d.token_id
                ORDER BY s.ts DESC LIMIT 1) AS rfmv
        FROM dim_card d
        LEFT JOIN fact_holding fh ON fh.token_id = d.token_id
    """).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for (tid, tier, img, status, rfmv) in rows:
        tid = str(tid)
        if tid in have_idx:
            continue
        rfmv = rfmv or 0
        prio, reasons = 0, []
        if rfmv >= BIG_FMV:
            prio += 2000; reasons.append("big")
        if status == "in_pool":
            prio += 1000; reasons.append("in_pool")
        elif status == "held":
            prio += 500; reasons.append("held")
        prio += min(int(rfmv), 900)
        payload.append((tid, prio, ",".join(reasons) or "base", now, 0))

    core.executemany(
        "INSERT OR REPLACE INTO enrich_queue"
        "(token_id,priority,reason,added_at,done) VALUES(?,?,?,?,?)", payload)
    core.commit()
    return {"queued": len(payload)}


def cache_images(core, limit: int = 50) -> dict:
    """Download not-yet-cached images to data/img/ and backfill dim_card.image_local, by priority."""
    import requests
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    rows = core.execute("""
        SELECT d.token_id, d.image_url
        FROM dim_card d
        LEFT JOIN enrich_queue q ON q.token_id = d.token_id
        WHERE d.image_url IS NOT NULL AND d.image_url != ''
          AND (d.image_local IS NULL OR d.image_local = '')
        ORDER BY COALESCE(q.priority, 0) DESC
        LIMIT ?
    """, (limit,)).fetchall()
    ok = fail = 0
    for (tid, url) in rows:
        dest = IMG_DIR / f"{tid}.jpg"
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            dest.write_bytes(r.content)
            core.execute("UPDATE dim_card SET image_local=? WHERE token_id=?",
                         (str(dest.relative_to(ROOT)), str(tid)))
            ok += 1
        except Exception as e:
            print(f"  [fail] {str(tid)[:12]}… {e}")
            fail += 1
    core.commit()
    return {"cached": ok, "failed": fail, "attempted": len(rows)}


def main() -> int:
    core = ledger.init_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "queue"
    if cmd == "queue":
        st = build_enrich_queue(core)
        top = core.execute(
            "SELECT token_id, priority, reason FROM enrich_queue "
            "WHERE done=0 ORDER BY priority DESC LIMIT 5").fetchall()
        print(f"[enrich] queue {st['queued']} pending")
        for (t, p, r) in top:
            print(f"  prio={p:5} [{r}] {str(t)[:16]}…")
    elif cmd == "images":
        limit = 50
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        st = cache_images(core, limit)
        print(f"[enrich] image cache: succeeded {st['cached']} / failed {st['failed']} "
              f"(attempted {st['attempted']})")
    else:
        print(f"unknown cmd: {cmd}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
