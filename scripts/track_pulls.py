#!/usr/bin/env python3
"""Renaiss 抽卡逐筆追蹤器 —— 永久累積「哪張卡 / 何時 / 被誰抽走」。

資料來源：open-monitor 公開 API（已內建鏈上爬蟲），不需要自架 BNB RPC。
  - /api/packs            ：列出所有卡池（自動涵蓋未來新開的週五限時卡機）
  - /api/recent-pulls     ：逐筆抽卡，含 user_address(誰) / pulled_at(何時) / tx_hash

設計重點：
  - 用 SQLite 永久保存，主鍵 tx_hash 去重 → 多次輪詢同一視窗不會重覆，
    且超過 API 視窗的舊紀錄不會遺失（一旦寫入就永久留存）。
  - 每輪自動重新抓 packs，新卡池一上線就會被納入追蹤。
  - 高頻輪詢（建議 5 分鐘）配合大 limit，限時卡機暴量也不漏。
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
                # 無 tx_hash 時用 id 當主鍵，避免漏記
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
