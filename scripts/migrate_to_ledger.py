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

import re
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
COLLECTIQ_DB = DATA / "collectiq.db"

ZERO = "0x0000000000000000000000000000000000000000"

# 已知基礎設施 / 池地址標籤（小寫）
KNOWN_LABELS = {
    "0xb95f8867ff54fd16342cb414c0f57237be7dc512": ("central_minter", 1),
    "0x341edb3edc1e45612e5704f29ec8d26fbb4072b4": ("relay", 1),
    "0x478e0a8304ea430187fa9c085a139599af2e03b1": ("operator", 0),
    "0xcf20276612cd67d2db0726120ee6161fef498e17": ("operator", 0),
    "0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910": ("pool:omega", 1),
    "0xfda4a907d23d9f24271bc47483c5b983831e325e": ("pool:eden", 1),
    "0xb2891022648c5fad3721c42c05d8d283d4d53080": ("pool:renacrypt", 1),
    "0xaab5f5fa75437a6e9e7004c12c9c56cda4b4885a": ("pool:legacy", 1),
    ZERO: ("zero", 1),
}


def _serial_num(serial: str | None) -> int | None:
    """從序號字串解析數字（支援 '#12/100' 與 'BGS0015525430' 等）。"""
    if not serial:
        return None
    for part in serial.replace("#", " ").replace("/", " ").split():
        if part.isdigit():
            return int(part)
    m = re.search(r"(\d+)$", serial)  # 退而求其次：取末尾連續數字
    return int(m.group(1)) if m else None


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


def backfill_dims(core: sqlite3.Connection) -> dict:
    """回填 dim_card / dim_wallet（導出維度，全量 REPLACE 重建，冪等）。"""
    now = datetime.now(timezone.utc).isoformat()
    card: dict[str, dict] = {}

    # --- dim_card：collectiq（豐富靜態屬性）為底 ---
    if COLLECTIQ_DB.exists():
        src = sqlite3.connect(str(COLLECTIQ_DB))
        for (tid, name, set_name, cnum, cname, serial, grader, grade, img) in src.execute(
                "SELECT token_id,name,set_name,card_number,character_name,serial,"
                "grader,grade,image_url FROM tokens"):
            card[str(tid)] = {
                "name": name, "set_name": set_name, "card_number": cnum,
                "character_name": cname, "serial": serial,
                "serial_num": _serial_num(serial), "grader": grader,
                "grade": grade, "tier": None, "image_url": img,
            }
        src.close()

    # --- dim_card：live_pool.tokens 補 tier，並加入池內獨有卡 ---
    if LIVE_DB.exists():
        src = sqlite3.connect(str(LIVE_DB))
        for (tid, cardname, tier) in src.execute(
                "SELECT token_id,card_name,tier FROM tokens"):
            tid = str(tid)
            if tid in card:
                if tier:
                    card[tid]["tier"] = tier
            else:
                card[tid] = {
                    "name": cardname, "set_name": None, "card_number": None,
                    "character_name": None, "serial": None, "serial_num": None,
                    "grader": None, "grade": None, "tier": tier, "image_url": None,
                }
        src.close()

    payload = [
        (tid, d["name"], d["set_name"], d["card_number"], d["character_name"],
         None, d["serial"], d["serial_num"], d["grader"], d["grade"],
         d["tier"], d["image_url"], None, now)
        for tid, d in card.items()
    ]
    core.executemany(
        "INSERT OR REPLACE INTO dim_card"
        "(token_id,name,set_name,card_number,character_name,character_family,"
        " serial,serial_num,grader,grade,tier,image_url,image_local,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload)
    core.commit()

    # --- dim_wallet：live_pool.wallets + 已知標籤 ---
    wallets: dict[str, dict] = {}
    if LIVE_DB.exists():
        src = sqlite3.connect(str(LIVE_DB))
        for (addr, is_c, fsb, fst) in src.execute(
                "SELECT address,is_contract,first_seen_block,first_seen_time FROM wallets"):
            a = (addr or "").lower()
            wallets[a] = {"is_contract": is_c or 0, "label": None,
                          "first_seen_block": fsb, "first_seen_time": fst}
        src.close()
    for addr, (label, is_c) in KNOWN_LABELS.items():
        w = wallets.setdefault(addr, {"is_contract": is_c, "label": None,
                                      "first_seen_block": None, "first_seen_time": None})
        w["label"] = label
        w["is_contract"] = is_c
    wpayload = [
        (a, w["is_contract"], w["label"], w["first_seen_block"], w["first_seen_time"])
        for a, w in wallets.items()
    ]
    core.executemany(
        "INSERT OR REPLACE INTO dim_wallet"
        "(address,is_contract,label,first_seen_block,first_seen_time) "
        "VALUES(?,?,?,?,?)", wpayload)
    core.commit()
    return {"cards": len(payload), "wallets": len(wpayload)}


def backfill_holdings(core: sqlite3.Connection) -> dict:
    """由 ledger_transfers 摺疊導出 fact_holding（每 token 取最新一筆 transfer）。

    掛牌狀態暫用 collectiq 快照；日後 ledger_market 建好後改由市場事件摺疊。
    """
    # 池地址集合（供 in_pool 判定）
    pools = {a for (a,) in core.execute(
        "SELECT address FROM dim_wallet WHERE label LIKE 'pool:%'").fetchall()}

    # 每 token 的最新一筆 transfer（block, log_index 排序）
    latest = core.execute("""
        WITH ranked AS (
            SELECT token_id, to_addr, pool, block_number, block_time, kind,
                   ROW_NUMBER() OVER (
                       PARTITION BY token_id
                       ORDER BY block_number DESC, log_index DESC) rn
            FROM ledger_transfers)
        SELECT token_id, to_addr, pool, block_number, block_time, kind
        FROM ranked WHERE rn = 1
    """).fetchall()

    # 掛牌快照（collectiq 現況）
    listed: dict[str, tuple] = {}
    if COLLECTIQ_DB.exists():
        src = sqlite3.connect(str(COLLECTIQ_DB))
        for (tid, il, ap) in src.execute(
                "SELECT token_id,is_listed,ask_price FROM tokens"):
            listed[str(tid)] = (il or 0, ap)
        src.close()

    payload = []
    for (tid, to_addr, pool, blk, btime, kind) in latest:
        to_l = (to_addr or "").lower()
        if to_l == ZERO:
            status, holder, cur_pool = "burned", None, None
        elif to_l in pools:
            status, holder, cur_pool = "in_pool", None, to_l
        else:
            status, holder, cur_pool = "held", to_l, pool
        il, ap = listed.get(str(tid), (0, None))
        payload.append((str(tid), holder, status, cur_pool, il, ap, blk, btime))

    core.executemany(
        "INSERT OR REPLACE INTO fact_holding"
        "(token_id,current_holder,status,pool,is_listed,ask_price,last_block,last_time) "
        "VALUES(?,?,?,?,?,?,?,?)", payload)
    core.commit()
    return {"holdings": len(payload)}


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

    print("[migrate] backfill_dims ...")
    ds = backfill_dims(core)
    core.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('dims_backfilled_at',?)",
                 (datetime.now(timezone.utc).isoformat(),))
    core.commit()
    print(f"[migrate] dim_card={ds['cards']} dim_wallet={ds['wallets']}")

    print("[migrate] backfill_holdings ...")
    hs = backfill_holdings(core)
    core.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('holdings_backfilled_at',?)",
                 (datetime.now(timezone.utc).isoformat(),))
    core.commit()
    st_dist = core.execute(
        "SELECT status, COUNT(*) FROM fact_holding GROUP BY status "
        "ORDER BY 2 DESC").fetchall()
    print(f"[migrate] fact_holding={hs['holdings']} 狀態分布: {st_dist}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
