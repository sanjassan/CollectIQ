#!/usr/bin/env python3
"""
pool_live_monitor.py — 限量卡機「開放中」即時獎池追蹤器

專為「週五限量卡機開放窗口」設計：對單一卡池合約做高頻聚焦掃描，回答：
  · 這池灌了幾張卡（loaded）
  · 每一張卡的即時狀態：仍在池內 / 已被抽走(pulled) / 被抽走後又回收回池(recycled) / 已銷毀(burned)
  · 每張被抽走的卡是「哪個錢包」抽走的、什麼時間、哪筆 tx
  · 有沒有「新錢包 / 新智能合約」首次出現（開池前的鏈上前兆）

與 track_pulls_onchain.py 的差異：
  track_pulls 掃「整張共用 NFT 合約」的所有 Transfer（全站，量大、慢）。
  本監看器用 topic filter 只抓「與目標池相關」的 Transfer（from=池 或 to=池），
  資料量小很多 → 免費節點也能高頻（每 30~60 秒）更新，適合開放窗口的爆量抽卡。

資料寫入 data/live_pool.db：
  pool_meta(address, slug, name, active_from, loaded, pulled, recycled, remaining, burned,
            distinct_buyers, last_block, updated_at)
  tokens(token_id, status, holder, card_name, fmv, tier, is_big, last_block, last_time)
  events(block, log_index, time, token_id, kind, from_addr, to_addr, tx, card_name, fmv)
  wallets(address, first_seen_block, first_seen_time, pulls, is_contract)

目標池決定順序：
  1) POOL_ADDR 環境變數 / 命令列參數（明確指定）
  2) tRPC 找到「開放窗口內」的 limited 卡機 → 鏈上探測其池位址
  3) 預設 omega（有歷史資料，供介面現在就有東西可顯示 / 測試）

用法：
  python3 pool_live_monitor.py                 # 自動選池
  python3 pool_live_monitor.py 0x94e7...       # 指定池位址
  POOL_ADDR=0x.. python3 pool_live_monitor.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from track_pulls_onchain import (  # 重用穩定的多節點 RPC + 常數
    RpcPool, NFT, TRANSFER_TOPIC, ZERO, _addr, _load_market,
)

try:  # 統一核心帳本（RAW 全記錄、reorg 安全）；缺席不影響 live_pool 運作
    import ledger as _ledger
    _CORE = _ledger.init_db()
except Exception as _e:  # pragma: no cover
    _ledger = None
    _CORE = None
    print(f"[core] ledger 未啟用：{_e}")


def _iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


DATA = ROOT / "data"
LIVE_DB = DATA / "live_pool.db"
ONCHAIN_DB = DATA / "onchain_pulls.db"
TRPC = "https://www.renaiss.xyz/api/trpc/cardPack.getAll"

KNOWN_POOLS = {
    "0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910": "omega",
    "0xfda4a907d23d9f24271bc47483c5b983831e325e": "eden-pack",
    "0xb2891022648c5fad3721c42c05d8d283d4d53080": "renacrypt-pack",
    "0xaab5f5fa75437a6e9e7004c12c9c56cda4b4885a": "legacy/costume",
}
# 中央鑄造/路由合約與轉發中繼——這些不是「買家」，抽卡分佈統計要排除
INFRA_ADDRS = {
    "0xb95f8867ff54fd16342cb414c0f57237be7dc512",  # 中央鑄造/路由合約
    "0x341edb3edc1e45612e5704f29ec8d26fbb4072b4",  # 鑄造中繼
    ZERO,
}
BIG_FMV = 300.0  # ≥ 此 FMV 視為大獎


# ─── DB ──────────────────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(LIVE_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pool_meta (
            address TEXT PRIMARY KEY, slug TEXT, name TEXT, active_from TEXT,
            loaded INTEGER, pulled INTEGER, recycled INTEGER, remaining INTEGER,
            burned INTEGER, distinct_buyers INTEGER, last_block INTEGER, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tokens (
            pool TEXT, token_id TEXT, status TEXT, holder TEXT,
            card_name TEXT, fmv REAL, tier TEXT, is_big INTEGER,
            recycled INTEGER DEFAULT 0, last_block INTEGER, last_time INTEGER,
            PRIMARY KEY (pool, token_id)
        );
        CREATE TABLE IF NOT EXISTS events (
            pool TEXT, block INTEGER, log_index INTEGER, time INTEGER,
            token_id TEXT, kind TEXT, from_addr TEXT, to_addr TEXT, tx TEXT,
            card_name TEXT, fmv REAL,
            PRIMARY KEY (pool, block, log_index)
        );
        CREATE TABLE IF NOT EXISTS wallets (
            pool TEXT, address TEXT, first_seen_block INTEGER, first_seen_time INTEGER,
            pulls INTEGER DEFAULT 0, is_contract INTEGER DEFAULT 0,
            PRIMARY KEY (pool, address)
        );
        CREATE INDEX IF NOT EXISTS idx_ev_time ON events(pool, time DESC);
        CREATE INDEX IF NOT EXISTS idx_tok_status ON tokens(pool, status);
        """
    )
    return conn


# ─── 目標池決定 ───────────────────────────────────────────────────────────────
def _pad(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().replace("0x", "")


def fetch_limited_packs() -> list[dict]:
    import urllib.parse
    import requests
    inp = urllib.parse.quote(json.dumps({"0": {"json": {"includeInactive": True}}}))
    try:
        r = requests.get(f"{TRPC}?batch=1&input={inp}",
                         headers={"accept": "application/json", "user-agent": "Mozilla/5.0"},
                         timeout=30)
        r.raise_for_status()
        return r.json()[0]["result"]["data"]["json"]["cardPacks"]
    except Exception as e:
        print(f"[trpc] 取卡機清單失敗：{e}")
        return []


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


TARGET_CACHE = DATA / "live_target.json"


def _read_cache() -> dict:
    try:
        return json.loads(TARGET_CACHE.read_text())
    except Exception:
        return {}


def _write_cache(d: dict):
    try:
        TARGET_CACHE.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    except Exception:
        pass


def pick_target_pool(rpcs: RpcPool) -> dict:
    """回傳 {address, slug, name, active_from}。含探測快取，避免每輪重掃 6000 區塊燒 RPC。"""
    # 1) 明確指定
    explicit = (sys.argv[1] if len(sys.argv) > 1 else "") or os.getenv("POOL_ADDR", "")
    explicit = explicit.strip().lower()
    if explicit.startswith("0x") and len(explicit) == 42:
        return {"address": explicit, "slug": KNOWN_POOLS.get(explicit, "custom"),
                "name": KNOWN_POOLS.get(explicit, explicit[:10]), "active_from": None}

    now = datetime.now(timezone.utc)
    packs = fetch_limited_packs()
    live = [p for p in packs if p.get("packType") == "limited"
            and p.get("stage") not in ("archived",)]

    for p in live:
        af = _parse_iso(p.get("activeFrom"))
        in_window = af is not None and (af - now).total_seconds() <= 1800 \
            and (now - af).total_seconds() <= 14 * 86400
        slug = (p.get("name") or "").lower().replace(" ", "-")
        if not in_window:
            if af and af > now:
                print(f"[target] 即將開放：{p.get('name')} activeFrom={p.get('activeFrom')} "
                      f"(還有 {(af-now).total_seconds()/3600:.1f} 小時)")
            continue

        # 2a) 快取命中（已探測到本檔池位址）→ 直接用，不重掃鏈（省 RPC）
        cache = _read_cache()
        if cache.get("slug") == slug and cache.get("address"):
            return {"address": cache["address"], "slug": slug, "name": p.get("name"),
                    "active_from": p.get("activeFrom"), "notified": cache.get("notified", False)}

        # 2b) 首次：鏈上探測新池
        print(f"[target] 開放窗口內：{p.get('name')} → 鏈上探測池…")
        addr = discover_pool(rpcs)
        if addr:
            _write_cache({"slug": slug, "address": addr, "name": p.get("name"),
                          "active_from": p.get("activeFrom"), "notified": False,
                          "discovered_at": now.isoformat()})
            return {"address": addr, "slug": slug, "name": p.get("name"),
                    "active_from": p.get("activeFrom"), "notified": False}
        print("[target] 尚未偵測到已灌卡的新池（可能還沒灌池）。")

    # 3) 預設 omega（有歷史資料可顯示）
    print("[target] 無開放中限量卡機，回退預設 omega（供介面/測試）。")
    return {"address": "0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910",
            "slug": "omega", "name": "Omega (demo)", "active_from": None}


def notify_capture(target: dict, stats: dict, conn):
    """首次抓到某限量池（loaded>0）時，發 Telegram：地址 + 大獎 + 圖片。"""
    if not target.get("active_from") or target.get("notified"):
        return
    if (stats.get("loaded") or 0) < 20:
        return  # 還沒真正灌池
    pool = target["address"]
    big = conn.execute(
        "SELECT card_name, fmv, token_id FROM tokens WHERE pool=? AND is_big=1 "
        "ORDER BY fmv DESC LIMIT 6", (pool,)).fetchall()
    img = None
    if big:
        try:
            import sqlite3 as _sq
            cdb = _sq.connect(str(ROOT / "data" / "collectiq.db"))
            r = cdb.execute("SELECT image_url FROM tokens WHERE token_id=?",
                            (str(big[0][2]),)).fetchone()
            cdb.close()
            img = r[0] if r and r[0] else None
        except Exception:
            pass
    lines = [f"🎰 *限量卡機池已上鏈*：{target.get('name')}",
             f"`{pool}`",
             f"灌卡 {stats['loaded']} · 在池 {stats['in_pool']} · 已抽 {stats['pulled']} · 買家 {stats['buyers']}"]
    if big:
        lines.append("💎 大獎：" + "，".join(
            f"{(n or '#'+str(t)[-6:])[:32]}{(' $'+format(f,'.0f')) if f else ''}" for n, f, t in big))
    lines.append("→ /live 面板即時追蹤每張卡被哪個錢包抽走")
    from hermes_notify import tg
    if tg("\n".join(lines), image_path=img):
        c = _read_cache()
        c["notified"] = True
        _write_cache(c)
        print("[tg] 已發池上鏈通知")


def discover_pool(rpcs: RpcPool, blocks: int = 6000) -> str | None:
    """掃最近區塊，找被灌大量 from-zero mint 的全新合約 = 新卡池。"""
    latest = rpcs.block_number()
    start = latest - blocks
    import collections
    mint_to = collections.Counter()
    cur = start
    while cur <= latest:
        end = min(cur + 50, latest)
        try:
            logs = rpcs.get_logs({"address": NFT, "topics": [TRANSFER_TOPIC],
                                  "fromBlock": cur, "toBlock": end})
        except Exception:
            cur = end + 1
            continue
        for lg in logs:
            if len(lg["topics"]) != 4:
                continue
            frm = _addr(lg["topics"][1]).lower()
            to = _addr(lg["topics"][2]).lower()
            if frm == ZERO and to not in KNOWN_POOLS and to not in INFRA_ADDRS:
                mint_to[to] += 1
        cur = end + 1
    for addr, n in mint_to.most_common():
        if n < 20:
            break
        try:
            code = rpcs._try_all(lambda w3: w3.eth.get_code(w3.to_checksum_address(addr)))
            if len(code) > 4:
                print(f"[discover] 命中候選池 {addr} (mintIn={n}, is_contract)")
                return addr
        except Exception:
            pass
    return None


# ─── 掃描（聚焦目標池） ────────────────────────────────────────────────────────
def _seed_from_onchain_db(conn: sqlite3.Connection, pool: str) -> int:
    """首次針對某池時，用 onchain_pulls.db 既有紀錄 bootstrap（省去重掃歷史）。回傳最大 block。"""
    if not ONCHAIN_DB.exists():
        return 0
    src = sqlite3.connect(ONCHAIN_DB)
    rows = src.execute(
        "SELECT tx_hash, log_index, token_id, from_addr, to_addr, block_number, block_time, "
        "market_fmv, card_name FROM onchain_pulls WHERE from_addr=? OR to_addr=? "
        "ORDER BY block_number, log_index",
        (pool, pool),
    ).fetchall()
    src.close()
    for tx, li, tid, frm, to, bn, bt, fmv, name in rows:
        _apply_event(conn, pool, bn, li or 0, bt, tid, frm.lower(), to.lower(), tx, name, fmv)
    print(f"[seed] 從 onchain_pulls.db 匯入 {len(rows)} 筆與 {pool[:10]}… 相關的事件")
    return max((r[5] for r in rows), default=0)


def _classify(pool: str, frm: str, to: str) -> str:
    # 灌卡進池：從 0x0 鑄造，或由中央鑄造/中繼合約路由進來
    if to == pool and frm in INFRA_ADDRS:
        return "load"
    # 回收回池：由「真實買家錢包」送回（被抽走後又送回獎池）
    if to == pool and frm not in INFRA_ADDRS:
        return "recycle"
    if frm == pool and to == ZERO:
        return "burn"          # 從池內銷毀
    if frm == pool and to not in (ZERO,):
        return "pull"          # 被抽走
    return "other"


def _apply_event(conn, pool, bn, li, bt, tid, frm, to, tx, name, fmv):
    kind = _classify(pool, frm, to)
    if kind == "other":
        return
    conn.execute(
        "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pool, bn, li, bt, tid, kind, frm, to, tx, name, fmv),
    )
    is_big = 1 if (fmv is not None and fmv >= BIG_FMV) else 0
    if kind == "load":
        conn.execute(
            "INSERT OR REPLACE INTO tokens VALUES (?,?,?,?,?,?,?,?,COALESCE((SELECT recycled FROM tokens WHERE pool=? AND token_id=?),0),?,?)",
            (pool, tid, "in_pool", pool, name, fmv, None, is_big, pool, tid, bn, bt),
        )
    elif kind == "pull":
        conn.execute(
            "INSERT OR REPLACE INTO tokens VALUES (?,?,?,?,?,?,?,?,COALESCE((SELECT recycled FROM tokens WHERE pool=? AND token_id=?),0),?,?)",
            (pool, tid, "pulled", to, name, fmv, None, is_big, pool, tid, bn, bt),
        )
        # 買家錢包（排除基礎設施）
        if to not in INFRA_ADDRS and to not in KNOWN_POOLS:
            conn.execute(
                "INSERT INTO wallets (pool,address,first_seen_block,first_seen_time,pulls) "
                "VALUES (?,?,?,?,1) ON CONFLICT(pool,address) DO UPDATE SET pulls=pulls+1",
                (pool, to, bn, bt),
            )
    elif kind == "recycle":
        conn.execute(
            "INSERT OR REPLACE INTO tokens VALUES (?,?,?,?,?,?,?,?,1,?,?)",
            (pool, tid, "in_pool", pool, name, fmv, None, is_big, bn, bt),
        )
    elif kind == "burn":
        conn.execute(
            "UPDATE tokens SET status='burned', last_block=?, last_time=? WHERE pool=? AND token_id=?",
            (bn, bt, pool, tid),
        )


def scan_forward(conn, rpcs, pool: str, from_block: int, market: dict) -> int:
    """用 topic filter 聚焦掃 from=池 與 to=池 的 Transfer。回傳掃到的最新 block。"""
    latest = rpcs.block_number()
    if from_block <= 0:
        from_block = latest - 6000
    padded = _pad(pool)
    block_ts: dict[int, int] = {}
    new_ev = 0
    for topics in ([TRANSFER_TOPIC, padded], [TRANSFER_TOPIC, None, padded]):
        cur = from_block + 1
        while cur <= latest:
            end = min(cur + 2000, latest)
            try:
                logs = rpcs.get_logs({"address": NFT, "topics": topics,
                                      "fromBlock": cur, "toBlock": end})
            except Exception:
                # 免費節點窄跨度重試
                end = min(cur + 50, latest)
                try:
                    logs = rpcs.get_logs({"address": NFT, "topics": topics,
                                          "fromBlock": cur, "toBlock": end})
                except Exception:
                    cur = end + 1
                    continue
            for lg in logs:
                if len(lg["topics"]) != 4:
                    continue
                frm = _addr(lg["topics"][1]).lower()
                to = _addr(lg["topics"][2]).lower()
                tid = str(int(lg["topics"][3].hex(), 16))
                bn = lg["blockNumber"]
                if bn not in block_ts:
                    block_ts[bn] = rpcs.get_block_ts(bn)
                mk = market.get(tid) or {}
                txh = lg["transactionHash"].hex()
                txh = txh if txh.startswith("0x") else "0x" + txh
                # RAW 核心帳本：全記錄每筆 Transfer（含 reorg 未定案旗標）
                if _CORE is not None:
                    k = _classify(pool, frm, to)
                    _ledger.ingest_transfer(
                        _CORE, txh, lg["logIndex"], bn, _iso(block_ts[bn]),
                        tid, frm, to, pool,
                        k if k != "other" else _ledger.KIND_XFER, latest)
                _apply_event(conn, pool, bn, lg["logIndex"], block_ts[bn], tid, frm, to,
                             txh, mk.get("name"), mk.get("fmv"))
                new_ev += 1
            cur = end + 1
            time.sleep(0.15)
    conn.commit()
    if _CORE is not None:
        promoted = _ledger.finalize_confirmations(_CORE, latest)
        if promoted:
            print(f"[core] 定案升級 {promoted} 筆（confirmed 0→1）")
    return latest, new_ev


# ─── 聚合 + 寫 meta ──────────────────────────────────────────────────────────
def recompute_meta(conn, target: dict, last_block: int):
    pool = target["address"]
    def one(sql, *a):
        return conn.execute(sql, a).fetchone()[0]
    loaded = one("SELECT COUNT(*) FROM events WHERE pool=? AND kind='load'", pool)
    pulled = one("SELECT COUNT(*) FROM tokens WHERE pool=? AND status='pulled'", pool)
    in_pool = one("SELECT COUNT(*) FROM tokens WHERE pool=? AND status='in_pool'", pool)
    burned = one("SELECT COUNT(*) FROM tokens WHERE pool=? AND status='burned'", pool)
    recycled = one("SELECT COUNT(*) FROM tokens WHERE pool=? AND recycled=1", pool)
    buyers = one("SELECT COUNT(DISTINCT address) FROM wallets WHERE pool=?", pool)
    conn.execute(
        "INSERT OR REPLACE INTO pool_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (pool, target.get("slug"), target.get("name"), target.get("active_from"),
         loaded, pulled, recycled, in_pool, burned, buyers, last_block,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return dict(loaded=loaded, pulled=pulled, in_pool=in_pool, burned=burned,
               recycled=recycled, buyers=buyers)


def run_once(conn, rpcs, market, target) -> dict:
    pool = target["address"]
    row = conn.execute("SELECT last_block FROM pool_meta WHERE address=?", (pool,)).fetchone()
    if row and row[0]:
        from_block = int(row[0])
    else:
        from_block = _seed_from_onchain_db(conn, pool)
        conn.commit()
    latest, new_ev = scan_forward(conn, rpcs, pool, from_block, market)
    stats = recompute_meta(conn, target, latest)
    ts = datetime.now().strftime("%F %T")
    print(f"[{ts}] 池 {pool[:10]}… 掃到 {latest} · 本輪新增 {new_ev} · "
          f"灌卡 {stats['loaded']} 在池 {stats['in_pool']} 已抽 {stats['pulled']} "
          f"回收 {stats['recycled']} 銷毀 {stats['burned']} 買家 {stats['buyers']}")
    return stats


def _in_open_window(target) -> bool:
    """開放前 30 分 ~ 開放後 3 天 → 高頻掃描窗口。"""
    af = _parse_iso(target.get("active_from"))
    if not af:
        return False
    now = datetime.now(timezone.utc)
    return (af - now).total_seconds() <= 1800 and (now - af).total_seconds() <= 3 * 86400


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    rpcs = RpcPool()
    conn = _conn()
    market = _load_market()

    target = pick_target_pool(rpcs)
    print(f"[monitor] 目標池 {target['address']} ({target.get('name')})")

    fast = _in_open_window(target) or os.getenv("FAST_LOOP") == "1"
    if fast:
        # 開放窗口內：內部每 30 秒續掃約 110 秒（配合 launchd 120s → 近乎連續 30s 更新）
        print("[monitor] 開放窗口 → 高頻模式（30s×~4 輪）")
        deadline = time.time() + 110
        while True:
            stats = run_once(conn, rpcs, market, target)
            notify_capture(target, stats, conn)
            if time.time() >= deadline:
                break
            time.sleep(30)
    else:
        stats = run_once(conn, rpcs, market, target)
        notify_capture(target, stats, conn)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
