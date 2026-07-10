#!/usr/bin/env python3
"""
CollectIQ — build_universe.py
================================
Merge all data sources into a single SQLite "universe database":
  1. marketplace_all.json   → Renaiss FMV / ask / image / cert
  2. holdings.json          → on-chain holder
  3. Index API /v1/graded   → real market pricing + cert images + price history

Output: data/collectiq.db
Each run only fills in certs not yet queried against the Index API (resumable).

Usage:
  python3 scripts/build_universe.py                # normal run
  python3 scripts/build_universe.py --reset        # wipe and rebuild
  python3 scripts/build_universe.py --limit 100   # only the first 100 rows (testing)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import requests

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA   = os.path.join(BASE, "data")
DB_PATH= os.path.join(DATA, "collectiq.db")

MKT_PATH      = os.path.join(DATA, "marketplace_all.json")
HOLDINGS_PATH = os.path.join(DATA, "holdings.json")

INDEX_BASE    = "https://api.renaissos.com/v1"
RENAISS_BASE  = "https://api.renaiss.xyz/v0"
DELAY         = 10.0   # seconds: 10s is safe to avoid 429 (Index API ~6 req/min limit)
HEADERS       = {"Accept": "application/json", "User-Agent": "collectiq/1.0"}

# ─── Schema ───────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    token_id        TEXT PRIMARY KEY,
    name            TEXT,
    set_name        TEXT,
    card_number     TEXT,
    character_name  TEXT,
    serial          TEXT,
    grader          TEXT,
    grade           TEXT,
    renaiss_fmv     REAL,
    ask_price       REAL,
    is_listed       INTEGER,
    image_url       TEXT,
    holder          TEXT,
    holder_short    TEXT,
    -- Index API 查詢結果
    index_price_usd REAL,
    index_confidence TEXT,
    index_delta_pct  REAL,
    index_last_sale  TEXT,
    index_spark      TEXT,      -- JSON
    cert_front       TEXT,
    cert_back        TEXT,
    cert_item        TEXT,
    index_href       TEXT,
    index_queried    INTEGER DEFAULT 0,  -- 0=未查 1=已查
    -- 衍生欄位
    fmv_gap_pct     REAL,   -- (renaiss_fmv - index_price) / index_price × 100
    fmv_gap_usd     REAL,   -- renaiss_fmv - index_price
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_serial  ON tokens(serial);
CREATE INDEX IF NOT EXISTS idx_holder  ON tokens(holder);
CREATE INDEX IF NOT EXISTS idx_fmv_gap ON tokens(fmv_gap_pct);
CREATE INDEX IF NOT EXISTS idx_renaiss_fmv ON tokens(renaiss_fmv);

CREATE TABLE IF NOT EXISTS fmv_history (
    serial          TEXT,
    date            TEXT,
    price_usd       REAL,
    PRIMARY KEY (serial, date)
);

CREATE TABLE IF NOT EXISTS wallet_summary (
    holder          TEXT PRIMARY KEY,
    token_count     INTEGER,
    total_renaiss_fmv REAL,
    total_index_fmv   REAL,
    avg_fmv_gap_pct   REAL,
    top_card_name     TEXT,
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


# ─── DB helpers ───────────────────────────────────────────────────────────────
def open_db() -> sqlite3.Connection:
    os.makedirs(DATA, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ─── Ingest helpers ───────────────────────────────────────────────────────────
def load_marketplace(conn: sqlite3.Connection):
    """Load marketplace_all.json → tokens table."""
    if not os.path.exists(MKT_PATH):
        print(f"[WARN] {MKT_PATH} not found"); return
    rows = json.load(open(MKT_PATH, encoding="utf-8"))
    print(f"[load] marketplace_all: {len(rows)} tokens")
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        conn.execute("""
            INSERT OR IGNORE INTO tokens
            (token_id, name, set_name, card_number, character_name,
             serial, grader, grade, renaiss_fmv, ask_price, is_listed,
             image_url, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            str(r.get("token_id","")),
            r.get("name",""),
            r.get("set_name",""),
            r.get("card_number",""),
            r.get("character_name",""),
            r.get("serial",""),
            r.get("grader",""),
            r.get("grade",""),
            r.get("fmv"),
            r.get("ask_price"),
            int(bool(r.get("is_listed"))),
            r.get("image_url",""),
            now,
        ))
    conn.commit()
    print(f"[load] inserted / merged marketplace tokens")


def load_holdings(conn: sqlite3.Connection):
    """Backfill the holder column into tokens from holdings.json."""
    if not os.path.exists(HOLDINGS_PATH):
        print(f"[WARN] {HOLDINGS_PATH} not found"); return
    data  = json.load(open(HOLDINGS_PATH, encoding="utf-8"))
    holdings = data.get("holdings", [])
    print(f"[load] holdings: {len(holdings)} entries")
    for h in holdings:
        tid = str(h.get("token_id",""))
        conn.execute("""
            UPDATE tokens SET holder=?, holder_short=?,
            renaiss_fmv=COALESCE(renaiss_fmv, ?),
            image_url=COALESCE(NULLIF(image_url,''), ?)
            WHERE token_id=?
        """, (
            h.get("holder",""),
            h.get("holder_short",""),
            h.get("fmv"),
            h.get("image_url",""),
            tid,
        ))
    conn.commit()
    print(f"[load] holder data merged")


def _api_get_safe(url: str, params: dict = None) -> dict:
    """GET with 429 backoff retry (up to 5 attempts, starting at 30s)."""
    _429_hit = False
    for attempt in range(5):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                _429_hit = True
                # Honour Retry-After header if present (Index API daily quota)
                retry_after = r.headers.get("Retry-After")
                remaining   = r.headers.get("X-RateLimit-Remaining", "?")
                reset_ts    = r.headers.get("X-RateLimit-Reset", "?")
                if retry_after and int(retry_after) > 300:
                    # Daily quota exhausted — no point retrying for hours
                    from datetime import datetime, timezone as _tz
                    try:
                        reset_human = datetime.fromtimestamp(int(reset_ts), tz=_tz.utc).strftime("%H:%M UTC")
                    except Exception:
                        reset_human = "?"
                    print(f"[429] daily quota exhausted (remaining={remaining}, resets {reset_human}). Stopping.")
                    return {"_quota_exhausted": True}
                wait = 30 * (2 ** attempt)   # 30 → 60 → 120 → 240 → 480
                print(f"[429] → wait {wait}s (attempt {attempt+1}/5, remaining={remaining})")
                time.sleep(wait)
                continue
            if r.status_code == 404:
                return {"found": False}
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError:
            return {}
        except Exception as e:
            print(f"[WARN] {url}: {e}")
            return {}
    # All 5 attempts failed — signal caller so it can skip without marking queried
    print(f"[SKIP] all retries exhausted for {url}")
    return {"_rate_limited": True}


def _extract_search_key(row: sqlite3.Row) -> str:
    """
    Build the best search string from the token's columns.
    Prefer character_name + set abbreviation + grade_label;
    otherwise fall back to the first 50 characters of name.
    """
    char = row["character_name"] or ""
    grade = row["grade"] or ""
    grader = row["grader"] or ""
    grade_label = f"{grader} {grade.split()[0]}" if grader and grade else ""  # e.g. "PSA 10"
    name = row["name"] or ""
    # Extract year
    import re
    yr = re.search(r'\b(20\d{2})\b', name)
    year = yr.group(1) if yr else ""
    # Build query: character year grade
    if char and grade_label:
        q = f"{char} {grade_label}"
        if year:
            q += f" {year}"
        return q[:80]
    return name[:60]


def fetch_index_for_token(row: sqlite3.Row) -> dict:
    """
    Query the Index API — search-first strategy.
    Searching directly by character name + grade gives far better coverage than /graded cert lookup.
    Avoids hitting the cert API an extra time per card, which would trigger rate limits.
    """
    cert = row["serial"] or ""

    # Search-first: name search (broad, high coverage)
    q = _extract_search_key(row)
    if not q or len(q) < 4:
        return {}
    resp = _api_get_safe(f"{INDEX_BASE}/search", {"q": q, "limit": 3})
    results = resp.get("results", [])
    if not results:
        return {}
    # Take the first result (best match) and wrap it into a graded-response-like format
    hit = results[0]
    return {
        "found": True,
        "cert": cert,
        "card": {
            "name":         hit.get("name",""),
            "setName":      hit.get("setName",""),
            "company":      hit.get("company",""),
            "grade":        hit.get("grade",""),
            "gradeLabel":   hit.get("gradeLabel",""),
            "priceUsdCents":hit.get("priceUsdCents",0),
            "deltaPct":     hit.get("deltaPct"),
            "confidence":   hit.get("confidence","low"),
            "lastSaleAt":   hit.get("lastSaleAt",""),
            "spark":        hit.get("spark",[]),
            "href":         hit.get("href",""),
            "imageUrl":     hit.get("imageUrl",""),
            "imageUrlThumb":hit.get("imageUrlThumb",""),
        },
        "certImages": {
            "front": hit.get("imageUrl",""),
            "back":  "",
            "item":  hit.get("imageUrl",""),
        },
        "_source": "search",
    }


def enrich_from_index(conn: sqlite3.Connection, limit: int = 0):
    """
    Query the Index API for every token with index_queried=0,
    write the real market price + cert images back to the DB, and compute fmv_gap.
    """
    # Daily quota of 95 (leaving a 5 buffer); process high-value cards first by descending renaiss_fmv
    DAILY_QUOTA = int(os.environ.get("INDEX_DAILY_QUOTA", 95))
    q = """SELECT token_id, serial, renaiss_fmv, name, character_name, grade, grader
           FROM tokens WHERE index_queried=0
           ORDER BY COALESCE(renaiss_fmv, 0) DESC"""
    if limit:
        q += f" LIMIT {limit}"
    else:
        q += f" LIMIT {DAILY_QUOTA}"
    todo = conn.execute(q).fetchall()
    total = len(todo)
    pending_total = conn.execute("SELECT COUNT(*) FROM tokens WHERE index_queried=0").fetchone()[0]
    print(f"[enrich] daily quota={DAILY_QUOTA}  this run={total}  total_pending={pending_total}")
    print(f"[enrich] est days to complete: {pending_total // DAILY_QUOTA + 1}")

    for i, row in enumerate(todo):
        tid   = row["token_id"]
        r_fmv = row["renaiss_fmv"] or 0

        data = fetch_index_for_token(row)

        # Daily quota exhausted — stop immediately, resume tomorrow
        if data.get("_quota_exhausted"):
            conn.commit()
            priced = conn.execute("SELECT COUNT(*) FROM tokens WHERE index_price_usd IS NOT NULL").fetchone()[0]
            print(f"[enrich] quota exhausted at token {i+1}/{total}. Committed. priced={priced}. Exiting.")
            break

        # All retries hit 429 (transient) — skip, leave index_queried=0 for retry
        if data.get("_rate_limited"):
            print(f"  [SKIP transient-429] tok={str(tid)[:8]}")
            time.sleep(DELAY)
            continue

        card = data.get("card", {})

        idx_price   = card.get("priceUsdCents", 0) / 100 if card.get("priceUsdCents") else None
        idx_conf    = card.get("confidence")
        idx_delta   = card.get("deltaPct")
        idx_sale    = card.get("lastSaleAt","")[:10] if card.get("lastSaleAt") else None
        idx_spark   = json.dumps(card.get("spark", [])) if card.get("spark") else None
        idx_href    = card.get("href","")
        imgs        = data.get("certImages", {})
        cert_front  = imgs.get("front","")
        cert_back   = imgs.get("back","")
        cert_item   = imgs.get("item","")

        # calc gap
        if idx_price and idx_price > 0:
            gap_pct = round((r_fmv - idx_price) / idx_price * 100, 1)
            gap_usd = round(r_fmv - idx_price, 2)
        else:
            gap_pct = None
            gap_usd = None

        # If cert_item has an image and the existing image_url is an old renaiss render, update it
        conn.execute("""
            UPDATE tokens SET
                index_price_usd=?, index_confidence=?, index_delta_pct=?,
                index_last_sale=?, index_spark=?, cert_front=?, cert_back=?,
                cert_item=?, index_href=?, index_queried=1,
                fmv_gap_pct=?, fmv_gap_usd=?,
                image_url=COALESCE(NULLIF(?,''), image_url),
                updated_at=?
            WHERE token_id=?
        """, (
            idx_price, idx_conf, idx_delta,
            idx_sale, idx_spark, cert_front, cert_back,
            cert_item, idx_href, gap_pct, gap_usd,
            cert_item,
            datetime.now(timezone.utc).isoformat(),
            tid,
        ))

        # Per-token brief log (every 10 tokens so we can verify it's alive)
        if (i + 1) % 10 == 0 or idx_price:
            tid_short = str(row["token_id"])[:8]
            price_str = f"${idx_price:.0f}" if idx_price else "—"
            print(f"  [{i+1}/{total}] {row['name'][:35]:35} price={price_str}  gap={gap_pct}%")

        if (i + 1) % 100 == 0:
            conn.commit()
            pct = (i + 1) / total * 100
            print(f"  ── commit [{pct:.0f}%] ──")

        time.sleep(DELAY)

    conn.commit()
    print(f"[enrich] done ({total} processed)")


def build_wallet_summary(conn: sqlite3.Connection):
    """Aggregate per-holder stats (total FMV, average gap, cards held)."""
    conn.execute("DELETE FROM wallet_summary")
    conn.execute("""
        INSERT INTO wallet_summary
        SELECT
            holder,
            COUNT(*)                   as token_count,
            SUM(renaiss_fmv)           as total_renaiss_fmv,
            SUM(index_price_usd)       as total_index_fmv,
            ROUND(AVG(fmv_gap_pct),1)  as avg_fmv_gap_pct,
            (SELECT name FROM tokens t2 WHERE t2.holder=t.holder
             ORDER BY renaiss_fmv DESC LIMIT 1) as top_card_name,
            datetime('now')            as updated_at
        FROM tokens t
        WHERE holder IS NOT NULL AND holder != ''
        GROUP BY holder
        ORDER BY total_renaiss_fmv DESC
    """)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM wallet_summary").fetchone()[0]
    print(f"[wallet] {n} wallets summarized")


def export_fmv_gap_json(conn: sqlite3.Connection):
    """Export the 500 largest FMV gaps to JSON for the dashboard to read directly."""
    rows = conn.execute("""
        SELECT token_id, name, set_name, grader, grade, serial,
               renaiss_fmv, index_price_usd, fmv_gap_pct, fmv_gap_usd,
               index_confidence, index_last_sale, index_href,
               image_url, cert_item, cert_front,
               holder, holder_short, is_listed, ask_price
        FROM tokens
        WHERE fmv_gap_pct IS NOT NULL
        ORDER BY fmv_gap_pct DESC
        LIMIT 500
    """).fetchall()

    out = [dict(r) for r in rows]
    path = os.path.join(DATA, "fmv_gap.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[export] fmv_gap.json: {len(out)} rows → {path}")


def export_wallet_json(conn: sqlite3.Connection):
    """Top 200 wallets by renaiss FMV → wallet_summary.json"""
    rows = conn.execute("""
        SELECT * FROM wallet_summary ORDER BY total_renaiss_fmv DESC LIMIT 200
    """).fetchall()
    out = [dict(r) for r in rows]
    path = os.path.join(DATA, "wallet_summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[export] wallet_summary.json: {len(out)} wallets → {path}")


def export_stats(conn: sqlite3.Connection):
    stats = conn.execute("""
        SELECT
            COUNT(*)                            as total_tokens,
            SUM(index_queried)                  as queried,
            SUM(CASE WHEN fmv_gap_pct > 100 THEN 1 ELSE 0 END) as over_100pct,
            SUM(CASE WHEN fmv_gap_pct < -20 THEN 1 ELSE 0 END) as under_20pct,
            ROUND(AVG(renaiss_fmv),2)           as avg_renaiss_fmv,
            ROUND(AVG(index_price_usd),2)       as avg_index_price,
            ROUND(AVG(fmv_gap_pct),1)           as avg_gap_pct,
            ROUND(SUM(renaiss_fmv),0)           as total_renaiss_fmv,
            ROUND(SUM(index_price_usd),0)       as total_index_fmv
        FROM tokens
    """).fetchone()
    print("\n=== CollectIQ Universe Stats ===")
    for k in stats.keys():
        print(f"  {k}: {stats[k]}")

    # update meta
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('stats', ?)", [json.dumps(dict(stats))])
    conn.commit()
    return dict(stats)


# ─── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset",  action="store_true", help="Drop and recreate DB")
    ap.add_argument("--limit",  type=int, default=0,  help="Limit Index API queries (0=all)")
    ap.add_argument("--no-enrich", action="store_true", help="Skip Index API enrichment")
    ap.add_argument("--refresh-mkt", action="store_true", help="Pull fresh marketplace from Renaiss API")
    args = ap.parse_args()

    if args.reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print("[reset] DB deleted")

    conn = open_db()

    # 1. Optionally refresh marketplace_all.json first
    if args.refresh_mkt:
        print("[refresh] pulling fresh marketplace from api.renaiss.xyz …")
        import sys
        sys.path.insert(0, BASE)
        import renaiss_api
        total_new = 0
        offset = 0
        batch  = 100
        all_rows = []
        while True:
            result = renaiss_api._get(renaiss_api.RENAISS_BASE, "/marketplace",
                                      {"limit": batch, "offset": offset, "listedOnly": "false"})
            rows = result.get("collection", [])
            if not rows:
                break
            all_rows.extend(rows)
            total_new += len(rows)
            pg = result.get("pagination", {})
            print(f"  fetched offset={offset}  total_so_far={total_new}  api_total={pg.get('total')}")
            if not pg.get("hasMore"):
                break
            offset += batch
            time.sleep(0.3)
        # transform to flat schema
        flat = []
        for r in all_rows:
            attrs = {a["trait"]: a["value"] for a in r.get("attributes", [])}
            serial = attrs.get("Serial","")
            flat.append({
                "token_id":       str(r.get("tokenId","")),
                "name":           r.get("name",""),
                "set_name":       r.get("setName",""),
                "card_number":    r.get("cardNumber",""),
                "character_name": r.get("pokemonName") or r.get("characterName",""),
                "serial":         serial,
                "serial_num":     None,
                "grader":         r.get("gradingCompany",""),
                "grade":          r.get("grade",""),
                "image_url":      "",
                "ask_price":      int(r.get("askPriceInUSDT",0) or 0) / 1e18,
                "fmv":            float(r.get("fmvPriceInUSD",0) or 0),
                "is_listed":      True,
                "marketplace_url": f"https://www.renaiss.xyz/card/{r.get('tokenId','')}",
                "remaining_quantity": None,
                "market_price":   None,
                "sources":        0,
            })
        with open(MKT_PATH, "w", encoding="utf-8") as f:
            json.dump(flat, f, ensure_ascii=False, indent=2)
        print(f"[refresh] saved {len(flat)} tokens → {MKT_PATH}")

    # 2. Load base data
    load_marketplace(conn)
    load_holdings(conn)

    # 3. Enrich from Index API
    if not args.no_enrich:
        enrich_from_index(conn, limit=args.limit)

    # 4. Build wallet summary
    build_wallet_summary(conn)

    # 5. Export JSONs for dashboard
    export_fmv_gap_json(conn)
    export_wallet_json(conn)
    stats = export_stats(conn)

    conn.close()
    print("\n✅ build_universe complete")
    print(f"   DB: {DB_PATH}")
    print(f"   gap JSON: {os.path.join(DATA, 'fmv_gap.json')}")
    print(f"   wallet JSON: {os.path.join(DATA, 'wallet_summary.json')}")
