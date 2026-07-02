#!/usr/bin/env python3
"""
新限量卡機「卡池錢包」即時捕捉 + 分析。

背景：Renaiss 新限量卡機（如 Bowtie Pack）會先在官方站 countdown，開抽
(activeFrom) 前才把卡池合約部署上鏈並灌入卡片。重點：Renaiss API 的
vendingMachineAddress 永遠是 null（連已封存的限量卡機也是），所以卡池合約
「只能在鏈上找」——這正是「公告前先上鏈」的可利用點。

新卡池的鏈上特徵：開抽前後，會有一大批 mint（from=0x0）灌進一個全新的合約
位址（≈整池張數），接著該位址開始 Transfer 給買家（抽卡）。

本腳本：
  1) 打 Renaiss tRPC cardPack.getAll，挑出 packType=limited 且接近/已過
     activeFrom 的卡機（開抽窗口內才掃鏈，避免空掃）。
  2) 掃最近區塊找「被灌大量 mint 的全新合約」= 新卡池，記住它的位址。
  3) 對該卡池做錢包分析：灌卡張數、不同買家數、前幾大鯨魚錢包、已抽 token 數。
  4) 寫 data/new_pack_bowtie.json 的 pool 區塊 + data/new_packs_watch.json；
     首次抓到卡池上鏈時發 Telegram。

排程（launchd ai.renaiss.newpool 每 15 分）會在開抽前後自動捕捉。
"""
from __future__ import annotations

import json
import os
import sys
import time
import collections
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
TRPC = "https://www.renaiss.xyz/api/trpc/cardPack.getAll"
NFT = "0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30"
TRANSFER_TOPIC = None  # 延後算（需 web3）
ZERO = "0x0000000000000000000000000000000000000000"


def fetch_packs() -> list[dict]:
    import urllib.parse
    inp = urllib.parse.quote(json.dumps({"0": {"json": {"includeInactive": True}}}))
    r = requests.get(f"{TRPC}?batch=1&input={inp}",
                     headers={"accept": "application/json", "user-agent": "Mozilla/5.0"},
                     timeout=30)
    r.raise_for_status()
    return r.json()[0]["result"]["data"]["json"]["cardPacks"]


KNOWN_POOLS = {
    "0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910",  # omega
    "0xfda4a907d23d9f24271bc47483c5b983831e325e",  # eden
    "0xb2891022648c5fad3721c42c05d8d283d4d53080",  # renacrypt
    "0xaab5f5fa75437a6e9e7004c12c9c56cda4b4885a",  # legacy/costume(舊)
    "0x5bbd2f57fe5b4d74ef704436fee5d5175e609079",  # omega router (非池，排除)
}


def _rpc_clients():
    from web3 import Web3
    rpcs = [r for r in [os.getenv("BNB_RPC", "").strip(),
            "https://bsc.publicnode.com", "https://bsc-rpc.publicnode.com",
            "https://1rpc.io/bnb", "https://bsc.drpc.org"] if r]
    clients = []
    for rpc in rpcs:
        try:
            w = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
            _ = w.eth.block_number
            clients.append(w)
        except Exception:
            pass
    return clients


def discover_new_pool(blocks: int = 5000) -> dict:
    """掃最近區塊，找出被灌大量 mint（from=0x0）的全新合約 = 新卡池。"""
    from web3 import Web3
    clients = _rpc_clients()
    if not clients:
        return {"error": "no RPC"}
    topic = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().lstrip("0x")
    nft = Web3.to_checksum_address(NFT)
    idx = 0

    def get_logs(p):
        nonlocal idx
        last = None
        for _ in range(len(clients)):
            try:
                return clients[idx].eth.get_logs(p)
            except Exception as e:
                last = e; idx = (idx + 1) % len(clients)
        raise last

    def a(t):
        h = t.hex(); h = h if h.startswith("0x") else "0x" + h
        return "0x" + h[-40:]

    latest = clients[idx].eth.block_number
    start = latest - blocks
    mint_to = collections.Counter()   # 0x0 -> X（被灌卡）
    dist_from = collections.defaultdict(set)  # X -> {買家}
    cur = start
    while cur <= latest:
        end = min(cur + 50, latest)
        try:
            logs = get_logs({"address": nft, "topics": [topic],
                             "fromBlock": cur, "toBlock": end})
        except Exception:
            cur = end + 1; continue
        for lg in logs:
            if len(lg["topics"]) != 4:
                continue
            frm = a(lg["topics"][1]).lower(); to = a(lg["topics"][2]).lower()
            if frm == ZERO:
                mint_to[to] += 1
            elif frm not in (ZERO,):
                dist_from[frm].add(to)
        cur = end + 1

    # 候選：被灌很多 mint，且不在已知名單
    cands = []
    for addr, n in mint_to.most_common():
        if addr in KNOWN_POOLS or addr == ZERO:
            continue
        # 是合約嗎？
        try:
            is_contract = len(clients[idx].eth.get_code(Web3.to_checksum_address(addr)).hex()) > 4
        except Exception:
            is_contract = False
        cands.append({"address": addr, "minted_in": n,
                      "distributes_to": len(dist_from.get(addr, set())),
                      "is_contract": is_contract})
    cands.sort(key=lambda c: -(c["minted_in"] + c["distributes_to"]))
    return {"scanned_blocks": f"{start}..{latest}", "candidates": cands[:8]}


def analyze_pool(addr: str, blocks: int = 4000) -> dict:
    """掃最近 blocks 個區塊，統計卡池合約的灌卡 + 抽卡錢包分佈。"""
    from web3 import Web3
    topic = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().lstrip("0x")
    nft = Web3.to_checksum_address(NFT)
    target = addr.lower()
    rpcs = [r for r in [os.getenv("BNB_RPC", "").strip(),
            "https://bsc.publicnode.com", "https://bsc-rpc.publicnode.com",
            "https://1rpc.io/bnb", "https://bsc.drpc.org"] if r]
    clients = []
    for rpc in rpcs:
        try:
            w = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
            _ = w.eth.block_number
            clients.append(w)
        except Exception:
            pass
    if not clients:
        return {"error": "no RPC"}
    idx = 0

    def get_logs(p):
        nonlocal idx
        last = None
        for _ in range(len(clients)):
            try:
                return clients[idx].eth.get_logs(p)
            except Exception as e:
                last = e; idx = (idx + 1) % len(clients)
        raise last

    def a(t):
        h = t.hex(); h = h if h.startswith("0x") else "0x" + h
        return "0x" + h[-40:]

    latest = clients[idx].eth.block_number
    start = latest - blocks
    buyers = collections.Counter()   # 池 -> 買家（抽卡）
    minted_in = 0                    # 0x0 -> 池（灌卡）
    loaded_from = collections.Counter()
    pulled_tokens = set()
    minted_tokens = set()            # 灌進池但尚未抽走 = 仍在池內
    pulled_detail = []               # [(token_id, buyer, block)] 抽出的每張
    first_block = last_block = None
    cur = start
    while cur <= latest:
        end = min(cur + 50, latest)
        try:
            logs = get_logs({"address": nft, "topics": [topic],
                             "fromBlock": cur, "toBlock": end})
        except Exception:
            cur = end + 1; continue
        for lg in logs:
            if len(lg["topics"]) != 4:
                continue
            frm = a(lg["topics"][1]).lower(); to = a(lg["topics"][2]).lower()
            tid = str(int(lg["topics"][3].hex(), 16))
            if to == target:
                if frm == ZERO:
                    minted_in += 1
                    minted_tokens.add(tid)
                loaded_from[frm] += 1
                first_block = first_block or lg["blockNumber"]
                last_block = lg["blockNumber"]
            if frm == target:
                buyers[to] += 1
                pulled_tokens.add(tid)
                pulled_detail.append((tid, to, lg["blockNumber"]))
                first_block = first_block or lg["blockNumber"]
                last_block = lg["blockNumber"]
        cur = end + 1

    remaining_tokens = sorted(minted_tokens - pulled_tokens)
    return {
        "pool_address": addr,
        "scanned_blocks": f"{start}..{latest}",
        "minted_into_pool": minted_in,
        "loaded_from_top": loaded_from.most_common(5),
        "pulls_total": sum(buyers.values()),
        "distinct_buyers": len(buyers),
        "top_buyers": buyers.most_common(10),
        "distinct_pulled_tokens": len(pulled_tokens),
        "pulled_detail": pulled_detail,
        "remaining_tokens": remaining_tokens,
        "remaining_in_pool": len(remaining_tokens),
        "activity_block_range": [first_block, last_block],
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_market_by_token() -> dict:
    """token_id -> {name, fmv, tier?} 取自 data/marketplace_all.json（Renaiss FMV）。"""
    mp = DATA / "marketplace_all.json"
    out = {}
    if not mp.exists():
        return out
    try:
        for c in json.loads(mp.read_text()):
            tid = str(c.get("token_id") or "")
            if tid:
                out[tid] = {"name": c.get("name"), "fmv": c.get("fmv"),
                            "tier": c.get("tier") or c.get("rarity")}
    except Exception:
        pass
    return out


def _tier_of(fmv, tiers: dict | None):
    """用卡池分級的價格區間，把單張 fmv 對應回 TOP/S/A/B/C/D（缺 tier 時的後援）。"""
    if fmv is None or not tiers:
        return None
    for t in ["TOP", "S", "A", "B", "C", "D"]:
        x = tiers.get(t)
        if x and x.get("min") is not None and x.get("max") is not None:
            if x["min"] <= fmv <= x["max"]:
                return t
    return None


def classify_pulls(pool: dict, tiers: dict | None) -> dict:
    """把已抽出的 token 對應回卡片（名稱/FMV/分級），找出已被抽走的大獎。

    來源優先：marketplace_all.json（token_id→FMV）。新卡可能尚未上架 marketplace，
    屆時 token_id 對不到 → 標 unknown，待 sync_renaiss_marketplace 補齊後自動補上。
    """
    market = _load_market_by_token()
    big = []          # 已抽出的大獎（TOP/S 或 FMV 高）
    pulled_known = []
    for tid, buyer, blk in pool.get("pulled_detail", []):
        mk = market.get(tid) or {}
        fmv = mk.get("fmv")
        tier = mk.get("tier") or _tier_of(fmv, tiers)
        rec = {"token_id": tid, "name": mk.get("name") or f"#{tid}",
               "fmv": fmv, "tier": tier, "buyer": buyer, "block": blk}
        pulled_known.append(rec)
        if tier in ("TOP", "S") or (fmv is not None and fmv >= 300):
            big.append(rec)
    big.sort(key=lambda r: (r["fmv"] or 0), reverse=True)

    # 剩餘池的大獎（還沒被抽走的 TOP/S）→ 仍可期待
    remaining_big = []
    for tid in pool.get("remaining_tokens", []):
        mk = market.get(tid) or {}
        fmv = mk.get("fmv")
        tier = mk.get("tier") or _tier_of(fmv, tiers)
        if tier in ("TOP", "S") or (fmv is not None and fmv >= 300):
            remaining_big.append({"token_id": tid, "name": mk.get("name") or f"#{tid}",
                                  "fmv": fmv, "tier": tier})
    remaining_big.sort(key=lambda r: (r["fmv"] or 0), reverse=True)

    # 剩餘池估算 EV（已知 FMV 的剩餘卡平均）
    rem_fmvs = [r["fmv"] for r in remaining_big if r["fmv"]]
    return {
        "big_prizes_pulled": big,
        "big_prizes_remaining": remaining_big,
        "n_pulled_classified": len([r for r in pulled_known if r["fmv"] is not None]),
        "n_pulled_unknown": len([r for r in pulled_known if r["fmv"] is None]),
        "classified_at": datetime.now(timezone.utc).isoformat(),
    }


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    packs = fetch_packs()
    now = datetime.now(timezone.utc)
    prev = {}
    wpath = DATA / "new_packs_watch.json"
    if wpath.exists():
        try:
            prev = {p["slug"]: p for p in json.loads(wpath.read_text()).get("packs", [])}
        except Exception:
            prev = {}

    watched = []
    # 只關注最近一檔開抽中/即將開抽的限量卡機（packType=limited, stage 非 archived）
    live = [p for p in packs if p.get("packType") == "limited"
            and p.get("stage") not in ("archived",)]
    for p in live:
        slug = (p.get("name") or "").lower().replace(" ", "-")
        af = _parse_iso(p.get("activeFrom"))
        # 開抽窗口：activeFrom 前 30 分 ~ 之後 14 天，才掃鏈（避免空掃）
        in_window = af is not None and (af - now).total_seconds() <= 1800 \
            and (now - af).total_seconds() <= 14 * 86400
        cached_pool = (prev.get(slug) or {}).get("pool_address")
        rec = {"name": p.get("name"), "slug": slug, "stage": p.get("stage"),
               "active_from": p.get("activeFrom"), "pool_address": cached_pool,
               "official_ev_usd": p.get("expectedValueInUsd"),
               "price_usd": float(p.get("priceInUsdt", 0)) / 1e18}
        print(f"  {p.get('name')} stage={p.get('stage')} activeFrom={p.get('activeFrom')} "
              f"window={in_window} pool={cached_pool or '未知'}")

        if in_window:
            pool_addr = cached_pool
            if not pool_addr:
                print("  🔍 開抽窗口內，鏈上探測新卡池（找被灌 mint 的全新合約）…")
                disc = discover_new_pool()
                rec["discovery"] = disc
                top = next((c for c in disc.get("candidates", [])
                            if c.get("is_contract") and c["minted_in"] >= 20), None)
                if top:
                    pool_addr = top["address"]
                    print(f"  🎯 命中候選卡池 {pool_addr} (mintIn={top['minted_in']})")
                else:
                    print("  …尚未發現足量灌卡的新合約（可能還沒灌池）。候選："
                          + ", ".join(f"{c['address'][:10]}…({c['minted_in']})"
                                      for c in disc.get("candidates", [])[:3]))
            if pool_addr:
                print(f"  🔗 卡池錢包分析 {pool_addr} …")
                pool = analyze_pool(pool_addr)
                rec["pool_address"] = pool_addr
                rec["pool"] = pool
                # 合併進 bowtie scrape 檔
                bpath = DATA / "new_pack_bowtie.json"
                base = json.loads(bpath.read_text()) if bpath.exists() else {}
                # 把已抽出的 token 對應回卡片，找出已被抽走 / 仍在池內的大獎
                bigp = classify_pulls(pool, base.get("tiers"))
                rec["big_prizes_pulled"] = bigp["big_prizes_pulled"]
                if base.get("slug") == slug or slug.startswith(base.get("slug", "###")):
                    base["vending_machine_address"] = pool_addr
                    base["pool"] = pool
                    base["big_prizes"] = bigp
                    base["pool_captured_at"] = now.isoformat()
                    bpath.write_text(json.dumps(base, ensure_ascii=False, indent=2))
                np_big = len(bigp["big_prizes_pulled"])
                print(f"  ✓ 灌卡 {pool.get('minted_into_pool')} · 抽卡 {pool.get('pulls_total')} "
                      f"· 買家 {pool.get('distinct_buyers')} · 已抽出大獎 {np_big} "
                      f"(剩 {len(bigp['big_prizes_remaining'])})")
                # 首次抓到才發 Telegram
                if not cached_pool:
                    try:
                        try:
                            sys.path.insert(0, str(Path(__file__).resolve().parent))
                            from hermes_notify import load_env as _le
                            _le()
                        except Exception:
                            pass
                        from main import TelegramAlert
                        tg = TelegramAlert()
                        if tg.is_configured():
                            bp = bigp["big_prizes_pulled"][:5]
                            bp_txt = ("\n🎁 已抽出大獎：" + "，".join(
                                f"{r['name']}{('（'+r['tier']+'）') if r.get('tier') else ''}"
                                f"{(' $'+format(r['fmv'],'.0f')) if r.get('fmv') else ''}"
                                for r in bp)) if bp else "\n🎁 尚無大獎被抽出"
                            tg.send_alert(
                                f"🎰 *新限量卡機卡池上鏈*：{p.get('name')}\n"
                                f"`{pool_addr}`\n灌卡 {pool.get('minted_into_pool')} 張 · "
                                f"已抽 {pool.get('pulls_total')} · 買家 "
                                f"{pool.get('distinct_buyers')} 個錢包" + bp_txt)
                            print("  📨 已發 Telegram")
                    except Exception as e:
                        print(f"  ⚠️ 通知失敗：{e}")
        watched.append(rec)

    wpath.write_text(json.dumps({"checked_at": now.isoformat(), "packs": watched},
                                ensure_ascii=False, indent=2))
    print(f"[{now:%F %T}Z] 監看 {len(watched)} 個（非封存）限量卡機")
    return 0


if __name__ == "__main__":
    sys.exit(main())
