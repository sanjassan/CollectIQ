#!/usr/bin/env python3
"""Renaiss 自建鏈上抽卡追蹤器 —— 完全不依賴 open-monitor。

原理（已實測驗證）：
  所有卡池共用同一張 ERC721 卡片合約：
      NFT = 0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30
  每次「抽卡」會在該合約 emit Transfer(from=gacha合約, to=買家, tokenId)。
  盯住這個合約的 Transfer 事件，就能抓到：
      tokenId(哪張) · to(被誰抽走) · block timestamp(何時) · tx_hash
  未來週五限時卡池只要沿用同一張 NFT 合約即自動納入。

RPC：公開節點 bsc.drpc.org 支援 getLogs（≤1000 區塊/次），免費即可前向追蹤。
  （完整歷史回補才需要付費 archival 節點，可日後再加。）

抽到的卡即時與市場比對：join Renaiss Marketplace FMV（data/marketplace_all.json）。
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

# 已知 gacha/pack 合約：Transfer.from ∈ 此集合 → 視為「抽卡鑄造」(MINT)，否則為二手轉移
GACHA_CONTRACTS = {a.lower() for a in [
    "0x94E7732B0B2E7c51FFD0D56580067d9c2e2B7910",  # omega
    "0xfdA4a907D23d9f24271Bc47483C5B983831E325E",  # eden
    "0xb2891022648c5Fad3721C42C05d8d283D4d53080",  # renacrypt
    "0xAAb5F5FA75437a6e9E7004c12C9c56CdA4b4885A",  # legacy/costume (舊)
]}
ZERO = "0x0000000000000000000000000000000000000000"

# 可用 BNB_RPC 環境變數放「帶 API key 的節點」以提升穩定度（強烈建議用於正式追蹤）。
# 經實測（2026-06）：免費公開節點對 eth_getLogs 限制很嚴：
#   bsc.publicnode.com / 1rpc.io/bnb → span ≤ 50 可用；其餘 span 直接 403/-32602
#   bnbchain.org / defibit 系 → 一律 -32005 limit exceeded（等同封鎖 getLogs）
#   bsc.drpc.org → 偶爾允許大 span，但常 429 rate-limit（只當備援）
# 故預設 CHUNK=50，並把「能用 span50」的節點排前面；帶 key 的 BNB_RPC 最優先。
# BNB_RPC 可放「帶 API key 的節點」（可用逗號或空白分隔多個做備援，最優先）。
# 實測（2026-06）NodeReal 允許 getLogs span < 50000，遠勝免費公開節點（span≤50）。
_KEYED = [r.strip() for r in os.getenv("BNB_RPC", "").replace(",", " ").split() if r.strip()]
RPCS = _KEYED + [
    "https://bsc.publicnode.com",
    "https://1rpc.io/bnb",
    "https://bsc-rpc.publicnode.com",
    "https://bsc.drpc.org",
]
# 有帶 key 的節點時放大跨度、加快追補（19 天落後幾分鐘補完）；否則退回免費節點保守上限。
CHUNK = 2000 if _KEYED else 50            # 每次 getLogs 區塊跨度
FIRST_RUN_LOOKBACK = 1000  # 首次執行往回看的區塊數（之後從 cursor 續抓；完整歷史需 archival 節點）
MAX_CHUNKS_PER_RUN = 120 if _KEYED else 30    # 單次執行最多掃幾個 chunk，避免一次跑太久/被限速


def _mk(rpc: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


class RpcPool:
    """多節點 round-robin 負載分攤：每次呼叫從「下一個」節點起步，把流量平均
    散到 4 支 key 上，避免單一節點被連續打到撞月配額/速率上限；該節點失敗才
    換下一個做 failover。建構時「不做探測呼叫」，省掉每次啟動的無謂 RPC call；
    壞掉的節點在實際呼叫時才被略過。"""

    def __init__(self):
        # 只建物件、不連線（_mk 不發 RPC），避免每 5 分鐘啟動就浪費 N 次 block_number 探測。
        self.clients = [(rpc, _mk(rpc)) for rpc in RPCS]
        if not self.clients:
            raise RuntimeError("沒有可用的 BSC RPC（請檢查 BNB_RPC）")
        self.i = 0

    def _next(self):
        """取目前節點，並把指標往前推一格（下次呼叫換下一支，平均分攤）。"""
        item = self.clients[self.i]
        self.i = (self.i + 1) % len(self.clients)
        return item

    def _try_all(self, fn):
        """從下一個節點開始輪流嘗試：成功即回（指標已前進＝下次自動換人），
        只有「全部節點都失敗」才丟出最後錯誤。"""
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
        time.sleep(0.2)  # 對免費節點客氣一點，降低被限速機率

    grand = conn.execute("SELECT COUNT(*) FROM onchain_pulls").fetchone()[0]
    mints = conn.execute("SELECT COUNT(*) FROM onchain_pulls WHERE is_mint=1").fetchone()[0]
    row_last = conn.execute("SELECT v FROM state WHERE k='last_block'").fetchone()
    last = row_last[0] if row_last else f"(未推進，起點 {start})"

    # 即時市場比對：列出本輪「剛抽出」且有市場 FMV 的卡（高價在前）
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
