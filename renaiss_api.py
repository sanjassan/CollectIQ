#!/usr/bin/env python3
"""
renaiss_api.py -- unified wrapper for the Renaiss platform API + Index API.

Two backends:
  RENAISS_BASE  = https://api.renaiss.xyz/v0  (packs / marketplace / holdings)
  INDEX_BASE    = https://api.renaissos.com/v1 (card indices / pricing / trade records)

Caching: the same key is not re-fetched within 5 minutes.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

import requests

# ─── Configuration ───────────────────────────────────────────────────────────
RENAISS_BASE = "https://api.renaiss.xyz/v0"
INDEX_BASE   = "https://api.renaissos.com/v1"
CACHE_TTL    = 300   # seconds (platform API)
INDEX_TTL    = 900   # seconds (Index API 429s more often and changes slowly -> longer cache)
TIMEOUT      = 20    # seconds
MAX_RETRIES  = 3     # 429/5xx retry count
BACKOFF_BASE = 1.5   # seconds, exponential backoff base

HEADERS = {"Accept": "application/json", "User-Agent": "renaiss-ev-monitor/2.0"}


def _retry_after(resp) -> Optional[float]:
    """Parse the Retry-After header (seconds format); returns None if non-numeric or missing."""
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return min(float(val), 30.0)   # cap at 30s so a single fetch doesn't take too long
    except ValueError:
        return None

# ─── Cache ────────────────────────────────────────────────────────────────────
_cache: Dict[str, dict] = {}


def _cached(key: str, fn, ttl: int = CACHE_TTL):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < ttl:
        return entry["data"]
    data = fn()
    _cache[key] = {"ts": time.time(), "data": data}
    return data


def _get(base: str, path: str, params: dict = None) -> dict:
    """HTTP GET with cache.

    The Index API (api.renaissos.com) occasionally returns 429/5xx; this
    retries those statuses with exponential backoff, and if all attempts fail,
    falls back to the last successful cached value (even if stale) to avoid a
    fully broken page."""
    params = params or {}
    key = f"{base}{path}?{'&'.join(f'{k}={v}' for k,v in sorted(params.items()))}"

    def fetch():
        for attempt in range(MAX_RETRIES):
            try:
                r = requests.get(f"{base}{path}", params=params, headers=HEADERS, timeout=TIMEOUT)
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = _retry_after(r) or BACKOFF_BASE * (2 ** attempt)
                    print(f"[renaiss_api] {r.status_code} on {path}, retry {attempt+1}/{MAX_RETRIES} in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                if attempt == MAX_RETRIES - 1:
                    print(f"[renaiss_api] GET {base}{path} failed after {MAX_RETRIES} tries: {e}")
                    break
                time.sleep(BACKOFF_BASE * (2 ** attempt))
        # All attempts failed: if a stale cache exists, an old value beats a blank
        stale = _cache.get(key)
        if stale:
            print(f"[renaiss_api] serving stale cache for {path} (age {round(time.time()-stale['ts'])}s)")
            return stale["data"]
        return {}

    ttl = INDEX_TTL if base == INDEX_BASE else CACHE_TTL
    return _cached(key, fetch, ttl=ttl)


# ═══════════════════════════════════════════════════════════════════════════════
#  RENAISS PLATFORM API  (api.renaiss.xyz/v0)
# ═══════════════════════════════════════════════════════════════════════════════

def get_packs(include_inactive: bool = False) -> List[dict]:
    """List of all packs (slug / name / priceInUsdt / expectedValueInUsd / featuredCardFmvInUsd).

    Known packs overview (2026-07-03):
      Active perpetual : eden-pack ($150), omega ($48), renacrypt-pack ($88)
      Archived limited : bowtie/ribbon/plasma/starry/magma/costume/legacy-7,8,9/aura/destiny
      Live limited     : world-cup-pack (live; returned natively by ?includeInactive=true)

    Note: the /v0/packs/{slug} endpoint returns 404 for archived limited packs;
          only the ?includeInactive=true list has basic info (no pull records).
    """
    import re as _re
    params = {"includeInactive": "true"} if include_inactive else {}
    data = _get(RENAISS_BASE, "/packs", params)
    packs = data.get("cardPacks", [])
    for p in packs:
        p["price_usd"]       = int(p.get("priceInUsdt", 0) or 0) / 1e18
        # Although expectedValueInUsd / featuredCardFmvInUsd are named "InUsd",
        # they are actually in cents (across all 15 packs, EV/100 = 1.03-1.08x
        # the price; not dividing by 100 yields absurd 100x values).
        p["official_ev_usd"] = float(p.get("expectedValueInUsd", 0) or 0) / 100
        p["featured_fmv_usd"]= float(p.get("featuredCardFmvInUsd", 0) or 0) / 100
        # Parse supply from the description (limited pack descriptions contain "1,000 real graded cards")
        desc = p.get("description", "")
        m = _re.search(r'(\d[\d,]+)\s*(packs?|graded cards?)', desc, _re.I)
        p["supply_hint"] = int(m.group(1).replace(",", "")) if m else None
        p["ev_ratio"] = round(p["official_ev_usd"] / p["price_usd"], 1) if p["price_usd"] else None

    # Limited packs shown on the site but not yet live in the API go here as a
    # fallback (deduplicated against the native list). world-cup-pack is now
    # live natively in the API, so it was removed; this list is currently empty.
    COMING_SOON: List[dict] = []
    if include_inactive and COMING_SOON:
        have = {(p.get("slug") or p.get("name")) for p in packs}
        packs = packs + [c for c in COMING_SOON
                         if c["slug"] not in have and c["name"] not in have]

    return packs


def get_pack_detail(slug: str) -> dict:
    """Detail for a single pack, including recentOpenedPacks (recent pull records with tier + fmv)."""
    data = _get(RENAISS_BASE, f"/packs/{slug}")
    pack = data.get("cardPack", {})
    if pack:
        pack["price_usd"]        = int(pack.get("priceInUsdt", 0) or 0) / 1e18
        # Same as get_packs: these two fields are actually in cents, divide by 100 to restore USD.
        pack["official_ev_usd"]  = float(pack.get("expectedValueInUsd", 0) or 0) / 100
        pack["featured_fmv_usd"] = float(pack.get("featuredCardFmvInUsd", 0) or 0) / 100
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
    Marketplace listing list. Returns {"collection": [...], "pagination": {...}}.
    Each card includes tokenId / name / fmvPriceInUSD / askPriceInUSDT / ownerAddress, etc.
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
    """Detail for a single card (by tokenId)."""
    data = _get(RENAISS_BASE, f"/cards/{token_id}")
    return data


def compute_pack_ev(slug: str) -> dict:
    """
    Compute for a single pack:
      official_ev   = platform-published expectedValueInUsd
      empirical_ev  = average FMV of recent pulls (most recent N records)
      ev_ratio      = official_ev / price (>1 -> worth the price)
      ev_delta_pct  = (empirical_ev - official_ev) / official_ev × 100
    """
    p = get_pack_detail(slug)
    if not p:
        return {"slug": slug, "error": "pack not found"}

    price    = p.get("price_usd", 0)
    official = p.get("official_ev_usd", 0)
    recent   = p.get("recentOpenedPacks", [])

    if recent:
        # recentOpenedPacks[].fmv, like expectedValueInUsd, is in cents; divide
        # by 100 to restore USD (otherwise empirical_ev / ev_delta_pct inflate 100x).
        fmvs = [float(r.get("fmv", 0)) / 100 for r in recent if r.get("fmv") is not None]
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
    """EV analysis for all packs (a list, for direct dashboard use)."""
    packs = get_packs()
    results = []
    for p in packs:
        slug = p.get("slug", "")
        ev = compute_pack_ev(slug)
        # Fill in the fields the old dashboard needs
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
    """Full-text search card names; returns a list including priceUsdCents / grade / spark."""
    data = _get(INDEX_BASE, "/search", {"q": q, "limit": limit})
    results = data.get("results", [])
    for r in results:
        r["price_usd"] = r.get("priceUsdCents", 0) / 100
    return results


def get_card_by_renaiss_id(rid: str) -> dict:
    """Query the Index API by Renaiss tokenId; returns card detail + priceUsdCents."""
    data = _get(INDEX_BASE, f"/cards/by-renaiss-id/{rid}")
    if data.get("priceUsdCents"):
        data["price_usd"] = data["priceUsdCents"] / 100
    return data


def get_card_fmv_series(rid: str) -> List[dict]:
    """Query a card's daily FMV history (Index API by-renaiss-id)."""
    data = _get(INDEX_BASE, f"/cards/by-renaiss-id/{rid}/fmv-series")
    return data.get("series", data if isinstance(data, list) else [])


def get_graded(cert: str) -> dict:
    """Query the Index API by grading serial (e.g. PSA138947522); returns grade + card + price."""
    data = _get(INDEX_BASE, f"/graded/{cert}")
    card = data.get("card", {})
    if card.get("priceUsdCents"):
        card["price_usd"] = card["priceUsdCents"] / 100
    return data


def get_recent_trades(limit: int = 50) -> List[dict]:
    """Recent cross-platform trade records (snkrdunk / other sources)."""
    data = _get(INDEX_BASE, "/trades/recent", {"limit": limit})
    trades = data.get("trades", [])
    for t in trades:
        t["price_usd"] = t.get("priceUsdCents", 0) / 100
    return trades


def get_index_overview(game: str = "pokemon") -> dict:
    """Index overview (game = pokemon / one-piece)."""
    return _get(INDEX_BASE, f"/indices/{game}")


# ─── Cache utilities ──────────────────────────────────────────────────────────

def clear_cache():
    """Manually clear the cache."""
    _cache.clear()


def cache_stats() -> dict:
    now = time.time()
    return {
        k: {"age_s": round(now - v["ts"]), "expired": (now - v["ts"]) > CACHE_TTL}
        for k, v in _cache.items()
    }


# ─── Quick test ───────────────────────────────────────────────────────────────
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
