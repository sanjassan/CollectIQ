#!/usr/bin/env python3
"""
ledger.py — CollectIQ 統一核心資料庫（collectiq_core.db）的 schema 與連線工具。

設計原則（事件溯源 / append-only）：
  RAW 層     只增不覆，每筆鏈上 Transfer / 市場事件都全記錄，靠自然主鍵冪等去重。
  CURATED 層 維度表 + 當前狀態，全部可從 RAW 層純函數重建。
  MART 層    聚合快照（EV 時序、獎勵狀態），供顯示 / 通知。

token_id 一律以 TEXT 保存（uint256，CAST INTEGER 會溢位）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_DB = ROOT / "data" / "collectiq_core.db"

# 交易分類（鏈上 Transfer 語意）
KIND_MINT = "mint"        # 0x0 -> 中央/中繼（尚未進池）
KIND_LOAD = "load"        # 基礎設施 -> 池（灌卡）
KIND_PULL = "pull"        # 池 -> 錢包（抽出）
KIND_RECYCLE = "recycle"  # 錢包 -> 池（回收）
KIND_BURN = "burn"        # -> 0x0（銷毀）
KIND_XFER = "transfer"    # 錢包 <-> 錢包（其他移轉，含二級市場成交）

# 市場事件分類
MKT_LIST = "list"
MKT_RELIST = "relist"     # 改價
MKT_DELIST = "delist"
MKT_SALE = "sale"

SCHEMA = """
-- ============ RAW 層（不可變、全記錄）============

-- 全 NFT Transfer 事件。自然主鍵 (tx_hash, log_index) 全鏈唯一 → 冪等去重。
-- confirmed=0 表示尚在 reorg 深度內（未定案）；確定後改 1。
CREATE TABLE IF NOT EXISTS ledger_transfers (
    tx_hash      TEXT NOT NULL,
    log_index    INTEGER NOT NULL,
    block_number INTEGER,
    block_time   TEXT,
    token_id     TEXT NOT NULL,
    from_addr    TEXT,
    to_addr      TEXT,
    pool         TEXT,            -- 若已知屬於某池，記錄池地址
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

-- 全市場事件（上架/改價/下架/成交）。快照 diff 合成事件，靠內容主鍵去重。
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

-- SBT（不可轉讓獎勵）授予事件。
CREATE TABLE IF NOT EXISTS sbt_awards (
    tx_hash    TEXT NOT NULL,
    token_id   TEXT NOT NULL,
    wallet     TEXT,
    reason     TEXT,
    block_time TEXT,
    PRIMARY KEY (tx_hash, token_id)
);
CREATE INDEX IF NOT EXISTS idx_sbt_wallet ON sbt_awards(wallet);

-- ============ CURATED 層（維度 + 當前狀態）============

-- 卡牌維度：token_id → 靜態屬性。
CREATE TABLE IF NOT EXISTS dim_card (
    token_id         TEXT PRIMARY KEY,
    name             TEXT,
    set_name         TEXT,
    card_number      TEXT,
    character_name   TEXT,
    character_family TEXT,   -- 例：eeveelution（伊布家族），供全款判定
    serial           TEXT,
    serial_num       INTEGER,-- 解析出的數字序號，供連號判定
    grader           TEXT,
    grade            TEXT,
    tier             TEXT,
    image_url        TEXT,
    image_local      TEXT,   -- data/img/{token_id}.jpg 本地快取路徑
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_dc_family ON dim_card(character_family);
CREATE INDEX IF NOT EXISTS idx_dc_serial ON dim_card(character_name, serial_num);

-- 錢包維度。
CREATE TABLE IF NOT EXISTS dim_wallet (
    address          TEXT PRIMARY KEY,
    is_contract      INTEGER DEFAULT 0,
    label            TEXT,
    first_seen_block INTEGER,
    first_seen_time  TEXT
);

-- 當前持有狀態：由 ledger_transfers 摺疊導出（可重建）。
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

-- FMV 時序快照（append-only）：每次採集追加，供 EV 推移與幸運值分析。
-- luck_value = index_price_usd / renaiss_fmv（>1.5 視為幸運卡 / 復活蛋）。
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

-- ============ MART 層（聚合快照）============

-- 卡池 EV 時序：每次巡檢寫一列。
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

-- 獎勵狀態：SBT / 連號 / 伊布全款 的達成與持有者。
CREATE TABLE IF NOT EXISTS reward_status (
    reward_type TEXT NOT NULL,   -- sbt / serial_run / eevee_full
    key         TEXT NOT NULL,   -- wallet 或 family 或 run 識別
    holder      TEXT,
    achieved    INTEGER DEFAULT 0,
    detail      TEXT,            -- JSON 明細
    updated_at  TEXT,
    PRIMARY KEY (reward_type, key)
);

-- Index API 補資料佇列：依 priority 消化每日 100 筆配額。
CREATE TABLE IF NOT EXISTS enrich_queue (
    token_id TEXT PRIMARY KEY,
    priority INTEGER DEFAULT 0,  -- 越大越優先（大獎/在池/被抽）
    reason   TEXT,
    added_at TEXT DEFAULT (datetime('now')),
    done     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_eq_pending ON enrich_queue(done, priority DESC);

-- 卡機內容目錄（開抽前預覽）：卡池「宣稱」裝了哪些卡。
-- 來源：Renaiss tRPC cardPack.getContent（packId + tiers）。
-- 這是「上鏈前」就能拿到的全池清單：countdown 階段卡片只有 Renaiss 內部
-- item_id（UUID），還沒 mint 成鏈上 token_id。開抽後才由鏈上灌卡回填 token_id。
-- 主鍵 (pack_id, item_id) → 冪等；刷新只更新目錄欄位，不清掉已補的市價/token_id。
-- 註：getContent 回的是「卡池組成」——每個抽卡槽一列（entry_id 唯一）。同一張
-- 實體卡（item_id / cert）可能佔多槽（低階常見卡以重複表示中獎權重）。故主鍵用
-- entry_id（保全部槽位，供加權 EV）；用 DISTINCT cert 去重後才對市場（省額度）。
CREATE TABLE IF NOT EXISTS pack_content (
    pack_id             TEXT NOT NULL,   -- Renaiss cardPack UUID
    entry_id            TEXT NOT NULL,   -- 抽卡槽 UUID（getContent card.id，唯一）
    item_id             TEXT,            -- 實體卡 UUID（可跨槽重複；尚未上鏈）
    pack_name           TEXT,
    pack_stage          TEXT,            -- countdown/active/archived
    tier                TEXT,            -- TOP/S/A/B/C/D
    name                TEXT,
    cert                TEXT,            -- 從圖片 URL 解析的鑑定證號（對真實市場的萬用鍵）
    grader              TEXT,
    grade               TEXT,
    year                INTEGER,
    image_url           TEXT,
    renaiss_buyback_usd REAL,            -- 官方買回基準價（= Renaiss FMV）
    market_price_usd    REAL,            -- 真實市場價（後續分批補；可為 NULL）
    luck_value          REAL,            -- market / renaiss（>1.5 = 藏寶卡）
    token_id            TEXT,            -- 開抽上鏈後解析回填（可為 NULL）
    captured_at         TEXT,
    updated_at          TEXT,
    PRIMARY KEY (pack_id, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_pc_pack  ON pack_content(pack_id, tier);
CREATE INDEX IF NOT EXISTS idx_pc_cert  ON pack_content(cert);
CREATE INDEX IF NOT EXISTS idx_pc_item  ON pack_content(item_id);
CREATE INDEX IF NOT EXISTS idx_pc_token ON pack_content(token_id);
CREATE INDEX IF NOT EXISTS idx_pc_luck  ON pack_content(luck_value);

-- 遷移 / 掃描狀態鍵值。
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


REORG_DEPTH = 15  # 區塊確定深度：head 之下未滿此深度視為未定案（confirmed=0）


def ingest_transfer(core: sqlite3.Connection, tx_hash: str, log_index: int,
                    block_number: int, block_time, token_id, from_addr, to_addr,
                    pool, kind, chain_head: int,
                    reorg_depth: int = REORG_DEPTH) -> None:
    """冪等寫入單筆 Transfer。距鏈頭未滿 reorg_depth 的區塊標 confirmed=0。

    用 INSERT OR IGNORE：已存在的 (tx_hash,log_index) 不覆蓋，避免把已定案列
    降級。定案升級由 finalize_confirmations() 負責。
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
    """把已達確定深度的未定案列升級 confirmed=1。回傳升級筆數。"""
    cutoff = chain_head - reorg_depth
    cur = core.execute(
        "UPDATE ledger_transfers SET confirmed=1 "
        "WHERE confirmed=0 AND block_number <= ?", (cutoff,))
    core.commit()
    return cur.rowcount


def connect(path: Path | str = CORE_DB) -> sqlite3.Connection:
    """開啟核心 DB 連線，套用 WAL 與外鍵設定。"""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | str = CORE_DB) -> sqlite3.Connection:
    """建立所有表與索引（冪等）。回傳連線。"""
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# 期望存在的表（供測試斷言）
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
    print(f"[ledger] 建立 {len(tables & EXPECTED_TABLES)}/{len(EXPECTED_TABLES)} 表")
    if missing:
        print(f"[ledger] 缺少: {missing}")
    if extra:
        print(f"[ledger] 額外: {extra}")
    print("[ledger] OK" if not missing else "[ledger] FAIL")
