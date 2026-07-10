#!/usr/bin/env python3
"""
Real-time capture + analysis of the "pool wallet" for a new limited card machine.

Background: a new Renaiss limited machine (e.g. Bowtie Pack) first shows a countdown
on the official site; only before pulls open (activeFrom) does it deploy the pool
contract on-chain and load in the cards. Key point: the Renaiss API's
vendingMachineAddress is always null (even for archived limited machines), so the pool
contract "can only be found on-chain" -- and that's exactly the "on-chain before the
announcement" opportunity we exploit.

On-chain signature of a new pool: around when pulls open, a big batch of mints
(from=0x0) is loaded into a brand-new contract address (~= the whole pool's card
count), and then that address starts Transferring to buyers (pulls).

This script:
  1) calls Renaiss tRPC cardPack.getAll and picks machines with packType=limited that
     are near/past activeFrom (only scans the chain within the pull window, to avoid
     empty scans).
  2) scans recent blocks for a "brand-new contract loaded with many mints" = the new
     pool, and remembers its address.
  3) runs wallet analysis on that pool: number of cards loaded, distinct buyers, top
     whale wallets, number of tokens pulled.
  4) writes the pool block of data/new_pack_bowtie.json + data/new_packs_watch.json;
     sends Telegram the first time the pool is captured on-chain.

The schedule (launchd ai.renaiss.newpool, every 15 min) captures this automatically around when pulls open.
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
TRANSFER_TOPIC = None  # computed later (needs web3)
ZERO = "0x0000000000000000000000000000000000000000"


REST_PACKS = "https://api.renaiss.xyz/v0/packs"  # fallback: hit this REST endpoint when tRPC 500s
_HDRS = {"accept": "application/json", "user-agent": "Mozilla/5.0"}


def _fetch_trpc() -> list[dict]:
    import urllib.parse
    inp = urllib.parse.quote(json.dumps({"0": {"json": {"includeInactive": True}}}))
    r = requests.get(f"{TRPC}?batch=1&input={inp}", headers=_HDRS, timeout=30)
    r.raise_for_status()
    return r.json()[0]["result"]["data"]["json"]["cardPacks"]


def _fetch_rest() -> list[dict]:
    """Fallback source: REST v0/packs returns the same raw fields (packType/stage/priceInUsdt/
    expectedValueInUsd). Downside: activeFrom may be None (main() backfills it from cache)."""
    r = requests.get(REST_PACKS, params={"includeInactive": "true"}, headers=_HDRS, timeout=30)
    r.raise_for_status()
    return r.json().get("cardPacks", [])


def fetch_packs() -> list[dict]:
    """Retry tRPC first (it occasionally 500s the instant pulls open); only fall back to REST after repeated failures, and raise only if both are down."""
    last = None
    for attempt in range(3):
        try:
            return _fetch_trpc()
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    # tRPC failed all 3 times -> switch to the REST fallback, to avoid an empty round / missing the pull opening
    try:
        packs = _fetch_rest()
        print(f"  ⚠️ tRPC getAll 失敗（{type(last).__name__}），已改用 REST 備援（{len(packs)} 台）")
        return packs
    except Exception as e2:
        raise RuntimeError(f"tRPC 與 REST 皆失敗：trpc={last!r} rest={e2!r}")


KNOWN_POOLS = {
    "0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910",  # omega
    "0xfda4a907d23d9f24271bc47483c5b983831e325e",  # eden
    "0xb2891022648c5fad3721c42c05d8d283d4d53080",  # renacrypt
    "0xaab5f5fa75437a6e9e7004c12c9c56cda4b4885a",  # legacy/costume (old)
    "0x5bbd2f57fe5b4d74ef704436fee5d5175e609079",  # omega router (not a pool, excluded)
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
    """Scan recent blocks for a brand-new contract loaded with many mints (from=0x0) = a new card pool."""
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
    mint_to = collections.Counter()   # 0x0 -> X (loaded with cards)
    dist_from = collections.defaultdict(set)  # X -> {buyers}
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

    # Candidates: loaded with many mints and not in the known list
    cands = []
    for addr, n in mint_to.most_common():
        if addr in KNOWN_POOLS or addr == ZERO:
            continue
        # Is it a contract?
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
    """Scan the most recent `blocks` blocks and tally the pool contract's load + pull wallet distribution."""
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
    buyers = collections.Counter()   # pool -> buyers (pulls)
    minted_in = 0                    # 0x0 -> pool (loads)
    loaded_from = collections.Counter()
    pulled_tokens = set()
    minted_tokens = set()            # loaded into the pool but not yet pulled = still in the pool
    pulled_detail = []               # [(token_id, buyer, block)] for each pulled card
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
    """token_id -> {name, fmv, tier?} taken from data/marketplace_all.json (Renaiss FMV)."""
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
    """Map a single fmv back to TOP/S/A/B/C/D using the pool's tier price bands (a fallback when tier is missing)."""
    if fmv is None or not tiers:
        return None
    for t in ["TOP", "S", "A", "B", "C", "D"]:
        x = tiers.get(t)
        if x and x.get("min") is not None and x.get("max") is not None:
            if x["min"] <= fmv <= x["max"]:
                return t
    return None


def classify_pulls(pool: dict, tiers: dict | None) -> dict:
    """Map pulled tokens back to cards (name/FMV/tier) and find the big prizes already pulled.

    Source priority: marketplace_all.json (token_id->FMV). New cards may not be listed on
    the marketplace yet, in which case the token_id won't match -> mark unknown, filled in
    automatically once sync_renaiss_marketplace catches up.
    """
    market = _load_market_by_token()
    big = []          # big prizes already pulled (TOP/S or high FMV)
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

    # Big prizes remaining in the pool (TOP/S not yet pulled) -> still to look forward to
    remaining_big = []
    for tid in pool.get("remaining_tokens", []):
        mk = market.get(tid) or {}
        fmv = mk.get("fmv")
        tier = mk.get("tier") or _tier_of(fmv, tiers)
        if tier in ("TOP", "S") or (fmv is not None and fmv >= 300):
            remaining_big.append({"token_id": tid, "name": mk.get("name") or f"#{tid}",
                                  "fmv": fmv, "tier": tier})
    remaining_big.sort(key=lambda r: (r["fmv"] or 0), reverse=True)

    # Estimated EV of the remaining pool (average over remaining cards with known FMV)
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
    # Only care about the most recent limited machine currently opening / about to open (packType=limited, stage not archived)
    live = [p for p in packs if p.get("packType") == "limited"
            and p.get("stage") not in ("archived",)]
    for p in live:
        slug = (p.get("name") or "").lower().replace(" ", "-")
        # The REST fallback lacks activeFrom -> backfill from last round's cache so the pull-window check still works
        if not p.get("activeFrom") and (prev.get(slug) or {}).get("active_from"):
            p["activeFrom"] = prev[slug]["active_from"]
        af = _parse_iso(p.get("activeFrom"))
        # Pull window: from 30 min before activeFrom to 14 days after, only then scan the chain (to avoid empty scans)
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
                # Merge into the bowtie scrape file
                bpath = DATA / "new_pack_bowtie.json"
                base = json.loads(bpath.read_text()) if bpath.exists() else {}
                # Map pulled tokens back to cards; find the big prizes already pulled / still in the pool
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
                # Only send Telegram on the first capture
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
