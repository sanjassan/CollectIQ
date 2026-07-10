#!/usr/bin/env python3
"""
ledger.py — schema and connection helpers for CollectIQ's unified core database (collectiq_core.db).

Design principles (event sourcing / append-only):
  RAW layer     Append-only; every on-chain Transfer / market event is recorded in full,
                with idempotent dedup via natural primary keys.
  CURATED layer Dimension tables + current state, fully rebuildable as pure functions of the RAW layer.
  MART layer    Aggregate snapshots (EV time series, reward status) for display / notifications.

token_id is always stored as TEXT (uint256; CAST to INTEGER would overflow).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_DB = ROOT / "data" / "collectiq_core.db"

# Transaction classification (on-chain Transfer semantics)
KIND_MINT = "mint"        # 0x0 -> central/relay (not yet in pool)
KIND_LOAD = "load"        # infrastructure -> pool (loading cards)
KIND_PULL = "pull"        # pool -> wallet (pulled)
KIND_RECYCLE = "recycle"  # wallet -> pool (recycled)
KIND_BURN = "burn"        # -> 0x0 (burned)
KIND_XFER = "transfer"    # wallet <-> wallet (other transfers, incl. secondary-market trades)

# Market event classification
MKT_LIST = "list"
MKT_RELIST = "relist"     # price change
MKT_DELIST = "delist"
MKT_SALE = "sale"

SCHEMA = """
-- ============ RAW layer (immutable, full record) ============

-- All NFT Transfer events. Natural primary key (tx_hash, log_index) is chain-unique -> idempotent dedup.
-- confirmed=0 means still within reorg depth (not finalized); set to 1 once finalized.
CREATE TABLE IF NOT EXISTS ledger_transfers (
    tx_hash      TEXT NOT NULL,
    log_index    INTEGER NOT NULL,
    block_number INTEGER,
    block_time   TEXT,
    token_id     TEXT NOT NULL,
    from_addr    TEXT,
    to_addr      TEXT,
    pool         TEXT,            -- pool address, if the transfer is known to belong to a pool
    kind         TEXT,            -- mint/load/pull/recycle/burn/transfer
    confirmed    INTEGER NOT NULL DEFAULT 1,
    ingested_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_lt_token  ON ledger_transfers(token_id);
CREATE INDEX IF NOT EXISTS idx_lt_pool   ON ledger_transfers(pool, block_number);
CREATE INDEX IF NOT EXISTS idx_lt_time   ON ledger_transfers(block_time);
CREATE INDEX IF NOT EXISTS idx_lt_to     ON ledger_transfers(to_addr);
CREATE INDEX IF NOT EXISTS idx_lt_conf   ON ledger_transfers(confirmed);

-- All market events (list/relist/delist/sale). Synthesized by diffing snapshots; deduped via content-based primary key.
CREATE TABLE IF NOT EXISTS ledger_market (
    token_id    TEXT NOT NULL,
    serial      TEXT,
    event_kind  TEXT NOT NULL,   -- list/relist/delist/sale
    price_usd   REAL,
    seller      TEXT,
    buyer       TEXT,
    ts          TEXT NOT NULL,
    source      TEXT,
    ingested_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (token_id, event_kind, ts, price_usd)
);
CREATE INDEX IF NOT EXISTS idx_lm_token ON ledger_market(token_id);
CREATE INDEX IF NOT EXISTS idx_lm_ts    ON ledger_market(ts);

-- SBT (non-transferable reward) award events.
CREATE TABLE IF NOT EXISTS sbt_awards (
    tx_hash    TEXT NOT NULL,
    token_id   TEXT NOT NULL,
    wallet     TEXT,
    reason     TEXT,
    block_time TEXT,
    PRIMARY KEY (tx_hash, token_id)
);
CREATE INDEX IF NOT EXISTS idx_sbt_wallet ON sbt_awards(wallet);

-- ============ CURATED layer (dimensions + current state) ============

-- Card dimension: token_id -> static attributes.
CREATE TABLE IF NOT EXISTS dim_card (
    token_id         TEXT PRIMARY KEY,
    name             TEXT,
    set_name         TEXT,
    card_number      TEXT,
    character_name   TEXT,
    character_family TEXT,   -- e.g. eeveelution (the Eevee family), for full-set detection
    serial           TEXT,
    serial_num       INTEGER,-- parsed numeric serial, for consecutive-serial detection
    grader           TEXT,
    grade            TEXT,
    tier             TEXT,
    image_url        TEXT,
    image_local      TEXT,   -- local cache path, data/img/{token_id}.jpg
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_dc_family ON dim_card(character_family);
CREATE INDEX IF NOT EXISTS idx_dc_serial ON dim_card(character_name, serial_num);

-- Wallet dimension.
CREATE TABLE IF NOT EXISTS dim_wallet (
    address          TEXT PRIMARY KEY,
    is_contract      INTEGER DEFAULT 0,
    label            TEXT,
    first_seen_block INTEGER,
    first_seen_time  TEXT
);

-- Current holding state: derived by folding ledger_transfers (rebuildable).
CREATE TABLE IF NOT EXISTS fact_holding (
    token_id       TEXT PRIMARY KEY,
    current_holder TEXT,
    status         TEXT,     -- in_pool/held/burned
    pool           TEXT,
    is_listed      INTEGER DEFAULT 0,
    ask_price      REAL,
    last_block     INTEGER,
    last_time      TEXT
);
CREATE INDEX IF NOT EXISTS idx_fh_holder ON fact_holding(current_holder);
CREATE INDEX IF NOT EXISTS idx_fh_status ON fact_holding(status);
CREATE INDEX IF NOT EXISTS idx_fh_pool   ON fact_holding(pool);

-- FMV time-series snapshots (append-only): appended on each collection, for EV trend and luck-value analysis.
-- luck_value = index_price_usd / renaiss_fmv (>1.5 is treated as a lucky card / resurrection egg).
CREATE TABLE IF NOT EXISTS fmv_snapshots (
    token_id         TEXT NOT NULL,
    ts               TEXT NOT NULL,
    renaiss_fmv      REAL,
    index_price_usd  REAL,
    index_confidence TEXT,
    luck_value       REAL,
    source           TEXT,
    PRIMARY KEY (token_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_fs_token ON fmv_snapshots(token_id, ts);

-- ============ MART layer (aggregate snapshots) ============

-- Pool EV time series: one row written per patrol.
CREATE TABLE IF NOT EXISTS pool_ev_timeseries (
    pool                 TEXT NOT NULL,
    ts                   TEXT NOT NULL,
    loaded               INTEGER,
    in_pool              INTEGER,
    pulled               INTEGER,
    recycled             INTEGER,
    burned               INTEGER,
    buyers               INTEGER,
    remaining_ev_market  REAL,
    remaining_ev_renaiss REAL,
    ev_ratio             REAL,
    coverage             REAL,
    tier_counts          TEXT,   -- JSON: {tier: count}
    PRIMARY KEY (pool, ts)
);

-- Reward status: achievement and holders for SBT / consecutive serial / Eevee full-set.
CREATE TABLE IF NOT EXISTS reward_status (
    reward_type TEXT NOT NULL,   -- sbt / serial_run / eevee_full
    key         TEXT NOT NULL,   -- wallet, family, or run identifier
    holder      TEXT,
    achieved    INTEGER DEFAULT 0,
    detail      TEXT,            -- JSON detail
    updated_at  TEXT,
    PRIMARY KEY (reward_type, key)
);

-- Index API enrichment queue: consumes the daily quota of 100 by priority.
CREATE TABLE IF NOT EXISTS enrich_queue (
    token_id TEXT PRIMARY KEY,
    priority INTEGER DEFAULT 0,  -- higher = higher priority (jackpot / in-pool / pulled)
    reason   TEXT,
    added_at TEXT DEFAULT (datetime('now')),
    done     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_eq_pending ON enrich_queue(done, priority DESC);

-- Pack content catalog (pre-open preview): which cards a pack "claims" to contain.
-- Source: Renaiss tRPC cardPack.getContent (packId + tiers).
-- This is the full-pack list available "before" anything hits chain: during countdown, cards
-- only have Renaiss-internal item_id (UUID) and are not yet minted to an on-chain token_id.
-- token_id is backfilled from on-chain card loading only after the pack opens.
-- Primary key (pack_id, item_id) -> idempotent; refresh only updates catalog columns and does
-- not clear already-enriched market price / token_id.
-- Note: getContent returns the "pack composition" -- one row per draw slot (entry_id unique). A single
-- physical card (item_id / cert) may occupy multiple slots (low-tier common cards are duplicated to
-- represent win weighting). So the primary key uses entry_id (keeps all slots for weighted EV);
-- dedup by DISTINCT cert before hitting the market (to save quota).
CREATE TABLE IF NOT EXISTS pack_content (
    pack_id             TEXT NOT NULL,   -- Renaiss cardPack UUID
    entry_id            TEXT NOT NULL,   -- draw-slot UUID (getContent card.id, unique)
    item_id             TEXT,            -- physical-card UUID (may repeat across slots; not yet on chain)
    pack_name           TEXT,
    pack_stage          TEXT,            -- countdown/active/archived
    tier                TEXT,            -- TOP/S/A/B/C/D
    name                TEXT,
    cert                TEXT,            -- grading cert number parsed from image URL (universal key to the real market)
    grader              TEXT,
    grade               TEXT,
    year                INTEGER,
    image_url           TEXT,
    renaiss_buyback_usd REAL,            -- official buyback reference price (= Renaiss FMV)
    market_price_usd    REAL,            -- market reference price (backfilled later in batches; may be NULL)
    market_source       TEXT,            -- market price source: renaiss_index (non-independent) / pricecharting_ebay (independent) / ...
    market_url          TEXT,            -- verifiable source link for that market price (only for independent sources)
    luck_value          REAL,            -- market / renaiss (>1.5 = treasure card)
    token_id            TEXT,            -- resolved and backfilled after the pack opens on chain (may be NULL)
    captured_at         TEXT,
    updated_at          TEXT,
    PRIMARY KEY (pack_id, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_pc_pack  ON pack_content(pack_id, tier);
CREATE INDEX IF NOT EXISTS idx_pc_cert  ON pack_content(cert);
CREATE INDEX IF NOT EXISTS idx_pc_item  ON pack_content(item_id);
CREATE INDEX IF NOT EXISTS idx_pc_token ON pack_content(token_id);
CREATE INDEX IF NOT EXISTS idx_pc_luck  ON pack_content(luck_value);

-- Migration / scan state key-value store.
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


REORG_DEPTH = 15  # block finality depth: blocks less than this below head are treated as unfinalized (confirmed=0)


def ingest_transfer(core: sqlite3.Connection, tx_hash: str, log_index: int,
                    block_number: int, block_time, token_id, from_addr, to_addr,
                    pool, kind, chain_head: int,
                    reorg_depth: int = REORG_DEPTH) -> None:
    """Idempotently write a single Transfer. Blocks less than reorg_depth from head are marked confirmed=0.

    Uses INSERT OR IGNORE: an existing (tx_hash, log_index) is not overwritten, so an
    already-finalized row is never downgraded. Finalization upgrades are handled by
    finalize_confirmations().
    """
    confirmed = 1 if (chain_head - block_number) >= reorg_depth else 0
    core.execute(
        "INSERT OR IGNORE INTO ledger_transfers"
        "(tx_hash,log_index,block_number,block_time,token_id,"
        " from_addr,to_addr,pool,kind,confirmed) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (tx_hash, log_index, block_number, block_time, str(token_id),
         from_addr, to_addr, pool, kind, confirmed))


def finalize_confirmations(core: sqlite3.Connection, chain_head: int,
                           reorg_depth: int = REORG_DEPTH) -> int:
    """Upgrade unfinalized rows that have reached finality depth to confirmed=1. Returns the number upgraded."""
    cutoff = chain_head - reorg_depth
    cur = core.execute(
        "UPDATE ledger_transfers SET confirmed=1 "
        "WHERE confirmed=0 AND block_number <= ?", (cutoff,))
    core.commit()
    return cur.rowcount


def connect(path: Path | str = CORE_DB) -> sqlite3.Connection:
    """Open a core DB connection, applying WAL and foreign-key settings."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | str = CORE_DB) -> sqlite3.Connection:
    """Create all tables and indexes (idempotent). Returns the connection."""
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# Tables expected to exist (for test assertions)
EXPECTED_TABLES = {
    "ledger_transfers", "ledger_market", "sbt_awards",
    "dim_card", "dim_wallet", "fact_holding", "fmv_snapshots",
    "pool_ev_timeseries", "reward_status", "enrich_queue",
    "pack_content", "meta",
}


if __name__ == "__main__":
    c = init_db()
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    missing = EXPECTED_TABLES - tables
    extra = tables - EXPECTED_TABLES - {"sqlite_sequence"}
    print(f"[ledger] DB: {CORE_DB}")
    print(f"[ledger] created {len(tables & EXPECTED_TABLES)}/{len(EXPECTED_TABLES)} tables")
    if missing:
        print(f"[ledger] missing: {missing}")
    if extra:
        print(f"[ledger] extra: {extra}")
    print("[ledger] OK" if not missing else "[ledger] FAIL")
