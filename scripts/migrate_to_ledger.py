#!/usr/bin/env python3
"""
migrate_to_ledger.py — losslessly consolidate the three existing DBs into collectiq_core.db.

Idempotent: can be re-run repeatedly, deduped via natural primary keys with INSERT OR IGNORE.
Stepwise: each backfill_* function is independent, making it easy to re-run / test in isolation.

Step 2 (currently implemented in this file): backfill_transfers()
  Source priority (richer source loaded first; OR IGNORE preserves the classification):
    1) live_pool.events   has pool + already-classified kind (load/pull/recycle/burn)
    2) onchain_pulls      global NFT Transfers; kind inferred from the zero address, pool left NULL
"""
from __future__ import annotations

import json
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
MARKET_LISTED_JSON = DATA / "marketplace_listed.json"

ZERO = "0x0000000000000000000000000000000000000000"

# Known infrastructure / pool address labels (lowercase)
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
    """Parse a number from a serial string (supports '#12/100', 'BGS0015525430', etc.)."""
    if not serial:
        return None
    for part in serial.replace("#", " ").replace("/", " ").split():
        if part.isdigit():
            return int(part)
    m = re.search(r"(\d+)$", serial)  # fallback: take the trailing run of digits
    return int(m.group(1)) if m else None


def _iso(ts) -> str | None:
    """unix seconds, or an existing string -> ISO UTC string."""
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _kind_from_addrs(frm: str | None, to: str | None) -> str:
    """When a global Transfer lacks pool context, we can only roughly classify by the zero address."""
    f = (frm or "").lower()
    t = (to or "").lower()
    if f == ZERO:
        return ledger.KIND_MINT
    if t == ZERO:
        return ledger.KIND_BURN
    return ledger.KIND_XFER


def backfill_transfers(core: sqlite3.Connection) -> dict:
    """Backfill ledger_transfers. Returns stats."""
    stats = {"from_live": 0, "from_onchain": 0}

    # --- 1) live_pool.events (has pool + kind, takes priority) ---
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

    # --- 2) onchain_pulls (global; OR IGNORE does not overwrite already-classified rows) ---
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
    """Backfill dim_card / dim_wallet (derived dimensions, fully rebuilt via REPLACE, idempotent)."""
    now = datetime.now(timezone.utc).isoformat()
    card: dict[str, dict] = {}

    # --- dim_card: collectiq (rich static attributes) as the base ---
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

    # --- dim_card: live_pool.tokens fills in tier and adds pool-only cards ---
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

    # --- dim_wallet: live_pool.wallets + known labels ---
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
    """Derive fact_holding by folding ledger_transfers (latest transfer per token).

    Listing status temporarily uses the collectiq snapshot; once ledger_market is built,
    switch to folding market events instead.
    """
    # Set of pool addresses (for in_pool detection)
    pools = {a for (a,) in core.execute(
        "SELECT address FROM dim_wallet WHERE label LIKE 'pool:%'").fetchall()}

    # Latest transfer per token (ordered by block, log_index)
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

    # Listing snapshot (current collectiq state)
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


def record_market_snapshot(core: sqlite3.Connection, items: list[dict],
                           ts: str, source: str = "marketplace") -> dict:
    """Diff a listing snapshot into market events and append them to ledger_market (idempotent, reusable).

    Rules (comparing against each token's latest known event):
      now listed, previously none/delisted/sold -> list
      now listed, previously listed but at a different price -> relist
      now listed, same price                    -> no-op
      previously listed, now absent from snapshot -> delist
    In the future, sync_renaiss_marketplace can call this function directly.
    """
    # Latest market-event state per token
    last: dict[str, tuple] = {}
    for (tid, ek, price, t) in core.execute(
            "SELECT token_id, event_kind, price_usd, ts FROM ledger_market "
            "ORDER BY ts").fetchall():
        last[str(tid)] = (ek, price)  # later rows overwrite -> keep the latest

    holders = {str(t): h for (t, h) in core.execute(
        "SELECT token_id, current_holder FROM fact_holding").fetchall()}

    events = []
    cur_listed: dict[str, float] = {}
    for it in items:
        if not it.get("is_listed"):
            continue
        tid = str(it.get("token_id"))
        price = it.get("ask_price")
        cur_listed[tid] = price
        prev = last.get(tid)
        seller = holders.get(tid)
        if prev is None or prev[0] in (ledger.MKT_DELIST, ledger.MKT_SALE):
            events.append((tid, it.get("serial"), ledger.MKT_LIST, price, seller, None, ts, source))
        elif prev[0] in (ledger.MKT_LIST, ledger.MKT_RELIST) and prev[1] != price:
            events.append((tid, it.get("serial"), ledger.MKT_RELIST, price, seller, None, ts, source))
        # same-price re-listing: no-op

    # previously listed, now gone -> delist
    for tid, (ek, price) in last.items():
        if ek in (ledger.MKT_LIST, ledger.MKT_RELIST) and tid not in cur_listed:
            events.append((tid, None, ledger.MKT_DELIST, price, holders.get(tid), None, ts, source))

    core.executemany(
        "INSERT OR IGNORE INTO ledger_market"
        "(token_id,serial,event_kind,price_usd,seller,buyer,ts,source) "
        "VALUES(?,?,?,?,?,?,?,?)", events)
    core.commit()
    return {"events": len(events)}


def backfill_market(core: sqlite3.Connection) -> dict:
    """Build a market-event baseline from the current listing snapshot."""
    if not MARKET_LISTED_JSON.exists():
        return {"events": 0}
    items = json.loads(MARKET_LISTED_JSON.read_text())
    ts = datetime.fromtimestamp(
        MARKET_LISTED_JSON.stat().st_mtime, tz=timezone.utc).isoformat()
    return record_market_snapshot(core, items, ts, source="marketplace_backfill")


def backfill_fmv_snapshots(core: sqlite3.Connection) -> dict:
    """Seed the FMV snapshot baseline (renaiss / index / luck value), append-only."""
    if not COLLECTIQ_DB.exists():
        return {"snaps": 0}
    src = sqlite3.connect(str(COLLECTIQ_DB))
    rows = src.execute(
        "SELECT token_id, renaiss_fmv, index_price_usd, index_confidence, updated_at "
        "FROM tokens").fetchall()
    src.close()
    default_ts = datetime.now(timezone.utc).isoformat()
    payload = []
    for (tid, rf, idx, conf, upd) in rows:
        if rf is None and idx is None:
            continue
        luck = (idx / rf) if (rf and idx and rf > 0) else None
        payload.append((str(tid), upd or default_ts, rf, idx, conf, luck, "collectiq_backfill"))
    core.executemany(
        "INSERT OR IGNORE INTO fmv_snapshots"
        "(token_id,ts,renaiss_fmv,index_price_usd,index_confidence,luck_value,source) "
        "VALUES(?,?,?,?,?,?,?)", payload)
    core.commit()
    return {"snaps": len(payload)}


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
    print(f"[migrate] added live={st['from_live']} onchain={st['from_onchain']}")
    print(f"[migrate] ledger_transfers total {total} (with pool context {with_pool})")
    print(f"[migrate] kind distribution: {kinds}")

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
    print(f"[migrate] fact_holding={hs['holdings']} status distribution: {st_dist}")

    print("[migrate] backfill_market ...")
    ms = backfill_market(core)
    mk_dist = core.execute(
        "SELECT event_kind, COUNT(*) FROM ledger_market GROUP BY event_kind").fetchall()
    print(f"[migrate] ledger_market events {ms['events']} distribution: {mk_dist}")

    print("[migrate] backfill_fmv_snapshots ...")
    fs = backfill_fmv_snapshots(core)
    lucky = core.execute(
        "SELECT COUNT(*) FROM fmv_snapshots WHERE luck_value >= 1.5").fetchone()[0]
    print(f"[migrate] fmv_snapshots={fs['snaps']} (lucky cards luck>=1.5: {lucky})")

    core.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('market_fmv_backfilled_at',?)",
                 (datetime.now(timezone.utc).isoformat(),))
    core.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
