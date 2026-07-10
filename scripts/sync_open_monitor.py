#!/usr/bin/env python3
"""Sync pools and pull records from the open-monitor API into the v2 local data/ directory."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

BASE = "https://open-monitor-rmrm.pages.dev"
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def fetch_packs() -> list[dict]:
    r = requests.get(f"{BASE}/api/packs", timeout=30)
    r.raise_for_status()
    return r.json().get("packs", [])


def fetch_pulls(limit: int = 5000, slug: str = "") -> list[dict]:
    path = f"/api/recent-pulls?limit={limit}"
    if slug:
        path += f"&slug={slug}"
    r = requests.get(f"{BASE}{path}", timeout=60)
    r.raise_for_status()
    return r.json().get("pulls", [])


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    packs = fetch_packs()
    if not packs:
        print("❌ open-monitor has no card pool data")
        return 1

    all_pulls: list[dict] = []
    seen_tx: set[str] = set()
    for p in packs:
        slug = p.get("slug") or ""
        if not slug:
            continue
        for pull in fetch_pulls(limit=5000, slug=slug):
            tx = pull.get("tx_hash") or ""
            if tx and tx in seen_tx:
                continue
            if tx:
                seen_tx.add(tx)
            row = dict(pull)
            row["pack_slug"] = slug
            if row.get("token_id"):
                row["card_url"] = f"https://www.renaiss.xyz/card/{row['token_id']}"
            all_pulls.append(row)

    # Each active pack's "empirical EV" = average of observed pull FMVs (not using their EV formula,
    # only the actual pull distribution of their per-card FMVs), as an independent recompute against the official platform_ev.
    import statistics
    pull_fmv: dict[str, list[float]] = {}
    for pull in all_pulls:
        slug = pull.get("pack_slug") or ""
        fmv = pull.get("card_fmv_usd")
        if slug and fmv:
            try:
                pull_fmv.setdefault(slug, []).append(float(fmv))
            except (TypeError, ValueError):
                pass

    pack_data = []
    for p in packs:
        slug = p.get("slug")
        fmvs = pull_fmv.get(slug, [])
        tiers = {}
        for t in ("s", "a", "b", "c", "d"):
            tiers[t] = {
                "pct": p.get(f"tier_{t}_pct"),
                "min_fmv": p.get(f"tier_{t}_min_fmv"),
                "max_fmv": p.get(f"tier_{t}_max_fmv"),
                "actual_count": p.get(f"actual_{t}_count"),
            }
        pack_data.append({
            "pack_id": slug,
            "name": p.get("name"),
            "remaining_cards": p.get("current_remaining"),
            "total_cards": p.get("current_total"),
            "platform_ev_usd": p.get("current_platform_ev_usd"),
            "is_sold_out": bool(p.get("is_sold_out")),
            "is_limited": bool(p.get("is_limited")),
            "total_fmv_usd": p.get("total_fmv_usd"),
            "actual_total": p.get("actual_total"),
            "tiers": tiers,
            # Empirical EV (computed from the observed pull distribution)
            "empirical_ev_usd": round(statistics.mean(fmvs), 2) if fmvs else None,
            "empirical_median_usd": round(statistics.median(fmvs), 2) if fmvs else None,
            "pull_sample_n": len(fmvs),
            # Limited-pool tracking signals
            "last_s_card_name": p.get("last_s_card_name"),
            "last_s_pulled_at": p.get("last_s_pulled_at"),
            "last_s_token_id": p.get("last_s_token_id"),
            "streak_since_s": p.get("streak_since_s"),
            "pulls_last_30min": p.get("pulls_last_30min"),
            "last_scraped_at": p.get("last_scraped_at"),
        })

    pool_data = []
    for pull in all_pulls:
        fmv = pull.get("card_fmv_usd") or 0
        pool_data.append({
            "card_id": str(pull.get("token_id") or pull.get("id") or ""),
            "name": pull.get("card_name") or "Unknown",
            "remaining_quantity": 1,
            "market_price": float(fmv) if fmv else 0.0,
            "tier": pull.get("card_tier"),
            "pack_slug": pull.get("pack_slug"),
            "tx_hash": pull.get("tx_hash"),
            "card_url": pull.get("card_url"),
            "pulled_at": pull.get("pulled_at"),
            "sources": 1 if fmv else 0,
        })

    (DATA / "pack_data.json").write_text(json.dumps(pack_data, indent=2, ensure_ascii=False))
    (DATA / "pool_data.json").write_text(json.dumps(pool_data, indent=2, ensure_ascii=False))
    (DATA / "open_monitor_sync.json").write_text(json.dumps({
        "packs": len(pack_data),
        "pulls": len(pool_data),
        "source": BASE,
    }, indent=2))

    print(f"✅ Sync complete: {len(pack_data)} card pools · {len(pool_data)} draw records")
    print(f"   → {DATA / 'pool_data.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
