#!/usr/bin/env python3
"""
grab_pack_contents.py — grab the "card machine content catalog" (the full pool
list, available even before pulls open).

Background:
  The Renaiss /gacha/{slug} page uses the tRPC `cardPack.getContent` endpoint
  (packId + tiers) to dump everything a card machine "claims to contain" in one
  shot — even countdown machines (limited packs not yet open, not yet on-chain).
  Each card carries: name / image / tier / year / Renaiss buyback base price,
  and the image URL embeds the grading cert (PSA/BGS) — the universal key to
  real market prices.

So this file can land the full pool table "before any announcement, before it
hits the chain," and then:
  1) use the cert against the Index API / external sources to fill in real
     market prices -> rank by luck value (treasure cards);
  2) after pulls open on-chain, watch_new_pool grabs the pool address and links
     item_id <-> token_id.

Usage:
  python3 grab_pack_contents.py            # grab every card machine
  python3 grab_pack_contents.py <packId>   # grab a single card machine only
  python3 grab_pack_contents.py --gems     # print the current treasure-card ranking (needs prices filled in)
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))          # renaiss_api.py lives at the project root
import ledger  # noqa: E402

TRPC = "https://www.renaiss.xyz/api/trpc"
# Browser-like headers to reduce the chance of being blocked by the WAF / risk
# controls (only sends fields a normal browser would send).
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "referer": "https://www.renaiss.xyz/",
    "origin": "https://www.renaiss.xyz",
    "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
}
TIERS = ["TOP", "S", "A", "B", "C", "D"]
# Image URLs look like .../pokemon-cards/PSA136046059/card.jpg -> take the folder name as the cert
CERT_RE = re.compile(r"/([A-Za-z]{2,5}\d{4,})/[^/]+\.(?:jpg|jpeg|png|webp)", re.I)

# Global polite throttling: all outbound requests share a single Session and add
# a jittered delay between requests, to avoid bursty traffic that could get us
# rate-limited / blocked by the site.
_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)


def _polite_sleep(base: float = 0.8, jitter: float = 0.6) -> None:
    time.sleep(base + random.random() * jitter)


def _get(proc: str, payload: dict, retries: int = 5) -> dict:
    """Call Renaiss tRPC; on 429/5xx retry with exponential backoff + jitter, honoring Retry-After."""
    inp = urllib.parse.quote(json.dumps({"0": {"json": payload}}))
    url = f"{TRPC}/{proc}?batch=1&input={inp}"
    last = None
    for i in range(retries):
        try:
            r = _SESSION.get(url, timeout=45)
            if r.status_code in (403, 429) or r.status_code >= 500:
                ra = r.headers.get("retry-after")
                wait = float(ra) if (ra and ra.isdigit()) else (2 ** i + random.random() * 2)
                last = f"HTTP {r.status_code}"
                time.sleep(min(wait, 30)); continue
            r.raise_for_status()
            return r.json()[0]["result"]["data"]["json"]
        except Exception as e:
            last = e; time.sleep(min(2 ** i + random.random() * 2, 30))
    raise RuntimeError(f"{proc} failed: {last}")


def list_packs() -> list[dict]:
    """All card machines (including archived); returns id / name / packType / stage."""
    data = _get("cardPack.getAll", {"includeInactive": True})
    return data.get("cardPacks", [])


def _cert_from_url(url: str) -> str | None:
    if not url:
        return None
    m = CERT_RE.search(url)
    return m.group(1).upper() if m else None


def grab_pack(core, pack: dict) -> dict:
    """Grab a single card machine's full pool table into pack_content (without overwriting filled-in prices/token_id)."""
    pid = pack.get("id")
    if not pid:
        return {"cards": 0, "skipped": "no id"}
    name = pack.get("name")
    stage = pack.get("stage")
    now = datetime.now(timezone.utc).isoformat()
    try:
        content = _get("cardPack.getContent", {"packId": pid, "tiers": TIERS})
    except Exception as e:
        return {"cards": 0, "error": str(e)}
    tiers = content.get("tiers", {}) or {}

    payload = []
    tier_counts: dict[str, int] = {}
    for tier, blob in tiers.items():
        cards = (blob or {}).get("cards", []) or []
        tier_counts[tier] = len(cards)
        for c in cards:
            entry_id = c.get("id")          # unique key for the pull slot
            if not entry_id:
                continue
            item_id = c.get("itemId")        # physical card (can repeat across slots)
            cert = _cert_from_url(c.get("frontImageUrl"))
            try:
                # Despite the name, buybackBaseValueInUSD is actually in cents (and often a string).
                # Divide by 100 to get dollars, matching the market price (get_graded is already in
                # dollars) so luck comes out correct.
                buyback = float(c.get("buybackBaseValueInUSD") or 0) / 100 or None
            except (TypeError, ValueError):
                buyback = None
            payload.append((
                pid, entry_id, item_id, name, stage, tier, c.get("name"), cert,
                c.get("gradingCompany"), c.get("grade"), c.get("year"),
                c.get("frontImageUrl"), buyback, now, now,
            ))

    # UPSERT: refresh catalog fields but preserve filled-in market_price_usd / luck_value / token_id
    core.executemany("""
        INSERT INTO pack_content
          (pack_id,entry_id,item_id,pack_name,pack_stage,tier,name,cert,grader,
           grade,year,image_url,renaiss_buyback_usd,captured_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pack_id,entry_id) DO UPDATE SET
          item_id=excluded.item_id,
          pack_name=excluded.pack_name, pack_stage=excluded.pack_stage,
          tier=excluded.tier, name=excluded.name, cert=excluded.cert,
          grader=excluded.grader, grade=excluded.grade, year=excluded.year,
          image_url=excluded.image_url,
          renaiss_buyback_usd=excluded.renaiss_buyback_usd,
          updated_at=excluded.updated_at
    """, payload)
    core.commit()
    return {"cards": len(payload), "tier_counts": tier_counts}


def grab_all(core, only: str | None = None) -> dict:
    packs = list_packs()
    if only:
        packs = [p for p in packs if p.get("id") == only]
    total = 0
    out = []
    for p in packs:
        res = grab_pack(core, p)
        n = res.get("cards", 0)
        total += n
        tc = res.get("tier_counts", {})
        tcs = " ".join(f"{t}{tc[t]}" for t in TIERS if tc.get(t))
        flag = res.get("error") or res.get("skipped") or ""
        print(f"  [{p.get('stage'):9}] {str(p.get('name'))[:34]:34} "
              f"{n:>5} cards  {tcs}  {flag}")
        out.append({"pack_id": p.get("id"), "name": p.get("name"),
                    "stage": p.get("stage"), **res})
        _polite_sleep()
    core.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('pack_content_grabbed_at',?)",
                 (datetime.now(timezone.utc).isoformat(),))
    core.commit()
    return {"packs": len(packs), "cards": total, "detail": out}


def enrich_market(core, limit: int = 90, pack_id: str | None = None) -> dict:
    """Use the grading cert against the Index API to fill in real market prices, writing back market_price_usd / luck_value.

    The Index API allows 100 anonymous requests per day, so:
      - dedupe by DISTINCT cert (query the same physical card once even across many slots);
      - priority order: countdown machines > higher tiers (TOP>S>A>B>C) > higher official buyback;
      - stop on consecutive 429s (quota exhausted); whatever was filled still lands.
    One fill updates "all slots with the same cert," so the weighted EV benefits too.
    """
    import renaiss_api as api
    now = datetime.now(timezone.utc).isoformat()
    where = "WHERE cert IS NOT NULL AND market_price_usd IS NULL"
    params: list = []
    if pack_id:
        where += " AND pack_id=?"
        params.append(pack_id)
    rows = core.execute(f"""
        SELECT cert,
               MIN(renaiss_buyback_usd) AS rb,
               MIN(CASE tier WHEN 'TOP' THEN 0 WHEN 'S' THEN 1 WHEN 'A' THEN 2
                             WHEN 'B' THEN 3 WHEN 'C' THEN 4 ELSE 5 END) AS trank,
               MAX(CASE pack_stage WHEN 'countdown' THEN 1 ELSE 0 END) AS live
        FROM pack_content {where}
        GROUP BY cert
        ORDER BY live DESC, trank ASC, rb DESC
        LIMIT ?
    """, (*params, limit)).fetchall()

    ok = miss = quota = 0
    consecutive_fail = 0
    for (cert, rb, _tr, _lv) in rows:
        try:
            g = api.get_graded(cert)
            price = (g.get("card") or {}).get("price_usd")
        except Exception:
            price = None
        if price is None:
            # Distinguish quota exhausted vs. card not found: roughly judged by consecutive failures
            miss += 1
            consecutive_fail += 1
            if consecutive_fail >= 8:
                quota = 1
                break
            _polite_sleep(0.5, 0.5)
            continue
        consecutive_fail = 0
        # luck_value must be computed per-slot: the same cert has different official
        # buyback prices across tiers/machines, so overwriting the whole batch with a
        # single cert-level ratio would pollute it (a C-slot $80 ratio clobbering a
        # B-slot $121). The market price is shared across every physical card and the
        # same per slot, so writing market_price_usd at the cert level is fine.
        # Label the source honestly: the Renaiss Index is Renaiss's own pricing, not an
        # independent third-party sale price.
        core.execute("""
            UPDATE pack_content
            SET market_price_usd=?,
                luck_value = CASE WHEN renaiss_buyback_usd > 0
                                  THEN ROUND(? / renaiss_buyback_usd, 4) END,
                market_source='renaiss_index', market_url=NULL, updated_at=?
            WHERE cert=?""", (price, price, now, cert))
        ok += 1
        _polite_sleep(0.5, 0.5)   # slow down for the Index API too, to avoid tripping risk controls
    core.commit()
    return {"enriched": ok, "missed": miss, "quota_hit": bool(quota),
            "candidates": len(rows)}


def enrich_independent(core, limit: int = 200, pack_id: str | None = None,
                       only_missing: bool = True) -> dict:
    """Fill in market prices from an "independent third-party sale price" -- PriceCharting
    (aggregating completed eBay auctions, split by grading level). This is the
    "credible + ordinary-buyer auction price" source the user actually wants, distinct
    from Renaiss's own Index (renaiss_index), and it answers "is the luck value fake?"

    Market-source priority: pricecharting_ebay (independent) > renaiss_index (Renaiss's own).
    So by default only_missing=True fills only cards that "have no market price yet"; an
    independent source can always override a non-independent one, but here we're conservative
    and don't auto-override, leaving that decision to the caller.

    PriceCharting is a public site with no per-day hard cap like the Index API's 100, but we
    still throttle politely. When there's no match (promo cards / obscure Japanese sets) we
    leave it None and never fabricate.
    """
    import our_price
    now = datetime.now(timezone.utc).isoformat()
    src_filter = "AND market_price_usd IS NULL" if only_missing \
        else "AND (market_source IS NULL OR market_source!='pricecharting_ebay')"
    where = f"WHERE cert IS NOT NULL {src_filter}"
    params: list = []
    if pack_id:
        where += " AND pack_id=?"
        params.append(pack_id)
    rows = core.execute(f"""
        SELECT cert,
               MIN(name)                AS name,
               MIN(grader)              AS grader,
               MIN(grade)               AS grade,
               MIN(renaiss_buyback_usd) AS rb,
               MIN(CASE tier WHEN 'TOP' THEN 0 WHEN 'S' THEN 1 WHEN 'A' THEN 2
                             WHEN 'B' THEN 3 WHEN 'C' THEN 4 ELSE 5 END) AS trank,
               MAX(CASE pack_stage WHEN 'countdown' THEN 1 ELSE 0 END)  AS live
        FROM pack_content {where}
        GROUP BY cert
        ORDER BY live DESC, trank ASC, rb DESC
        LIMIT ?
    """, (*params, limit)).fetchall()

    chk = our_price.OurPriceChecker(throttle=1.2)
    ok = miss = mismatch = 0
    total = len(rows)
    for idx, (cert, name, grader, grade, rb, _tr, _live) in enumerate(rows, 1):
        card = {"name": name, "grader": grader or "PSA", "grade": grade}
        try:
            res = chk.get_independent_price(card)
        except Exception as e:
            print(f"[independent] {cert} exception: {e}", flush=True)
            res = {}
        price = res.get("our_price")
        if price is None:
            miss += 1
            if res.get("title_mismatch"):
                mismatch += 1
        else:
            # Non-PSA grader: convert PriceCharting's PSA base price into that grader's
            # equivalent market price (e.g. BGS 10 ~= PSA 10 x 0.75). If no factor is
            # available, keep the original price.
            if (grader or "PSA").upper() != "PSA" and res.get("grade_matched") == "PSA 10":
                price = our_price.grader_convert(
                    price, "psa", 10.0, to_grader=(grader or "").lower()) or price
            price = round(float(price), 2)
            # luck_value computed per-slot (see the same note in enrich_market): the same
            # cert has different official buyback prices across tiers, so a single ratio
            # can't overwrite the whole batch; the market price is shared per-slot, hence
            # a cert-level write.
            core.execute("""
                UPDATE pack_content
                SET market_price_usd=?,
                    luck_value = CASE WHEN renaiss_buyback_usd > 0
                                      THEN ROUND(? / renaiss_buyback_usd, 4) END,
                    market_source='pricecharting_ebay', market_url=?, updated_at=?
                WHERE cert=?""", (price, price, res.get("source_url"), now, cert))
            ok += 1
        # Land every 15 cards: progress is visible, the run is resumable, and an interruption doesn't lose the whole batch.
        if idx % 15 == 0:
            core.commit()
            try:
                chk.save_cache()
            except Exception:
                pass
            print(f"[independent] {idx}/{total} · hits {ok} misses {miss}"
                  f" (false matches blocked {mismatch})", flush=True)
    core.commit()
    try:
        chk.save_cache()
    except Exception:
        pass
    return {"enriched": ok, "missed": miss, "title_mismatch": mismatch,
            "candidates": total}


def show_gems(core, limit: int = 30) -> None:
    """Treasure-card ranking under currently known market prices (luck value = market price / official buyback)."""
    rows = core.execute("""
        SELECT pack_name, tier, name, cert, renaiss_buyback_usd,
               market_price_usd, luck_value
        FROM pack_content
        WHERE luck_value IS NOT NULL
        ORDER BY luck_value DESC LIMIT ?
    """, (limit,)).fetchall()
    if not rows:
        print("No market price data yet (market_price_usd all empty) -- fill real market prices via cert first.")
        n = core.execute("SELECT COUNT(*), COUNT(cert) FROM pack_content").fetchone()
        print(f"pack_content currently has {n[0]} cards, of which {n[1]} have a grading cert to match against the market.")
        return
    print(f"{'Luck':>5}  {'Buyback':>10}  {'Market':>10}  Grade Name")
    for (pn, tier, name, cert, rb, mp, lv) in rows:
        print(f"{lv:>5.2f}  ${rb or 0:>9,.0f}  ${mp or 0:>9,.0f}  [{tier}] {str(name)[:50]}")


def main() -> int:
    core = ledger.init_db()
    args = sys.argv[1:]
    if args and args[0] == "--gems":
        show_gems(core)
        return 0
    if args and args[0] == "--enrich":
        limit = int(args[1]) if len(args) > 1 else 90
        st = enrich_market(core, limit)
        print(f"[enrich] filled market prices {st['enriched']}/{st['candidates']} unique cards"
              f" (missed {st['missed']}"
              f"{', quota exhausted' if st['quota_hit'] else ''})")
        show_gems(core)
        return 0
    if args and args[0] == "--independent":
        # Fill in market prices from independent third-party sale prices (PriceCharting/eBay).
        # Optional limit and pack_id: --independent 200 <packId>
        limit = int(args[1]) if len(args) > 1 else 200
        pid = args[2] if len(args) > 2 else None
        st = enrich_independent(core, limit, pid)
        print(f"[independent] filled independent market prices {st['enriched']}/{st['candidates']} unique cards"
              f" (missed {st['missed']}, of which suspected same-number false matches blocked {st['title_mismatch']})")
        show_gems(core)
        return 0
    if args and args[0] == "--daily":
        # Scheduled entry point: first refresh the full machine catalog (Renaiss tRPC,
        # no daily cap), then fill market prices preferring "independent sale prices"
        # (PriceCharting/eBay, a public site with no daily hard limit), and finally use
        # the day's remaining Index quota (renaiss_index, conservatively 90) to fill the
        # gaps the independent source couldn't find.
        limit = int(args[1]) if len(args) > 1 else 90
        indep_limit = int(args[2]) if len(args) > 2 else 250
        t0 = time.time()
        gs = grab_all(core)
        ei = enrich_independent(core, indep_limit)
        es = enrich_market(core, limit)
        n, ncert, nmkt = core.execute(
            "SELECT COUNT(*), COUNT(cert), COUNT(market_price_usd) FROM pack_content"
        ).fetchone()
        nindep = core.execute(
            "SELECT COUNT(*) FROM pack_content WHERE market_source='pricecharting_ebay'"
        ).fetchone()[0]
        print(f"[daily] {datetime.now(timezone.utc):%F %T}Z "
              f"catalog {gs['packs']} machines/{gs['cards']} cards · "
              f"independent filled {ei['enriched']} (missed {ei['missed']}) · "
              f"Index filled {es['enriched']} (missed {es['missed']}"
              f"{', quota exhausted' if es['quota_hit'] else ''}) · "
              f"cumulative market prices {nmkt}/{ncert} (independent {nindep})({time.time()-t0:.1f}s)")
        return 0
    only = args[0] if args else None
    t0 = time.time()
    st = grab_all(core, only)
    print(f"[grab] {datetime.now(timezone.utc):%F %T}Z {st['packs']} pack machines · "
          f"{st['cards']} cards stored ({time.time()-t0:.1f}s)")
    # Catalog stats
    n, ncert, nmkt = core.execute(
        "SELECT COUNT(*), COUNT(cert), COUNT(market_price_usd) FROM pack_content"
    ).fetchone()
    print(f"[grab] pack_content total {n} cards · with cert {ncert} · market prices filled {nmkt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
