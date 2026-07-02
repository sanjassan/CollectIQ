#!/usr/bin/env python3
"""
migrate_to_ledger.py — 把現有三個 DB 無損整合進 collectiq_core.db。

冪等：可重複執行，靠自然主鍵 INSERT OR IGNORE 去重。
分步：每個 backfill_* 函數獨立，方便單獨重跑 / 測試。

Step 2（本檔目前實作）：backfill_transfers()
  來源優先序（較豐富者先灌，OR IGNORE 保住分類）：
    1) live_pool.events   有 pool + 已分類 kind（load/pull/recycle/burn）
    2) onchain_pulls      全域 NFT Transfer，kind 由零地址推導，pool 留 NULL
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ledger  # noqa: E402

DATA = ROOT / "data"
ONCHAIN_DB = DATA / "onchain_pulls.db"
LIVE_DB = DATA / "live_pool.db"

ZERO = "0x0000000000000000000000000000000000000000"


def _iso(ts) -> str | None:
    """unix 秒 或 已是字串 → ISO UTC 字串。"""
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _kind_from_addrs(frm: str | None, to: str | None) -> str:
    """全域 Transfer 缺池脈絡時，只能由零地址粗分類。"""
    f = (frm or "").lower()
    t = (to or "").lower()
    if f == ZERO:
        return ledger.KIND_MINT
    if t == ZERO:
        return ledger.KIND_BURN
    return ledger.KIND_XFER


def backfill_transfers(core: sqlite3.Connection) -> dict:
    """回填 ledger_transfers。回傳統計。"""
    stats = {"from_live": 0, "from_onchain": 0}

    # --- 1) live_pool.events（有 pool + kind，優先）---
    if LIVE_DB.exists():
        src = sqlite3.connect(str(LIVE_DB))
        rows = src.execute(
            "SELECT tx, log_index, block, time, token_id, from_addr, to_addr, "
            "kind, pool FROM events").fetchall()
        src.close()
        payload = [
            (tx, li, blk, _iso(t), str(tid), frm, to, pool, kind, 1)
            for (tx, li, blk, t, tid, frm, to, kind, pool) in rows
        ]
        cur = core.executemany(
            "INSERT OR IGNORE INTO ledger_transfers"
            "(tx_hash,log_index,block_number,block_time,token_id,"
            " from_addr,to_addr,pool,kind,confirmed) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", payload)
        stats["from_live"] = cur.rowcount if cur.rowcount != -1 else len(payload)
        core.commit()

    # --- 2) onchain_pulls（全域，OR IGNORE 不覆蓋已分類列）---
    if ONCHAIN_DB.exists():
        src = sqlite3.connect(str(ONCHAIN_DB))
        rows = src.execute(
            "SELECT tx_hash, log_index, block_number, block_time, token_id, "
            "from_addr, to_addr FROM onchain_pulls").fetchall()
        src.close()
        payload = [
            (tx, li, blk, _iso(bt), str(tid), frm, to, None,
             _kind_from_addrs(frm, to), 1)
            for (tx, li, blk, bt, tid, frm, to) in rows
        ]
        before = core.execute("SELECT COUNT(*) FROM ledger_transfers").fetchone()[0]
        core.executemany(
            "INSERT OR IGNORE INTO ledger_transfers"
            "(tx_hash,log_index,block_number,block_time,token_id,"
            " from_addr,to_addr,pool,kind,confirmed) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", payload)
        core.commit()
        after = core.execute("SELECT COUNT(*) FROM ledger_transfers").fetchone()[0]
        stats["from_onchain"] = after - before

    return stats


def main() -> int:
    core = ledger.init_db()
    print("[migrate] backfill_transfers ...")
    st = backfill_transfers(core)
    total = core.execute("SELECT COUNT(*) FROM ledger_transfers").fetchone()[0]
    kinds = core.execute(
        "SELECT kind, COUNT(*) FROM ledger_transfers GROUP BY kind "
        "ORDER BY 2 DESC").fetchall()
    with_pool = core.execute(
        "SELECT COUNT(*) FROM ledger_transfers WHERE pool IS NOT NULL").fetchone()[0]
    core.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('transfers_backfilled_at',?)",
                 (datetime.now(timezone.utc).isoformat(),))
    core.commit()
    print(f"[migrate] 新增 live={st['from_live']} onchain={st['from_onchain']}")
    print(f"[migrate] ledger_transfers 總計 {total}（含 pool 脈絡 {with_pool}）")
    print(f"[migrate] kind 分布: {kinds}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
