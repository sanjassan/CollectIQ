#!/usr/bin/env python3
"""Per-pull tracker for Renaiss pulls — permanently accumulates "which card / when / pulled by whom".

Data source: the open-monitor public API (which already includes an on-chain scraper); no self-hosted BNB RPC needed.
  - /api/packs            : lists all pools (automatically covers future Friday limited pools as they open)
  - /api/recent-pulls     : per-pull records, including user_address (who) / pulled_at (when) / tx_hash

Design notes:
  - Stored permanently in SQLite with tx_hash as the primary key for dedup → polling the same window
    multiple times never duplicates, and old records beyond the API window aren't lost (once written, kept forever).
  - Re-fetches packs each round, so a new pool is tracked as soon as it goes live.
  - High-frequency polling (5 minutes recommended) with a large limit means no misses even during a limited pool's burst.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://open-monitor-rmrm.pages.dev"
ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "pull_log.db"
PER_PACK_LIMIT = 5000


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pull_log (
            tx_hash       TEXT PRIMARY KEY,
            pull_id       INTEGER,
            pack_slug     TEXT,
            user_address  TEXT,
            card_name     TEXT,
            card_tier     TEXT,
            card_fmv_usd  REAL,
            token_id      TEXT,
            pulled_at     INTEGER,
            first_seen    TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pull_pack ON pull_log(pack_slug)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pull_addr ON pull_log(user_address)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pull_time ON pull_log(pulled_at)")
    return conn


def fetch_packs() -> list[dict]:
    r = requests.get(f"{BASE}/api/packs", timeout=30)
    r.raise_for_status()
    return r.json().get("packs", [])


def fetch_pulls(slug: str, limit: int = PER_PACK_LIMIT) -> list[dict]:
    r = requests.get(
        f"{BASE}/api/recent-pulls?limit={limit}&slug={slug}", timeout=60
    )
    r.raise_for_status()
    return r.json().get("pulls", [])


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()

    try:
        packs = fetch_packs()
    except Exception as e:
        print(f"❌ 取得卡池失敗：{e}")
        return 1

    total_new = 0
    per_pack: list[str] = []
    for p in packs:
        slug = p.get("slug") or ""
        if not slug:
            continue
        try:
            pulls = fetch_pulls(slug)
        except Exception as e:
            print(f"  ⚠️ {slug} 抓取失敗：{e}")
            continue
        new_here = 0
        for pull in pulls:
            tx = (pull.get("tx_hash") or "").strip()
            if not tx:
                # When tx_hash is missing, use id as the primary key to avoid dropping the record
                tx = f"id:{pull.get('id')}"
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO pull_log (
                    tx_hash, pull_id, pack_slug, user_address, card_name,
                    card_tier, card_fmv_usd, token_id, pulled_at, first_seen
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    tx,
                    pull.get("id"),
                    slug,
                    pull.get("user_address"),
                    pull.get("card_name"),
                    pull.get("card_tier"),
                    pull.get("card_fmv_usd"),
                    str(pull.get("token_id") or ""),
                    pull.get("pulled_at"),
                    now,
                ),
            )
            new_here += cur.rowcount
        conn.commit()
        total_new += new_here
        if pulls:
            per_pack.append(f"{slug}:{len(pulls)}抓/{new_here}新")

    grand = conn.execute("SELECT COUNT(*) FROM pull_log").fetchone()[0]
    conn.close()
    ts = datetime.now().strftime("%F %T")
    print(f"[{ts}] 抽卡追蹤：卡池 {len(packs)} · 本輪新增 {total_new} · 累積 {grand} 筆")
    if per_pack:
        print("   " + " | ".join(per_pack))
    return 0


if __name__ == "__main__":
    sys.exit(main())
