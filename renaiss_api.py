#!/usr/bin/env python3
"""
renaiss_api.py — Renaiss 平台 API + Index API 統一封裝層

兩個後端：
  RENAISS_BASE  = https://api.renaiss.xyz/v0  (卡機 / 市場 / 持有資料)
  INDEX_BASE    = https://api.renaissos.com/v1 (卡片指數 / 定價 / 成交紀錄)

快取：同一 key 5 分鐘內不重抓。
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

import requests

# ─── 設定 ────────────────────────────────────────────────────────────────────
RENAISS_BASE = "https://api.renaiss.xyz/v0"
INDEX_BASE   = "https://api.renaissos.com/v1"
CACHE_TTL    = 300   # 秒
TIMEOUT      = 20    # 秒

HEADERS = {"Accept": "application/json", "User-Agent": "renaiss-ev-monitor/2.0"}

# ─── 快取 ─────────────────────────────────────────────────────────────────────
_cache: Dict[str, dict] = {}


def _cached(key: str, fn, ttl: int = CACHE_TTL):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < ttl:
        return entry["data"]
    data = fn()
    _cache[key] = {"ts": time.time(), "data": data}
    return data


def _get(base: str, path: str, params: dict = None) -> dict:
    """HTTP GET with cache."""
    params = params or {}
    key = f"{base}{path}?{'&'.join(f'{k}={v}' for k,v in sorted(params.items()))}"

    def fetch():
        try:
            r = requests.get(f"{base}{path}", params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[renaiss_api] GET {base}{path} failed: {e}")
            return {}

    return _cached(key, fetch)


# ═══════════════════════════════════════════════════════════════════════════════
#  RENAISS PLATFORM API  (api.renaiss.xyz/v0)
# ═══════════════════════════════════════════════════════════════════════════════

def get_packs(include_inactive: bool = False) -> List[dict]:
    """所有卡機清單（slug / name / priceInUsdt / expectedValueInUsd / featuredCardFmvInUsd）。

    已知卡機總覽（2026-07-01）：
      Active perpetual : eden-pack ($150), omega ($48), renacrypt-pack ($88)
      Archived limited : bowtie/ribbon/plasma/starry/magma/costume/legacy-7,8,9/aura/destiny
      Coming soon      : world-cup-pack (網站顯示，API 尚未上線)

    注意：archived 限量包的 /v0/packs/{slug} 端點回傳 404，
          只有 ?includeInactive=true 清單有基本資訊（無開包記錄）。
    """
    import re as _re
    params = {"includeInactive": "true"} if include_inactive else {}
    data = _get(RENAISS_BASE, "/packs", params)
    packs = data.get("cardPacks", [])
    for p in packs:
        p["price_usd"]       = int(p.get("priceInUsdt", 0) or 0) / 1e18
        p["official_ev_usd"] = float(p.get("expectedValueInUsd", 0) or 0)
        p["featured_fmv_usd"]= float(p.get("featuredCardFmvInUsd", 0) or 0)
        # 從 description 解析供應量（limited pack 描述含 "1,000 real graded cards"）
        desc = p.get("description", "")
        m = _re.search(r'(\d[\d,]+)\s*(packs?|graded cards?)', desc, _re.I)
        p["supply_hint"] = int(m.group(1).replace(",", "")) if m else None
        p["ev_ratio"] = round(p["official_ev_usd"] / p["price_usd"], 1) if p["price_usd"] else None

    # 補上官網顯示但 API 尚未上線的限量包
    COMING_SOON = [
        {
            "slug": "world-cup-pack", "name": "World Cup Pack",
            "packType": "limited", "stage": "coming_soon",
            "price_usd": None, "official_ev_usd": None, "featured_fmv_usd": 3800.0,
            "supply_hint": 1000, "ev_ratio": None,
            "description": "Football/soccer-themed limited release. 1,000 real graded cards. Top prize ~$3,800.",
            "author": "Renaiss",
        }
    ]
    if include_inactive:
        packs = packs + COMING_SOON

    return packs


def get_pack_detail(slug: str) -> dict:
    """單一卡機詳情，含 recentOpenedPacks（最近開包記錄，有 tier + fmv）。"""
    data = _get(RENAISS_BASE, f"/packs/{slug}")
    pack = data.get("cardPack", {})
    if pack:
        pack["price_usd"]        = int(pack.get("priceInUsdt", 0) or 0) / 1e18
        pack["official_ev_usd"]  = float(pack.get("expectedValueInUsd", 0) or 0)
        pack["featured_fmv_usd"] = float(pack.get("featuredCardFmvInUsd", 0) or 0)
    return pack


def get_marketplace(
    limit: int = 100,
    listed_only: bool = True,
    category: Optional[str] = None,
    sort: str = "listDate",
    order: str = "desc",
    offset: int = 0,
) -> dict:
    """
    市場掛單列表。回傳 {"collection": [...], "pagination": {...}}.
    每張卡含 tokenId / name / fmvPriceInUSD / askPriceInUSDT / ownerAddress 等。
    """
    params: dict = {
        "limit": limit,
        "sort":  sort,
        "order": order,
        "offset": offset,
    }
    if listed_only:
        params["listedOnly"] = "true"
    if category:
        params["category"] = category
    data = _get(RENAISS_BASE, "/marketplace", params)
    rows = data.get("collection", [])
    for r in rows:
        r["fmv"]       = float(r.get("fmvPriceInUSD") or 0)
        r["ask_price"] = int(r.get("askPriceInUSDT") or 0) / 1e18
    return {"collection": rows, "pagination": data.get("pagination", {})}


def get_card_detail(token_id: str) -> dict:
    """單張卡片詳情（by tokenId）。"""
    data = _get(RENAISS_BASE, f"/cards/{token_id}")
    return data


def compute_pack_ev(slug: str) -> dict:
    """
    對單一卡機計算：
      official_ev   = 平台公布 expectedValueInUsd
      empirical_ev  = 最近開包 FMV 的平均值（最近 N 筆）
      ev_ratio      = official_ev / price（>1 → 值回票價）
      ev_delta_pct  = (empirical_ev - official_ev) / official_ev × 100
    """
    p = get_pack_detail(slug)
    if not p:
        return {"slug": slug, "error": "pack not found"}

    price    = p.get("price_usd", 0)
    official = p.get("official_ev_usd", 0)
    recent   = p.get("recentOpenedPacks", [])

    if recent:
        fmvs = [float(r.get("fmv", 0)) for r in recent if r.get("fmv") is not None]
        empirical = round(sum(fmvs) / len(fmvs), 2) if fmvs else None
    else:
        empirical = None

    ev_delta = None
    if empirical is not None and official:
        ev_delta = round((empirical - official) / official * 100, 1)

    tier_dist: Dict[str, int] = {}
    for r in recent:
        t = r.get("tier", "unknown")
        tier_dist[t] = tier_dist.get(t, 0) + 1

    return {
        "slug":              slug,
        "name":              p.get("name", slug),
        "pack_type":         p.get("packType"),
        "stage":             p.get("stage"),
        "price_usd":         price,
        "official_ev_usd":   official,
        "empirical_ev_usd":  empirical,
        "ev_ratio":          round(official / price, 2) if price else None,
        "ev_delta_pct":      ev_delta,
        "recent_pulls_n":    len(recent),
        "tier_distribution": tier_dist,
        "featured_fmv_usd":  p.get("featured_fmv_usd", 0),
    }


def get_all_packs_ev() -> List[dict]:
    """所有卡機的 EV 分析（list，供 dashboard 直接用）。"""
    packs = get_packs()
    results = []
    for p in packs:
        slug = p.get("slug", "")
        ev = compute_pack_ev(slug)
        # 補齊舊 dashboard 需要的欄位
        ev["pack_name"]               = p.get("name", slug)
        ev["price"]                   = p.get("price_usd", 0)
        ev["optimized_ev"]            = ev.get("official_ev_usd")
        ev["ev_improvement"]          = ev.get("ev_delta_pct")
        ev["remaining_percent"]       = None
        ev["top_prize_remaining_prob"]= None
        results.append(ev)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  INDEX API  (api.renaissos.com/v1)
# ═══════════════════════════════════════════════════════════════════════════════

def search_cards(q: str, limit: int = 10) -> List[dict]:
    """全文搜尋卡片名稱，回傳含 priceUsdCents / grade / spark 的 list。"""
    data = _get(INDEX_BASE, "/search", {"q": q, "limit": limit})
    results = data.get("results", [])
    for r in results:
        r["price_usd"] = r.get("priceUsdCents", 0) / 100
    return results


def get_card_by_renaiss_id(rid: str) -> dict:
    """用 Renaiss tokenId 查 Index API，回傳 card detail + priceUsdCents。"""
    data = _get(INDEX_BASE, f"/cards/by-renaiss-id/{rid}")
    if data.get("priceUsdCents"):
        data["price_usd"] = data["priceUsdCents"] / 100
    return data


def get_card_fmv_series(rid: str) -> List[dict]:
    """查某張卡的每日 FMV 歷史（Index API by-renaiss-id）。"""
    data = _get(INDEX_BASE, f"/cards/by-renaiss-id/{rid}/fmv-series")
    return data.get("series", data if isinstance(data, list) else [])


def get_graded(cert: str) -> dict:
    """用分級序號（如 PSA138947522）查 Index API，回傳 grade + card + price。"""
    data = _get(INDEX_BASE, f"/graded/{cert}")
    card = data.get("card", {})
    if card.get("priceUsdCents"):
        card["price_usd"] = card["priceUsdCents"] / 100
    return data


def get_recent_trades(limit: int = 50) -> List[dict]:
    """最近跨平台成交紀錄（snkrdunk / 其他來源）。"""
    data = _get(INDEX_BASE, "/trades/recent", {"limit": limit})
    trades = data.get("trades", [])
    for t in trades:
        t["price_usd"] = t.get("priceUsdCents", 0) / 100
    return trades


def get_index_overview(game: str = "pokemon") -> dict:
    """Index 指數總覽（game = pokemon / one-piece）。"""
    return _get(INDEX_BASE, f"/indices/{game}")


# ─── 快取工具 ─────────────────────────────────────────────────────────────────

def clear_cache():
    """手動清快取。"""
    _cache.clear()


def cache_stats() -> dict:
    now = time.time()
    return {
        k: {"age_s": round(now - v["ts"]), "expired": (now - v["ts"]) > CACHE_TTL}
        for k, v in _cache.items()
    }


# ─── 快速測試 ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import pprint
    print("=== Packs ===")
    for p in get_packs():
        print(f"  {p['slug']}: ${p['price_usd']:.0f}  official_ev=${p['official_ev_usd']:.0f}")

    print("\n=== Omega EV ===")
    pprint.pprint(compute_pack_ev("omega"))

    print("\n=== Marketplace (3) ===")
    mkt = get_marketplace(limit=3)
    for r in mkt["collection"]:
        print(f"  {r['name'][:60]}  fmv=${r['fmv']}  ask=${r['ask_price']:.0f}")

    print("\n=== Search: charizard ===")
    for h in search_cards("charizard", limit=3):
        print(f"  {h['name']} / {h.get('gradeLabel')} / ${h['price_usd']:.2f}")

    print("\n=== Recent Trades (3) ===")
    for t in get_recent_trades(3):
        card = t.get("card", {})
        print(f"  {card.get('name','?')} / ${t['price_usd']:.2f} @ {t.get('displayName','?')}")
