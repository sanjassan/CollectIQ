#!/usr/bin/env python3
"""Self-built on-chain pull tracker for Renaiss -- fully independent of open-monitor.

How it works (empirically verified):
  All packs share the same ERC721 card contract:
      NFT = 0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30
  Every "pull" emits Transfer(from=gacha contract, to=buyer, tokenId) on that contract.
  Watching this contract's Transfer events lets us capture:
      tokenId (which card) - to (who pulled it) - block timestamp (when) - tx_hash
  Future Friday limited-time packs are picked up automatically as long as they reuse the same NFT contract.

RPC: the public node bsc.drpc.org supports getLogs (<=1000 blocks per call); free tier is enough for forward tracking.
  (A paid archival node is only needed for full historical backfill, which can be added later.)

Pulled cards are compared against the market in real time: joined with Renaiss Marketplace FMV (data/marketplace_all.json).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "onchain_pulls.db"
MARKET_PATH = ROOT / "data" / "marketplace_all.json"

NFT = Web3.to_checksum_address("0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30")
TRANSFER_TOPIC = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().lstrip("0x")

# Known gacha/pack contracts: if Transfer.from is in this set -> treated as a "pull mint" (MINT), otherwise a secondary transfer
GACHA_CONTRACTS = {a.lower() for a in [
    "0x94E7732B0B2E7c51FFD0D56580067d9c2e2B7910",  # omega
    "0xfdA4a907D23d9f24271Bc47483C5B983831E325E",  # eden
    "0xb2891022648c5Fad3721C42C05d8d283D4d53080",  # renacrypt
    "0xAAb5F5FA75437a6e9E7004c12C9c56CdA4b4885A",  # legacy/costume (old)
]}
ZERO = "0x0000000000000000000000000000000000000000"

# Set the BNB_RPC env var to an "API-keyed node" for better stability (strongly recommended for production tracking).
# Empirically (2026-06), free public nodes are very restrictive with eth_getLogs:
#   bsc.publicnode.com / 1rpc.io/bnb -> span <= 50 works; larger spans return 403/-32602
#   bnbchain.org / defibit family -> always -32005 limit exceeded (effectively blocks getLogs)
#   bsc.drpc.org -> occasionally allows large spans, but often 429 rate-limits (use only as a fallback)
# So the default is CHUNK=50, with "span-50-capable" nodes ordered first; a keyed BNB_RPC takes top priority.
# BNB_RPC may hold "API-keyed nodes" (comma- or space-separated for multiple fallbacks; highest priority).
# Empirically (2026-06), NodeReal allows getLogs span < 50000, far better than free public nodes (span <= 50).
_KEYED = [r.strip() for r in os.getenv("BNB_RPC", "").replace(",", " ").split() if r.strip()]
RPCS = _KEYED + [
    "https://bsc.publicnode.com",
    "https://1rpc.io/bnb",
    "https://bsc-rpc.publicnode.com",
    "https://bsc.drpc.org",
]
# With a keyed node, widen the span and catch up faster (a 19-day lag is cleared in minutes); otherwise fall back to the conservative free-node cap.
CHUNK = 2000 if _KEYED else 50            # block span per getLogs call
FIRST_RUN_LOOKBACK = 1000  # blocks to look back on the first run (afterwards continues from the cursor; full history needs an archival node)
MAX_CHUNKS_PER_RUN = 120 if _KEYED else 30    # max chunks scanned per run, to avoid running too long / getting rate-limited


def _mk(rpc: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


class RpcPool:
    """Multi-node round-robin load spreading: each call starts from the "next" node, spreading
    traffic evenly across the 4 keys so no single node gets hammered into its monthly quota /
    rate limit; only on failure does it fail over to the next node. Construction does "no probe
    calls," avoiding a wasted RPC call on every startup; broken nodes are skipped only when
    actually called."""

    def __init__(self):
        # Only build objects, don't connect (_mk sends no RPC), so a startup every 5 minutes doesn't waste N block_number probes.
        self.clients = [(rpc, _mk(rpc)) for rpc in RPCS]
        if not self.clients:
            raise RuntimeError("沒有可用的 BSC RPC（請檢查 BNB_RPC）")
        self.i = 0

    def _next(self):
        """Get the current node and advance the pointer by one (next call switches to the next node, spreading load evenly)."""
        item = self.clients[self.i]
        self.i = (self.i + 1) % len(self.clients)
        return item

    def _try_all(self, fn):
        """Try nodes in turn starting from the next one: return on success (the pointer has already
        advanced = next call auto-switches); only if "all nodes fail" is the last error raised."""
        last = None
        for _ in range(len(self.clients)):
            rpc, w3 = self._next()
            try:
                return fn(w3)
            except Exception as e:
                last = e
        raise last

    def block_number(self) -> int:
        return self._try_all(lambda w3: w3.eth.block_number)

    def get_logs(self, params):
        return self._try_all(lambda w3: w3.eth.get_logs(params))

    def get_block_ts(self, bn):
        return self._try_all(lambda w3: w3.eth.get_block(bn)["timestamp"])


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS onchain_pulls (
            tx_hash      TEXT,
            log_index    INTEGER,
            token_id     TEXT,
            from_addr    TEXT,
            to_addr      TEXT,
            is_mint      INTEGER,
            block_number INTEGER,
            block_time   INTEGER,
            market_fmv   REAL,
            card_name    TEXT,
            set_name     TEXT,
            first_seen   TEXT,
            PRIMARY KEY (tx_hash, log_index)
        )
        """
    )
    conn.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oc_to ON onchain_pulls(to_addr)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oc_tid ON onchain_pulls(token_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oc_time ON onchain_pulls(block_time)")
    return conn


def _load_market() -> dict:
    if not MARKET_PATH.exists():
        return {}
    out = {}
    for c in json.loads(MARKET_PATH.read_text(encoding="utf-8")):
        tid = str(c.get("token_id") or "")
        if tid:
            out[tid] = c
    return out


def _addr(topic) -> str:
    h = topic.hex()
    h = h if h.startswith("0x") else "0x" + h
    return "0x" + h[-40:]


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    pool = RpcPool()
    conn = _conn()
    market = _load_market()
    now = datetime.now(timezone.utc).isoformat()

    latest = pool.block_number()
    row = conn.execute("SELECT v FROM state WHERE k='last_block'").fetchone()
    start = int(row[0]) + 1 if row else latest - FIRST_RUN_LOOKBACK

    block_time: dict[int, int] = {}
    total_new = 0
    mint_new = 0
    chunks = 0
    cur = start
    while cur <= latest and chunks < MAX_CHUNKS_PER_RUN:
        end = min(cur + CHUNK, latest)
        try:
            logs = pool.get_logs({
                "address": NFT, "topics": [TRANSFER_TOPIC],
                "fromBlock": cur, "toBlock": end,
            })
        except Exception as e:
            print(f"  ⚠️ getLogs {cur}-{end} 所有節點皆失敗：{type(e).__name__}; 本輪到此停")
            break
        for lg in logs:
            if len(lg["topics"]) != 4:
                continue
            frm = _addr(lg["topics"][1]); to = _addr(lg["topics"][2])
            tid = str(int(lg["topics"][3].hex(), 16))
            bn = lg["blockNumber"]
            if bn not in block_time:
                block_time[bn] = pool.get_block_ts(bn)
            is_mint = 1 if (frm.lower() in GACHA_CONTRACTS or frm.lower() == ZERO) else 0
            mk = market.get(tid) or {}
            txh = lg["transactionHash"].hex(); txh = txh if txh.startswith("0x") else "0x"+txh
            c = conn.execute(
                "INSERT OR IGNORE INTO onchain_pulls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (txh, lg["logIndex"], tid, frm, to, is_mint, bn, block_time[bn],
                 mk.get("fmv"), mk.get("name"), mk.get("set_name"), now),
            )
            if c.rowcount:
                total_new += 1
                mint_new += is_mint
        conn.commit()
        conn.execute("INSERT OR REPLACE INTO state VALUES ('last_block', ?)", (str(end),))
        conn.commit()
        cur = end + 1
        chunks += 1
        time.sleep(0.2)  # be gentle on free nodes to lower the chance of being rate-limited

    grand = conn.execute("SELECT COUNT(*) FROM onchain_pulls").fetchone()[0]
    mints = conn.execute("SELECT COUNT(*) FROM onchain_pulls WHERE is_mint=1").fetchone()[0]
    row_last = conn.execute("SELECT v FROM state WHERE k='last_block'").fetchone()
    last = row_last[0] if row_last else f"(未推進，起點 {start})"

    # Sync freshness: lets the frontend tell "actually behind on sync" apart from "just nobody pulled recently."
    # synced_block tracks up to head, synced_ts is this run's wall-clock; only reading both together gives the real lag.
    synced_block = int(row_last[0]) if row_last else start - 1
    conn.execute("INSERT OR REPLACE INTO state VALUES ('head_block', ?)", (str(latest),))
    conn.execute("INSERT OR REPLACE INTO state VALUES ('synced_block', ?)", (str(synced_block),))
    conn.execute("INSERT OR REPLACE INTO state VALUES ('synced_ts', ?)", (str(int(time.time())),))
    conn.commit()

    # Real-time market comparison: list cards "just pulled" this run that have a market FMV (highest price first)
    hot = conn.execute(
        """
        SELECT card_name, token_id, market_fmv, to_addr, block_time
        FROM onchain_pulls
        WHERE first_seen=? AND is_mint=1 AND market_fmv IS NOT NULL
        ORDER BY market_fmv DESC LIMIT 5
        """,
        (now,),
    ).fetchall()
    conn.close()
    ts = datetime.now().strftime("%F %T")
    print(f"[{ts}] 鏈上追蹤：掃到區塊 {last}/{latest} · 本輪新增 {total_new}(其中抽卡 {mint_new}) · "
          f"累積 {grand}(抽卡 {mints})")
    for name, tid, fmv, to, bt in hot:
        when = datetime.fromtimestamp(bt, timezone.utc).strftime("%m-%d %H:%M")
        print(f"    💎 {name or ('#'+tid)}  FMV≈${fmv:.2f}  →{to[:10]}…  @{when}UTC")
    return 0


if __name__ == "__main__":
    sys.exit(main())
