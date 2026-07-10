#!/usr/bin/env python3
"""
Web Dashboard for Renaiss EV Monitor v2
Combines EV calculation, pack monitoring, and external price comparison.
"""

import json
import os
import time
from datetime import datetime
from flask import Flask, jsonify, render_template, request

app = Flask(__name__, static_folder="static", template_folder="templates")

# Import local modules
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from external_price import ExternalPriceChecker
from main import LocalPoolDataLoader, EVCalculator
from pack_monitor import PackPoolAnalyzer, MOCK_PACKS
from our_price import OurPriceChecker, compare_card
import pack_ev
import renaiss_api


@app.route("/")
def index():
    """Render main dashboard"""
    return render_template("index.html")


@app.route("/holdings")
def holdings_page():
    """On-chain holdings page: each card's image + info + current location (holder)."""
    return render_template("holdings.html")


@app.route("/api/holdings")
def api_holdings():
    """Return data/holdings.json (current holder + card info for each token).
    Builds the file on the fly if it does not exist. Add ?identified=1 to return only identified cards."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "holdings.json")
    if not os.path.exists(path):
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
            import build_holdings
            build_holdings.build()
        except Exception as e:
            return jsonify({"error": f"holdings 尚未生成且即時建立失敗: {e}"}), 500
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if request.args.get("identified") == "1":
        data = dict(data)
        data["holdings"] = [h for h in data["holdings"] if h.get("identified")]
    return jsonify(data)


_POOL_SLUG = {
    "0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910": "omega",
    "0xfda4a907d23d9f24271bc47483c5b983831e325e": "eden",
    "0xb2891022648c5fad3721c42c05d8d283d4d53080": "renacrypt",
    "0xaab5f5fa75437a6e9e7004c12c9c56cda4b4885a": "legacy",
}


@app.route("/api/pulls")
def api_pulls():
    """On-chain pull records (onchain_pulls.db): each row = which pack (pool), which card, FMV, when it was pulled, and the pulling wallet.
    Params: ?min_fmv= (only show pulls above this FMV, default 0), ?limit= (default 200), ?pool=slug (omega/eden/...).
    Returns sync progress (last synced block time, how far behind chain head) so the frontend can flag whether it's live."""
    import sqlite3
    base = os.path.dirname(os.path.abspath(__file__))
    db = os.path.join(base, "data", "onchain_pulls.db")
    if not os.path.exists(db):
        return jsonify({"pulls": [], "note": "onchain_pulls.db 尚未產生"})
    try:
        min_fmv = float(request.args.get("min_fmv", 0) or 0)
    except Exception:
        min_fmv = 0
    try:
        limit = max(1, min(2000, int(request.args.get("limit", 200))))
    except Exception:
        limit = 200
    pool_q = (request.args.get("pool") or "").strip().lower()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    where = ["is_mint=1"]
    params = []
    if min_fmv > 0:
        where.append("market_fmv >= ?"); params.append(min_fmv)
    if pool_q:
        addrs = [a for a, s in _POOL_SLUG.items() if s == pool_q]
        if addrs:
            where.append("from_addr=?"); params.append(addrs[0])
    sql = (f"SELECT token_id, card_name, set_name, market_fmv, from_addr, to_addr, "
           f"block_time, block_number, tx_hash FROM onchain_pulls "
           f"WHERE {' AND '.join(where)} ORDER BY block_time DESC, log_index DESC LIMIT ?")
    rows = conn.execute(sql, params + [limit]).fetchall()
    last_block_time = conn.execute("SELECT MAX(block_time) FROM onchain_pulls").fetchone()[0]

    def _state(k):
        try:
            r = conn.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
            return int(r[0]) if r else None
        except Exception:
            return None

    last_block  = _state("last_block")
    head_block  = _state("head_block")
    synced_ts   = _state("synced_ts")     # this round's wall-clock: how long ago we confirmed catching up to head
    synced_block = _state("synced_block") or last_block
    # Real sync lag = head minus last synced block; a stale synced_ts means the crawler itself stopped.
    sync_behind_blocks = (head_block - synced_block) if (head_block and synced_block) else None
    conn.close()
    pulls = []
    for r in rows:
        pulls.append({
            "token_id": r["token_id"],
            "card_name": r["card_name"],
            "set_name": r["set_name"],
            "fmv": r["market_fmv"],
            "pool": _POOL_SLUG.get((r["from_addr"] or "").lower(), (r["from_addr"] or "")[:10]),
            "buyer": r["to_addr"],
            "block_time": r["block_time"],
            "block_number": r["block_number"],
            "tx_hash": r["tx_hash"],
            "card_url": f"https://www.renaiss.xyz/card/{r['token_id']}" if r["token_id"] else None,
        })
    return jsonify({
        "pulls": pulls,
        "last_block_time": last_block_time,   # time of the most recent "pull" (not the sync delay!)
        "last_block": last_block,
        "head_block": head_block,             # chain head the crawler saw last round
        "synced_block": synced_block,
        "synced_ts": synced_ts,               # crawler's last-round wall-clock (tells whether the crawler is still alive)
        "sync_behind_blocks": sync_behind_blocks,  # real number of blocks behind (≈0 = live)
        "min_fmv": min_fmv,
    })


@app.route("/api/marketplace")
def api_marketplace():
    """Cards currently listed on the market (data/marketplace_listed.json).
    Each includes name / image / ask_price / FMV / a link to the Renaiss store page.
    Add ?limit=N (default 500), ?sort=ask|fmv|discount (discount = fmv/ask, high to low)."""
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "data", "marketplace_listed.json")
    if not os.path.exists(path):
        return jsonify({"cards": [], "total": 0, "note": "marketplace_listed.json 尚未產生"})
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        rows = []
    sort = request.args.get("sort", "discount")
    def _disc(r):
        a, fm = r.get("ask_price"), r.get("fmv")
        return (fm / a) if (a and fm) else 0
    if sort == "ask":
        rows = sorted(rows, key=lambda r: (r.get("ask_price") is None, r.get("ask_price") or 0))
    elif sort == "fmv":
        rows = sorted(rows, key=lambda r: -(r.get("fmv") or 0))
    else:
        rows = sorted(rows, key=lambda r: -_disc(r))
    total = len(rows)
    try:
        limit = max(1, min(2000, int(request.args.get("limit", 500))))
    except Exception:
        limit = 500
    return jsonify({"total": total, "cards": rows[:limit]})


@app.route("/api/pack-ev")
def api_pack_ev():
    """Per-pack official EV / empirical EV / our own computed EV (externally calibrated) + limited-edition flag."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pack_data.json")
    return jsonify(pack_ev.load_and_analyze(path))


def _limited_events():
    """Limited-pack detection events produced by track_limited_packs.py (includes ts / slug / remaining)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "limited_events.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else data.get("events", [])
    except Exception:
        return []


def _pack_tiers_by_name(conn, name):
    """Get the 'latest version' tier composition from pack_content by pack_name.
    Picks the newest version by updated_at (last refresh) rather than captured_at (first seen).
    Returns (pack_id, total_cards, tiers, our_ev, featured). Returns (None, 0, {}, None, None) if not found."""
    row = conn.execute(
        "SELECT pack_id FROM pack_content WHERE pack_name=? "
        "ORDER BY updated_at DESC LIMIT 1", (name,)).fetchone()
    if not row:
        return None, 0, {}, None, None
    pid = row["pack_id"]
    total = conn.execute("SELECT COUNT(*) FROM pack_content WHERE pack_id=?", (pid,)).fetchone()[0]
    order = "CASE tier WHEN 'TOP' THEN 0 WHEN 'S' THEN 1 WHEN 'A' THEN 2 WHEN 'B' THEN 3 WHEN 'C' THEN 4 WHEN 'D' THEN 5 ELSE 6 END"
    tiers = {}
    for t in conn.execute(f"""
            SELECT tier, COUNT(*) slots, ROUND(AVG(renaiss_buyback_usd),0) avg,
                   MIN(renaiss_buyback_usd) min, MAX(renaiss_buyback_usd) max
            FROM pack_content WHERE pack_id=? GROUP BY tier ORDER BY {order}""", (pid,)):
        tiers[t["tier"]] = {"pct": round(t["slots"]/total*100, 1) if total else 0,
                            "n_cards": t["slots"], "avg": t["avg"],
                            "min": t["min"], "max": t["max"]}
    # Our computed EV: weighted average of the whole pool's buyback (expected official buyback value per pull)
    our_ev = conn.execute(
        "SELECT ROUND(AVG(renaiss_buyback_usd),2) FROM pack_content WHERE pack_id=?",
        (pid,)).fetchone()[0]
    feat = conn.execute(
        "SELECT MAX(renaiss_buyback_usd) FROM pack_content WHERE pack_id=?", (pid,)).fetchone()[0]
    return pid, total, tiers, our_ev, feat


@app.route("/api/new-pack")
def api_new_pack():
    """This week's limited pack. Resolution order:
      1) Open/counting-down limited pack from the live API (is_open=true);
      2) Most recently detected limited pack from limited_events.json (may have ended, is_open=false);
      3) Empty state (empty=true) -- no longer falls back to the hardcoded 06/26 static file.
    Tiers are always filled in from the newest pack_content version by updated_at."""
    from datetime import timezone as _tz
    try:
        packs = renaiss_api.get_packs(include_inactive=True)
    except Exception:
        packs = []
    by_name = {p.get("name"): p for p in packs}
    by_slug = {p.get("slug"): p for p in packs}

    chosen = None
    is_open = False
    remaining = None
    detected_at = None

    # 1) Open/counting-down limited pack (ideal case)
    live_open = [p for p in packs
                 if p.get("packType") == "limited"
                 and p.get("stage") in ("countdown", "active")]
    if live_open:
        chosen = live_open[0]
        is_open = True

    # 2) Most recently detected limited pack (even if archived, more honest than freezing on 06/26)
    if chosen is None:
        events = sorted((e for e in _limited_events() if e.get("is_limited")),
                        key=lambda e: e.get("ts", 0), reverse=True)
        if events:
            ev = events[0]
            remaining = ev.get("remaining")
            if ev.get("ts"):
                detected_at = datetime.fromtimestamp(ev["ts"], tz=_tz.utc).isoformat()
            chosen = by_slug.get(ev.get("slug")) or by_name.get(ev.get("name")) or {
                "name": ev.get("name"), "slug": ev.get("slug"),
                "packType": "limited", "stage": "archived"}
            is_open = chosen.get("stage") in ("countdown", "active")

    # 3) Empty state: there really is no open/recent limited pack right now
    if chosen is None:
        return jsonify({"empty": True, "note": "目前無開放中限量卡機"})

    name = chosen.get("name")
    pid = total = None
    tiers = {}
    our_ev = feat = None
    conn = _core_db()
    if conn:
        pid, total, tiers, our_ev, feat = _pack_tiers_by_name(conn, name)
        conn.close()

    return jsonify({
        "name": name,
        "slug": chosen.get("slug"),
        "packType": chosen.get("packType", "limited"),
        "stage": chosen.get("stage"),
        "is_open": is_open,
        "remaining": remaining,
        "detected_at": detected_at,
        "pack_id": pid,
        "source": "live+events",
        "total_cards": total,
        "price_usd": chosen.get("price_usd"),
        "official_ev_usd": chosen.get("official_ev_usd"),
        "our_ev_usd": our_ev,
        "featured_card_fmv_usd": feat or chosen.get("featured_fmv_usd"),
        "active_from": None,
        "instant_buyback_pct": 90,
        "tiers": tiers or {},
    })


@app.route("/api/freshness")
def api_freshness():
    """Data source for the freshness badges: how old each source is and whether its crawler is still alive.
    Each source returns ts(ISO) / age_sec / stale; stale=true means it exceeded that source's tolerance threshold.
    This is a general safeguard for the 06/26-style stuck problem -- an expired page should flag red rather than silently show stale data."""
    import time as _time
    now = _time.time()
    sources = {}

    def _emit(name, epoch, ttl_sec, extra=None):
        if epoch is None:
            src = {"ts": None, "age_sec": None, "stale": True, "ttl_sec": ttl_sec}
        else:
            age = max(0, now - float(epoch))
            src = {"ts": datetime.utcfromtimestamp(float(epoch)).isoformat() + "Z",
                   "age_sec": int(age), "stale": age > ttl_sec, "ttl_sec": ttl_sec}
        if extra:
            src.update(extra)
        sources[name] = src

    # 1) pack_content catalog fetch time (core.db meta) -- freshness of this week's limited pack / pack overview
    grabbed_epoch = None
    conn = _core_db()
    if conn:
        try:
            r = conn.execute("SELECT v FROM meta WHERE k='pack_content_grabbed_at'").fetchone()
            if r and r[0]:
                grabbed_epoch = datetime.fromisoformat(r[0].replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
        conn.close()
    _emit("pack_catalog", grabbed_epoch, 8 * 3600)   # 6h schedule -> 8h tolerance

    # 2) On-chain sync (onchain_pulls.db state.synced_ts / sync_behind_blocks)
    synced_ts = sync_behind = None
    ocdb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "onchain_pulls.db")
    if os.path.exists(ocdb):
        import sqlite3 as _sq
        c = _sq.connect(ocdb)
        try:
            def _s(k):
                r = c.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
                return int(r[0]) if r and r[0] is not None else None
            synced_ts = _s("synced_ts")
            head, sb = _s("head_block"), (_s("synced_block") or _s("last_block"))
            sync_behind = (head - sb) if (head is not None and sb is not None) else None
        except Exception:
            pass
        c.close()
    _emit("onchain_sync", synced_ts, 900, {"sync_behind_blocks": sync_behind})  # 300s -> 15m tolerance

    # 3) Limited pack events (last time a new/open limited pack was detected)
    last_ev = None
    for e in _limited_events():
        ts = e.get("ts")
        if ts:
            last_ev = max(last_ev or 0, ts)
    _emit("limited_events", last_ev, 7 * 24 * 3600)  # weekly updates -> one-week tolerance

    return jsonify({
        "now": datetime.utcnow().isoformat() + "Z",
        "sources": sources,
        "any_stale": any(s.get("stale") for s in sources.values()),
    })


@app.route("/healthz")
def healthz():
    """Lightweight liveness probe for an external watchdog (scripts/healthcheck.py).
    Only confirms Flask is alive + core.db can open; hits no external API, keeping responses at millisecond latency."""
    ok = True
    db_ok = False
    conn = _core_db()
    if conn:
        try:
            conn.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:
            ok = False
        finally:
            conn.close()
    else:
        db_ok = False
    return jsonify({
        "status": "ok" if ok else "degraded",
        "db": db_ok,
        "now": datetime.utcnow().isoformat() + "Z",
    }), (200 if ok else 503)


@app.route("/limited-history")
def limited_history_page():
    """Limited pack history timeline (who opened / added / pulled an S card, and when)."""
    return render_template("limited_history.html")


@app.route("/api/limited-history")
def api_limited_history():
    """Timeline view of limited_events.json: sorted newest to oldest, with ISO timestamps and human-readable types.
    This is the audit panel for the 06/26-style "this week's limited pack is stuck" problem -- laying out every
    detected limited event makes it easy to see at a glance which sessions opened last week and whether any were missed."""
    LABELS = {"NEW_PACK": "新卡機", "LIMITED_OPEN": "限量開放", "NEW_S_PULL": "抽出 S 卡"}
    events = []
    for e in _limited_events():
        ts = e.get("ts")
        events.append({
            "ts": ts,
            "iso": (datetime.utcfromtimestamp(float(ts)).isoformat() + "Z") if ts else None,
            "type": e.get("type"),
            "type_label": LABELS.get(e.get("type"), e.get("type")),
            "name": e.get("name"),
            "slug": e.get("slug"),
            "is_limited": e.get("is_limited"),
            "remaining": e.get("remaining"),
            "platform_ev_usd": e.get("platform_ev_usd"),
            "card": e.get("card"),
            "token_id": e.get("token_id"),
        })
    events.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return jsonify({"events": events, "count": len(events),
                    "now": datetime.utcnow().isoformat() + "Z"})


@app.route("/api/pool-addresses")
def api_pool_addresses():
    """On-chain pack contract addresses (known + suspected new-pack candidates). Produced by discover_pools.py."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pool_addresses.json")
    if not os.path.exists(path):
        return jsonify({"known": [], "candidates": [], "note": "尚未產生，請跑 scripts/discover_pools.py"})
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/limited-events")
def api_limited_events():
    """Limited pack event stream (open/added/new S card). Produced by track_limited_packs.py."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "limited_events.json")
    if not os.path.exists(path):
        return jsonify([])
    with open(path, encoding="utf-8") as f:
        events = json.load(f)
    return jsonify(events[-100:][::-1])  # newest first


# ═══════════════════════════════════════════════════════════════════════════════
#  LIVE POOL TRACKER — real-time prize-pool tracking during a limited pack's open window (data/live_pool.db)
# ═══════════════════════════════════════════════════════════════════════════════
def _live_db():
    import sqlite3 as _sq
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "live_pool.db")
    if not os.path.exists(path):
        return None
    conn = _sq.connect(path)
    conn.row_factory = _sq.Row
    return conn


def _active_pool_row(conn):
    """Pick the most recently updated pool (currently open / most recently tracked)."""
    return conn.execute(
        "SELECT * FROM pool_meta ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()


@app.route("/live")
def live_page():
    """Real-time prize-pool tracking panel for a limited pack's open window (temporarily high-density -> its own page)."""
    return render_template("live.html")


@app.route("/api/live/pool")
def api_live_pool():
    """Target pool status summary + countdown. Use ?address= to specify a pool."""
    conn = _live_db()
    if not conn:
        return jsonify({"available": False, "note": "尚未產生，請跑 scripts/pool_live_monitor.py"})
    addr = request.args.get("address", "").lower()
    row = (conn.execute("SELECT * FROM pool_meta WHERE address=?", (addr,)).fetchone()
           if addr else _active_pool_row(conn))
    if not row:
        conn.close()
        return jsonify({"available": False})
    d = dict(row)
    # Big-prize stats
    pool = d["address"]
    d["big_in_pool"] = conn.execute(
        "SELECT COUNT(*) FROM tokens WHERE pool=? AND is_big=1 AND status='in_pool'", (pool,)).fetchone()[0]
    d["big_pulled"] = conn.execute(
        "SELECT COUNT(*) FROM tokens WHERE pool=? AND is_big=1 AND status='pulled'", (pool,)).fetchone()[0]
    # Pool list (for switching)
    pools = [dict(r) for r in conn.execute(
        "SELECT address, name, slug, loaded, pulled, remaining, updated_at FROM pool_meta ORDER BY updated_at DESC").fetchall()]
    conn.close()
    # Countdown (activeFrom)
    d["countdown_seconds"] = None
    if d.get("active_from"):
        try:
            af = datetime.fromisoformat(d["active_from"].replace("Z", "+00:00"))
            from datetime import timezone as _tz
            d["countdown_seconds"] = int((af - datetime.now(_tz.utc)).total_seconds())
        except Exception:
            pass
    d["available"] = True
    d["pools"] = pools
    return jsonify(d)


@app.route("/api/live/pulls")
def api_live_pulls():
    """Recent pull event stream: which card, which wallet pulled it, time, FMV. ?address= ?limit=60 ?kind=pull|recycle|burn|all"""
    conn = _live_db()
    if not conn:
        return jsonify({"events": []})
    addr = request.args.get("address", "").lower()
    if not addr:
        r = _active_pool_row(conn)
        addr = r["address"] if r else ""
    limit = min(int(request.args.get("limit", 60)), 300)
    kind = request.args.get("kind", "pull")
    q = "SELECT time,token_id,kind,from_addr,to_addr,card_name,fmv,tx FROM events WHERE pool=?"
    params = [addr]
    if kind != "all":
        q += " AND kind=?"
        params.append(kind)
    q += " ORDER BY block DESC, log_index DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    return jsonify({"events": rows})


@app.route("/api/live/buyers")
def api_live_buyers():
    """Wallets that pulled the most (whales). ?address= ?limit=20"""
    conn = _live_db()
    if not conn:
        return jsonify({"buyers": []})
    addr = request.args.get("address", "").lower()
    if not addr:
        r = _active_pool_row(conn)
        addr = r["address"] if r else ""
    limit = min(int(request.args.get("limit", 20)), 100)
    rows = [dict(r) for r in conn.execute(
        "SELECT address, pulls, first_seen_time FROM wallets WHERE pool=? ORDER BY pulls DESC LIMIT ?",
        (addr, limit)).fetchall()]
    conn.close()
    return jsonify({"buyers": rows})


@app.route("/api/live/big-prizes")
def api_live_big_prizes():
    """Big prizes (FMV≥$300): already pulled vs still in the pool. ?address="""
    conn = _live_db()
    if not conn:
        return jsonify({"pulled": [], "in_pool": []})
    addr = request.args.get("address", "").lower()
    if not addr:
        r = _active_pool_row(conn)
        addr = r["address"] if r else ""
    pulled = [dict(r) for r in conn.execute(
        "SELECT token_id, card_name, fmv, holder, last_time FROM tokens "
        "WHERE pool=? AND is_big=1 AND status='pulled' ORDER BY fmv DESC LIMIT 50", (addr,)).fetchall()]
    in_pool = [dict(r) for r in conn.execute(
        "SELECT token_id, card_name, fmv FROM tokens "
        "WHERE pool=? AND is_big=1 AND status='in_pool' ORDER BY fmv DESC LIMIT 50", (addr,)).fetchall()]
    conn.close()
    return jsonify({"pulled": pulled, "in_pool": in_pool})


@app.route("/api/live/ev")
def api_live_ev():
    """Live prize-pool EV back-calculation + per-card FMV comparison (Renaiss FMV vs real-market index_price).

    Returns:
      remaining_ev_market / remaining_ev_renaiss  expected value per pull for the remaining pool (market price / Renaiss price)
      pulled_ev_*                                 average of what's already been pulled
      ev_ratio_live = remaining_ev_market / price whether buying now is still worth it (>1 = worth the price)
      easter_eggs   lucky cards (Renaiss priced low, market ≥1.5x higher) -> still in pool, something to hope for
      data_errors   likely input errors (Renaiss ≫ market by 3x and low confidence) -> don't trust the Renaiss FMV
      recycled_gems high-market-value cards recycled back into the pool (spot recycling mistakes / bargains)
      coverage      fraction that already has a real market price
    """
    import sqlite3 as _sq
    conn = _live_db()
    if not conn:
        return jsonify({"available": False})
    addr = request.args.get("address", "").lower()
    if not addr:
        r = _active_pool_row(conn)
        addr = r["address"] if r else ""
    meta = conn.execute("SELECT * FROM pool_meta WHERE address=?", (addr,)).fetchone()
    toks = conn.execute(
        "SELECT token_id,status,recycled,fmv,card_name FROM tokens WHERE pool=?", (addr,)).fetchall()
    conn.close()
    meta = dict(meta) if meta else {}

    # Pack price (needed to judge whether it's worth the cost)
    price = None
    try:
        for p in renaiss_api.get_packs(include_inactive=True):
            if p.get("slug") == meta.get("slug"):
                price = p.get("price_usd")
                break
    except Exception:
        pass

    # Join collectiq.db to get real market price index_price_usd + image
    market = {}
    cdb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "collectiq.db")
    ids = [t["token_id"] for t in toks]
    if os.path.exists(cdb_path) and ids:
        cdb = _sq.connect(cdb_path)
        cdb.row_factory = _sq.Row
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            ph = ",".join("?" * len(chunk))
            for r in cdb.execute(
                f"SELECT token_id,name,renaiss_fmv,index_price_usd,index_confidence,"
                f"fmv_gap_pct,image_url FROM tokens WHERE token_id IN ({ph})", chunk):
                market[r["token_id"]] = dict(r)
        cdb.close()

    in_pool, pulled = [], []
    for t in toks:
        m = market.get(t["token_id"], {})
        rf = m.get("renaiss_fmv") if m.get("renaiss_fmv") is not None else t["fmv"]
        mk = m.get("index_price_usd")
        rec = {"token_id": t["token_id"], "name": m.get("name") or t["card_name"],
               "renaiss_fmv": rf, "market_fmv": mk, "gap_pct": m.get("fmv_gap_pct"),
               "confidence": m.get("index_confidence"), "image": m.get("image_url"),
               "recycled": t["recycled"]}
        if t["status"] == "in_pool":
            in_pool.append(rec)
        elif t["status"] == "pulled":
            pulled.append(rec)

    def _avg(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    rem_ev_mkt = _avg(in_pool, "market_fmv")
    rem_ev_ren = _avg(in_pool, "renaiss_fmv")
    easter_eggs, data_errors, recycled_gems = [], [], []
    for r in in_pool:
        rf, mk = r.get("renaiss_fmv"), r.get("market_fmv")
        if mk and rf and mk >= rf * 1.5 and mk >= 20:
            easter_eggs.append(r)
        if rf and mk and rf >= mk * 3 and (r.get("confidence") in ("low", None)) and rf >= 50:
            data_errors.append(r)
        if r.get("recycled") and mk and mk >= 100:
            recycled_gems.append(r)
    easter_eggs.sort(key=lambda r: (r["market_fmv"] or 0), reverse=True)
    data_errors.sort(key=lambda r: (r["renaiss_fmv"] or 0), reverse=True)
    recycled_gems.sort(key=lambda r: (r["market_fmv"] or 0), reverse=True)

    covered = len([r for r in in_pool if r.get("market_fmv") is not None])
    return jsonify({
        "available": True, "address": addr, "name": meta.get("name"),
        "slug": meta.get("slug"), "price": price,
        "loaded": meta.get("loaded"), "in_pool": meta.get("remaining"),
        "pulled": meta.get("pulled"),
        "remaining_ev_market": rem_ev_mkt, "remaining_ev_renaiss": rem_ev_ren,
        "pulled_ev_market": _avg(pulled, "market_fmv"), "pulled_ev_renaiss": _avg(pulled, "renaiss_fmv"),
        "ev_ratio_live": round(rem_ev_mkt / price, 2) if (rem_ev_mkt and price) else None,
        "coverage": {"in_pool_total": len(in_pool), "with_market_price": covered,
                     "pct": round(covered / len(in_pool) * 100, 1) if in_pool else 0},
        "easter_eggs": easter_eggs[:30],
        "data_errors": data_errors[:20],
        "recycled_gems": recycled_gems[:20],
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  CORE LEDGER — unified core ledger (data/collectiq_core.db)
#  Output of event sourcing: true holders / lucky cards / reward progress / full per-card transfer history
# ═══════════════════════════════════════════════════════════════════════════════
def _core_db():
    import sqlite3 as _sq
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "collectiq_core.db")
    if not os.path.exists(path):
        return None
    conn = _sq.connect(path)
    conn.row_factory = _sq.Row
    return conn


@app.route("/api/core/lucky")
def api_core_lucky():
    """Lucky cards (easter eggs): real market price far above Renaiss FMV, plus "who holds it now".
    Params: ?min_luck=1.5 ?limit=50"""
    conn = _core_db()
    if not conn:
        return jsonify({"available": False, "note": "尚未建立 collectiq_core.db"})
    min_luck = float(request.args.get("min_luck", 1.5))
    limit = int(request.args.get("limit", 50))
    rows = conn.execute("""
        SELECT s.token_id, d.name, d.character_name, d.image_url, d.image_local,
               s.renaiss_fmv, s.index_price_usd, s.luck_value,
               fh.status, fh.current_holder, fh.is_listed, fh.ask_price
        FROM fmv_snapshots s
        JOIN dim_card d ON d.token_id = s.token_id
        LEFT JOIN fact_holding fh ON fh.token_id = s.token_id
        WHERE s.luck_value >= ?
          AND s.ts = (SELECT MAX(ts) FROM fmv_snapshots x WHERE x.token_id = s.token_id)
        ORDER BY s.luck_value DESC LIMIT ?
    """, (min_luck, limit)).fetchall()
    conn.close()
    return jsonify({"available": True, "count": len(rows),
                    "cards": [dict(r) for r in rows]})


@app.route("/api/core/holders")
def api_core_holders():
    """Holder leaderboard + reward progress (Eevee full-set completion). ?limit=30"""
    conn = _core_db()
    if not conn:
        return jsonify({"available": False})
    limit = int(request.args.get("limit", 30))
    top = conn.execute("""
        SELECT current_holder AS holder, COUNT(*) AS tokens
        FROM fact_holding
        WHERE status='held' AND current_holder IS NOT NULL
        GROUP BY current_holder ORDER BY tokens DESC LIMIT ?
    """, (limit,)).fetchall()
    out = []
    for r in top:
        d = dict(r)
        ev = conn.execute(
            "SELECT detail FROM reward_status WHERE reward_type='eevee_full' AND holder=?",
            (d["holder"],)).fetchone()
        d["eevee"] = json.loads(ev["detail"]) if ev else None
        runs = conn.execute(
            "SELECT COUNT(*) FROM reward_status WHERE reward_type='serial_run' AND holder=?",
            (d["holder"],)).fetchone()[0]
        d["serial_runs"] = runs
        lbl = conn.execute("SELECT label FROM dim_wallet WHERE address=?",
                           (d["holder"],)).fetchone()
        d["label"] = lbl["label"] if lbl else None
        out.append(d)
    conn.close()
    return jsonify({"available": True, "holders": out})


@app.route("/api/core/rewards")
def api_core_rewards():
    """Reward status summary: Eevee full set / serial run / SBT."""
    conn = _core_db()
    if not conn:
        return jsonify({"available": False})
    eevee = [dict(r) for r in conn.execute("""
        SELECT holder, achieved, detail FROM reward_status
        WHERE reward_type='eevee_full'
        ORDER BY json_extract(detail,'$.count') DESC LIMIT 30""").fetchall()]
    for e in eevee:
        e["detail"] = json.loads(e["detail"]) if e.get("detail") else None
    runs = [dict(r) for r in conn.execute("""
        SELECT holder, detail FROM reward_status WHERE reward_type='serial_run'
        LIMIT 50""").fetchall()]
    for r in runs:
        r["detail"] = json.loads(r["detail"]) if r.get("detail") else None
    sbt = [dict(r) for r in conn.execute("""
        SELECT holder, detail FROM reward_status WHERE reward_type='sbt'
        LIMIT 50""").fetchall()]
    for s in sbt:
        s["detail"] = json.loads(s["detail"]) if s.get("detail") else None
    conn.close()
    return jsonify({"available": True, "eevee_full": eevee,
                    "serial_runs": runs, "sbt": sbt})


@app.route("/api/core/token/<token_id>")
def api_core_token(token_id):
    """A card's full on-chain transfer history (who pulled it, how it changed hands, who holds it now)."""
    conn = _core_db()
    if not conn:
        return jsonify({"available": False})
    card = conn.execute("SELECT * FROM dim_card WHERE token_id=?", (token_id,)).fetchone()
    hold = conn.execute("SELECT * FROM fact_holding WHERE token_id=?", (token_id,)).fetchone()
    hist = conn.execute("""
        SELECT block_number, block_time, from_addr, to_addr, kind, tx_hash, confirmed
        FROM ledger_transfers WHERE token_id=?
        ORDER BY block_number, log_index""", (token_id,)).fetchall()
    fmv = conn.execute("""
        SELECT ts, renaiss_fmv, index_price_usd, luck_value FROM fmv_snapshots
        WHERE token_id=? ORDER BY ts""", (token_id,)).fetchall()
    conn.close()
    return jsonify({
        "available": bool(card or hist),
        "card": dict(card) if card else None,
        "holding": dict(hold) if hold else None,
        "history": [dict(r) for r in hist],
        "fmv_history": [dict(r) for r in fmv],
    })


@app.route("/api/core/packs")
def api_core_packs():
    """Overview of unopened / opened packs (pack_content snapshot).
    Per pack: total slots, distinct card count, buyback range, top-price card thumbnail, whether a market price exists.
    ?stage=countdown shows only the ones about to open."""
    conn = _core_db()
    if not conn:
        return jsonify({"available": False, "note": "尚未建立 collectiq_core.db"})
    stage = request.args.get("stage")
    where = "WHERE pack_stage = ?" if stage else ""
    args = (stage,) if stage else ()
    rows = conn.execute(f"""
        SELECT pack_id, pack_name, pack_stage,
               COUNT(*) AS slots,
               COUNT(DISTINCT item_id) AS distinct_cards,
               MIN(renaiss_buyback_usd) AS min_buyback,
               MAX(renaiss_buyback_usd) AS max_buyback,
               COUNT(market_price_usd) AS priced,
               MAX(luck_value) AS top_luck,
               MAX(captured_at) AS captured_at
        FROM pack_content
        {where}
        GROUP BY pack_id
        ORDER BY CASE pack_stage WHEN 'countdown' THEN 0 WHEN 'active' THEN 1
                 ELSE 2 END, max_buyback DESC
    """, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        top = conn.execute("""
            SELECT name, image_url, renaiss_buyback_usd
            FROM pack_content WHERE pack_id=?
            ORDER BY renaiss_buyback_usd DESC LIMIT 1""", (d["pack_id"],)).fetchone()
        d["top_card"] = dict(top) if top else None
        out.append(d)
    conn.close()
    return jsonify({"available": True, "count": len(out), "packs": out})


@app.route("/api/core/pack/<pack_id>")
def api_core_pack_cards(pack_id):
    """All cards in a single pack (distinct physical cards, with slot weight / buyback / market price / luck).
    ?min_luck= shows only treasure cards."""
    conn = _core_db()
    if not conn:
        return jsonify({"available": False})
    min_luck = request.args.get("min_luck")
    # luck must come from the same source as the displayed buyback: one item_id can span multiple tiers/slots,
    # each with a different official buyback price. Take MAX(buyback) (most conservative, lowest luck) and compute
    # luck from that same value, to avoid pairing MAX(buyback) with MAX(luck) into an inconsistent, inflated fake value.
    luck_expr = "ROUND(MAX(market_price_usd) / NULLIF(MAX(renaiss_buyback_usd), 0), 4)"
    having = f"HAVING {luck_expr} >= ?" if min_luck else ""
    args = [pack_id]
    if min_luck:
        args.append(float(min_luck))
    rows = conn.execute(f"""
        SELECT item_id, name, tier, cert, grader, grade, year, image_url,
               COUNT(*) AS slots,
               MAX(renaiss_buyback_usd) AS renaiss_buyback_usd,
               MAX(market_price_usd) AS market_price_usd,
               MAX(market_source) AS market_source,
               MAX(market_url) AS market_url,
               {luck_expr} AS luck_value,
               MAX(token_id) AS token_id
        FROM pack_content WHERE pack_id=?
        GROUP BY item_id
        {having}
        ORDER BY luck_value DESC, renaiss_buyback_usd DESC
    """, args).fetchall()
    meta = conn.execute(
        "SELECT pack_name, pack_stage FROM pack_content WHERE pack_id=? LIMIT 1",
        (pack_id,)).fetchone()
    conn.close()
    return jsonify({
        "available": bool(rows),
        "pack_id": pack_id,
        "pack_name": meta["pack_name"] if meta else None,
        "pack_stage": meta["pack_stage"] if meta else None,
        "count": len(rows),
        "cards": [dict(r) for r in rows],
    })


@app.route("/api/core/pack-tiers/<pack_id>")
def api_core_pack_tiers(pack_id):
    """A single pack's tier composition (TOP/S/A/B/C/D): per-tier slot count, share, buyback range, representative cards.
    For seeing "what each tier of this pack contains". pack_id may be 'countdown' to get the currently counting-down pack."""
    conn = _core_db()
    if not conn:
        return jsonify({"available": False})
    if pack_id in ("countdown", "current"):
        r = conn.execute("""SELECT pack_id FROM pack_content WHERE pack_stage='countdown'
                            ORDER BY captured_at DESC LIMIT 1""").fetchone()
        if not r:
            conn.close()
            return jsonify({"available": False, "note": "目前沒有倒數中的卡機"})
        pack_id = r["pack_id"]
    meta = conn.execute(
        "SELECT pack_name, pack_stage, COUNT(*) AS slots FROM pack_content WHERE pack_id=?",
        (pack_id,)).fetchone()
    if not meta or not meta["slots"]:
        conn.close()
        return jsonify({"available": False})
    total = meta["slots"]
    order = "CASE tier WHEN 'TOP' THEN 0 WHEN 'S' THEN 1 WHEN 'A' THEN 2 WHEN 'B' THEN 3 WHEN 'C' THEN 4 WHEN 'D' THEN 5 ELSE 6 END"
    tiers = conn.execute(f"""
        SELECT tier, COUNT(*) AS slots, COUNT(DISTINCT item_id) AS distinct_cards,
               ROUND(AVG(renaiss_buyback_usd),0) AS avg_buyback,
               MIN(renaiss_buyback_usd) AS min_buyback,
               MAX(renaiss_buyback_usd) AS max_buyback
        FROM pack_content WHERE pack_id=?
        GROUP BY tier ORDER BY {order}
    """, (pack_id,)).fetchall()
    out = []
    for t in tiers:
        d = dict(t)
        d["pct"] = round(d["slots"] / total * 100, 2) if total else None
        tops = conn.execute("""
            SELECT DISTINCT name, cert, renaiss_buyback_usd, image_url
            FROM pack_content WHERE pack_id=? AND tier=?
            ORDER BY renaiss_buyback_usd DESC LIMIT 5""", (pack_id, t["tier"])).fetchall()
        d["top_cards"] = [dict(x) for x in tops]
        out.append(d)
    conn.close()
    return jsonify({"available": True, "pack_id": pack_id,
                    "pack_name": meta["pack_name"], "pack_stage": meta["pack_stage"],
                    "total_slots": total, "tiers": out})


@app.route("/api/core/pack-gems")
def api_core_pack_gems():
    """Cross-pack treasure-card leaderboard: highest luck_value in pack_content (including unopened packs).
    ?min_luck=1.5 ?limit=60"""
    conn = _core_db()
    if not conn:
        return jsonify({"available": False})
    min_luck = float(request.args.get("min_luck", 1.5))
    limit = int(request.args.get("limit", 60))
    rows = conn.execute("""
        SELECT pack_id, pack_name, pack_stage, item_id, name, tier, cert,
               grader, grade, year, image_url,
               renaiss_buyback_usd, market_price_usd, market_source, market_url,
               luck_value, token_id
        FROM pack_content
        WHERE luck_value >= ?
        GROUP BY pack_id, item_id
        ORDER BY luck_value DESC LIMIT ?
    """, (min_luck, limit)).fetchall()
    conn.close()
    return jsonify({"available": True, "count": len(rows),
                    "gems": [dict(r) for r in rows]})


@app.route("/intelligence")
def intelligence_page():
    """CollectIQ Price Intelligence — FMV gap analysis / whale wallets / True EV."""
    return render_template("intelligence.html")


@app.route("/api/intelligence/fmv-gap")
def api_fmv_gap():
    """
    CollectIQ core: Renaiss FMV vs real-market price gap table.
    Params: ?limit=500 ?min_gap=0 ?max_gap= ?confidence=high|medium|low
    Also returns stats (aggregate statistics).
    """
    import sqlite3 as _sq
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "collectiq.db")

    # Fallback: if the DB isn't built yet, read fmv_gap.json
    if not os.path.exists(db):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fmv_gap.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
            return jsonify({"items": items, "stats": {}, "note": "from fmv_gap.json"})
        return jsonify({"items": [], "stats": {}, "note": "collectiq.db not built yet"})

    conn = _sq.connect(db)
    conn.row_factory = _sq.Row
    try:
        limit = min(5000, int(request.args.get("limit", 1000)))
    except Exception:
        limit = 1000

    conf  = request.args.get("confidence","")
    where = []
    params: list = []
    if conf:
        where.append("index_confidence=?"); params.append(conf)
    min_gap = request.args.get("min_gap")
    max_gap = request.args.get("max_gap")
    if min_gap:
        where.append("fmv_gap_pct >= ?"); params.append(float(min_gap))
    if max_gap:
        where.append("fmv_gap_pct <= ?"); params.append(float(max_gap))

    wc = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(f"""
        SELECT token_id, name, set_name, grader, grade, serial,
               renaiss_fmv, index_price_usd, fmv_gap_pct, fmv_gap_usd,
               index_confidence, index_last_sale, index_href, index_spark,
               image_url, cert_item, cert_front,
               holder, holder_short, is_listed, ask_price
        FROM tokens {wc}
        ORDER BY fmv_gap_pct DESC NULLS LAST
        LIMIT ?
    """, params + [limit]).fetchall()

    # Stats
    stats = conn.execute("""
        SELECT COUNT(*) total_tokens,
               SUM(index_queried) queried,
               SUM(CASE WHEN fmv_gap_pct>100 THEN 1 ELSE 0 END) over_100pct,
               SUM(CASE WHEN fmv_gap_pct<-20 THEN 1 ELSE 0 END) under_20pct,
               ROUND(AVG(fmv_gap_pct),1) avg_gap_pct,
               ROUND(SUM(renaiss_fmv),0) total_renaiss_fmv,
               ROUND(SUM(index_price_usd),0) total_index_fmv
        FROM tokens
    """).fetchone()
    conn.close()

    return jsonify({
        "items": [dict(r) for r in rows],
        "stats": dict(stats),
    })


@app.route("/api/intelligence/wallets")
def api_intelligence_wallets():
    """CollectIQ whale-wallet summary (stats for wallets holding Renaiss tokens)."""
    import sqlite3 as _sq
    db  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "collectiq.db")
    alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "wallet_summary.json")
    if not os.path.exists(db):
        if os.path.exists(alt):
            with open(alt, encoding="utf-8") as f:
                return jsonify({"wallets": json.load(f)})
        return jsonify({"wallets": []})
    conn = _sq.connect(db)
    conn.row_factory = _sq.Row
    try:
        limit = min(500, int(request.args.get("limit", 200)))
    except Exception:
        limit = 200
    # Live aggregation from tokens table (always fresh)
    rows = conn.execute("""
        SELECT
            holder,
            MAX(holder_short) AS holder_short,
            COUNT(*) AS token_count,
            ROUND(SUM(renaiss_fmv),2) AS total_renaiss_fmv,
            ROUND(SUM(index_price_usd),2) AS total_index_fmv,
            ROUND(AVG(fmv_gap_pct),1) AS avg_fmv_gap_pct,
            (SELECT name FROM tokens t2 WHERE t2.holder=t.holder
             ORDER BY renaiss_fmv DESC LIMIT 1) AS top_card_name
        FROM tokens t
        WHERE holder IS NOT NULL AND holder != ''
        GROUP BY holder
        ORDER BY total_renaiss_fmv DESC
        LIMIT ?
    """, [limit]).fetchall()
    conn.close()
    return jsonify({"wallets": [dict(r) for r in rows]})


@app.route("/api/intelligence/token/<token_id>")
def api_intelligence_token(token_id):
    """Full CollectIQ analysis for a single token (FMV comparison + cert image + holder)."""
    import sqlite3 as _sq
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "collectiq.db")
    if not os.path.exists(db):
        return jsonify({"error": "collectiq.db not built yet"}), 404
    conn = _sq.connect(db); conn.row_factory = _sq.Row
    row = conn.execute("SELECT * FROM tokens WHERE token_id=?", [token_id]).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "token not found"}), 404
    return jsonify(dict(row))


@app.route("/api/intelligence/rebuild", methods=["POST"])
def api_intelligence_rebuild():
    """Trigger a background rebuild of collectiq.db (only queries certs not yet looked up)."""
    import subprocess as _sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "build_universe.py")
    _sp.Popen(
        ["python3", "-u", script],
        stdout=open("/tmp/collectiq-build2.log", "a"),
        stderr=_sp.STDOUT,
    )
    return jsonify({"status": "rebuild triggered", "log": "/tmp/collectiq-build2.log"})


@app.route("/api/build/status")
def api_build_status():
    """CollectIQ background build progress (DB stats + log tail + whether it's running)."""
    import sqlite3 as _sq, subprocess as _sp
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "collectiq.db")
    # Check if build process is running
    try:
        running_check = _sp.run(
            ["pgrep", "-f", "build_universe"],
            capture_output=True, text=True
        )
        is_running = bool(running_check.stdout.strip())
    except Exception:
        is_running = False

    stats = {}
    if os.path.exists(db):
        try:
            conn = _sq.connect(db)
            total = conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
            queried = conn.execute("SELECT COUNT(*) FROM tokens WHERE index_queried=1").fetchone()[0]
            priced = conn.execute("SELECT COUNT(*) FROM tokens WHERE index_price_usd IS NOT NULL AND index_price_usd > 0").fetchone()[0]
            conn.close()
            stats = {
                "total": total, "queried": queried, "pending": total - queried,
                "priced": priced,
                "pct": round(queried / total * 100, 1) if total else 0,
            }
        except Exception:
            pass

    # tail last 5 lines of log
    log_tail = ""
    try:
        with open("/tmp/collectiq-build2.log") as f:
            lines = f.readlines()
            log_tail = "".join(lines[-5:]).strip()
    except Exception:
        pass

    # Check if quota exhausted (Retry-After in log tail)
    quota_reset_utc = None
    if "quota exhausted" in log_tail or "resets" in log_tail:
        import re
        m = re.search(r'resets (\d{2}:\d{2} UTC)', log_tail)
        if m:
            quota_reset_utc = m.group(1)

    daily_quota = 95
    days_to_complete = round(stats.get("pending", 0) / daily_quota, 1) if stats else None

    return jsonify({
        "is_running": is_running,
        "stats": stats,
        "log_tail": log_tail,
        "daily_quota": daily_quota,
        "days_to_complete": days_to_complete,
        "quota_reset_utc": quota_reset_utc,
        "next_run": "daily at 08:10 Taiwan time (00:10 UTC)",
    })


@app.route("/verify")
@app.route("/compare")
def compare_page():
    """Price verification page: Renaiss internal price (FMV/listing) vs our independently scraped public market price."""
    return render_template("compare.html")


@app.route("/api/trust-score")
def api_trust_score():
    """CollectIQ platform trust score — one API returning all the key numbers the dashboard needs."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "comparison.json")
    if not os.path.exists(path):
        return jsonify({"error": "comparison.json not built yet"})
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("rows", [])
    total = len(rows)
    priced = [r for r in rows if r.get("our_price") is not None]
    if not priced:
        return jsonify({"error": "no priced cards"})
    coverage_pct = round(len(priced) / total * 100, 1) if total else 0
    within_30 = [r for r in priced if r.get("delta_pct") is not None and abs(r["delta_pct"]) <= 30]
    trust_score = round(len(within_30) / len(priced) * 100, 1)
    within_10 = [r for r in priced if r.get("delta_pct") is not None and abs(r["delta_pct"]) <= 10]
    strict_accuracy = round(len(within_10) / len(priced) * 100, 1)
    abs_devs = [abs(r["delta_pct"]) for r in priced if r["delta_pct"] is not None]
    avg_dev = round(sum(abs_devs) / len(abs_devs), 1) if abs_devs else 0
    eggs = [r for r in priced if r.get("flag") == "renaiss_low"]
    egg_gap_usd = round(sum(
        (r.get("our_price", 0) or 0) - (r.get("renaiss_fmv", 0) or 0)
        for r in eggs if r.get("our_price") and r.get("renaiss_fmv")
    ), 2)
    top_eggs = sorted(
        [r for r in eggs if r.get("delta_pct")],
        key=lambda r: r["delta_pct"], reverse=True
    )[:5]
    flags = data.get("summary", {}).get("flags", {})
    total_fmv = round(sum(r.get("renaiss_fmv", 0) or 0 for r in rows), 2)
    return jsonify({
        "generated_at": data.get("generated_at"),
        "total_cards": total,
        "coverage": len(priced),
        "coverage_pct": coverage_pct,
        "trust_score": trust_score,
        "strict_accuracy": strict_accuracy,
        "avg_deviation": avg_dev,
        "easter_eggs": len(eggs),
        "easter_egg_gap_usd": egg_gap_usd,
        "total_fmv_usd": total_fmv,
        "flags": flags,
        "top_eggs": [{
            "name": r.get("name", "")[:60],
            "delta_pct": r["delta_pct"],
            "renaiss_fmv": r.get("renaiss_fmv"),
            "market_price": r.get("our_price"),
            "grader": r.get("grader"),
            "grade": r.get("grade"),
        } for r in top_eggs],
    })


@app.route("/api/comparison")
def api_comparison():
    """Return data/comparison.json (produced by build_comparison.py).

    Supports ?flag=renaiss_high filtering; ?min_delta=20 shows only rows with absolute deviation >=20%.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "comparison.json")
    if not os.path.exists(path):
        return jsonify({"error": "comparison.json 尚未生成，請先跑 scripts/build_comparison.py",
                        "rows": [], "summary": {}}), 200
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("rows", [])
    flag = request.args.get("flag")
    if flag:
        rows = [r for r in rows if r.get("flag") == flag]
    min_delta = request.args.get("min_delta")
    if min_delta:
        try:
            md = float(min_delta)
            rows = [r for r in rows if r.get("delta_pct") is not None and abs(r["delta_pct"]) >= md]
        except ValueError:
            pass
    data = dict(data)
    data["rows"] = rows
    return jsonify(data)


@app.route("/api/our-price")
def api_our_price():
    """Live lookup of a single card's "self-scraped" external price (never touches Renaiss sources).

    ?token_id=... first finds the card in marketplace_all.json, then compares prices;
    or pass ?name=&set=&char=&number=&grader=&grade= directly for a custom query.
    """
    base = os.path.dirname(os.path.abspath(__file__))
    token_id = request.args.get("token_id")
    card = None
    if token_id:
        path = os.path.join(base, "data", "marketplace_all.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for c in json.load(f):
                    if str(c.get("token_id")) == str(token_id):
                        card = c
                        break
    if card is None:
        card = {
            "name": request.args.get("name", ""),
            "set_name": request.args.get("set", ""),
            "character_name": request.args.get("char", ""),
            "card_number": request.args.get("number", ""),
            "grader": request.args.get("grader", ""),
            "grade": request.args.get("grade", ""),
            "token_id": token_id or "",
        }
    chk = OurPriceChecker()
    our = chk.get_independent_price(card)
    chk.save_cache()
    return jsonify(compare_card(card, our))


@app.route("/api/pool-data")
def api_pool_data():
    """Fetch pool data from JSON"""
    pool_data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pool_data.json")
    loader = LocalPoolDataLoader(pool_data_path)
    pool_data = loader.load_pool_data()

    if not pool_data:
        pool_data = [
            {"card_id": "E001", "name": "Legendary Eagle", "remaining_quantity": 5, "market_price": 1500.00},
            {"card_id": "E002", "name": "Mystic Phoenix", "remaining_quantity": 15, "market_price": 500.00},
            {"card_id": "E003", "name": "Shadow Wolf", "remaining_quantity": 50, "market_price": 100.00},
        ]

    return jsonify(pool_data)


@app.route("/api/ev-calculate")
def api_ev_calculate():
    """Calculate EV from pool data"""
    pool_data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pool_data.json")
    loader = LocalPoolDataLoader(pool_data_path)
    pool_data = loader.load_pool_data()

    if not pool_data:
        pool_data = [
            {"card_id": "E001", "name": "Legendary Eagle", "remaining_quantity": 5, "market_price": 1500.00},
            {"card_id": "E002", "name": "Mystic Phoenix", "remaining_quantity": 15, "market_price": 500.00},
            {"card_id": "E003", "name": "Shadow Wolf", "remaining_quantity": 50, "market_price": 100.00},
        ]

    calculator = EVCalculator(pool_data)
    ev_df = calculator.calculate_all_ev()

    return jsonify(ev_df.to_dict(orient="records"))


@app.route("/api/external-price")
def api_external_price():
    """Get external price for a card"""
    card_name = request.args.get("name")
    card_id = request.args.get("id")

    if not card_name:
        return jsonify({"error": "card_name required"}), 400

    checker = ExternalPriceChecker()
    result = checker.get_market_price(card_name, card_id)

    return jsonify(result)


@app.route("/api/update-pool", methods=["POST"])
def api_update_pool():
    """Update pool data"""
    try:
        data = request.get_json()
        pool_data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pool_data.json")
        os.makedirs(os.path.dirname(pool_data_path), exist_ok=True)
        with open(pool_data_path, "w") as f:
            json.dump(data, f, indent=2)
        return jsonify({"status": "success", "message": "Pool data updated"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/pack-data")
def api_pack_data():
    """Fetch pack data — prefer live data from the Renaiss CLI, fall back to local JSON."""
    try:
        # Try live CLI data
        live_packs = renaiss_api.get_all_packs_ev()
        if live_packs:
            return jsonify({"results": live_packs, "source": "renaiss-cli-live"})
    except Exception as e:
        print(f"[WARN] CLI pack data failed, falling back: {e}")

    # Fallback: local JSON -> MOCK
    pack_data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pack_data.json")
    loader = LocalPoolDataLoader(pack_data_path)
    pack_data = loader.load_pool_data()
    if not pack_data:
        pack_data = MOCK_PACKS
    analyzer = PackPoolAnalyzer(pack_data)
    analysis = analyzer.analyze_all()
    return jsonify(analysis)


@app.route("/api/packs/live")
def api_packs_live():
    """Renaiss pack data.
    ?all=1 returns everything (including archived limited + coming_soon); defaults to active only.
    """
    try:
        show_all = request.args.get("all", "0") == "1"
        all_packs = renaiss_api.get_packs(include_inactive=True)
        results = []
        for p in all_packs:
            slug = p.get("slug", "")
            stage = p.get("stage", "")
            # coming_soon / archived -> return basic data only, don't call compute_pack_ev (would 404)
            if stage in ("archived", "coming_soon"):
                if not show_all:
                    continue
                results.append({
                    "slug": slug,
                    "name": p.get("name", slug),
                    "pack_type": p.get("packType"),
                    "stage": stage,
                    "price_usd": p.get("price_usd"),
                    "official_ev_usd": p.get("official_ev_usd"),
                    "featured_fmv_usd": p.get("featured_fmv_usd"),
                    "ev_ratio": p.get("ev_ratio"),
                    "supply_hint": p.get("supply_hint"),
                    "empirical_ev_usd": None,
                    "ev_delta_pct": None,
                    "recent_pulls_n": 0,
                    "tier_distribution": {},
                })
            else:
                # active perpetual -> full EV calculation
                ev = renaiss_api.compute_pack_ev(slug)
                ev["supply_hint"] = p.get("supply_hint")
                results.append(ev)

        return jsonify({
            "packs": results,
            "total": len(results),
            "active": sum(1 for r in results if r.get("stage") == "active"),
            "generated_at": datetime.utcnow().isoformat() + "Z",
        })
    except Exception as e:
        return jsonify({"error": str(e), "packs": []}), 500


@app.route("/api/packs/live/<slug>")
def api_pack_live_detail(slug):
    """Full live data for a single pack (including the last 20+ opened-pack fmv/tier entries)."""
    try:
        detail = renaiss_api.get_pack_detail(slug)
        ev = renaiss_api.compute_pack_ev(slug)
        return jsonify({"detail": detail, "ev": ev})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/market/search")
def api_market_search():
    """Index API card search — ?q=card_name&limit=10, returns price_usd / grade / spark."""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"error": "q must be ≥ 2 chars"}), 400
    try:
        limit = max(1, min(50, int(request.args.get("limit", 10))))
    except Exception:
        limit = 10
    results = renaiss_api.search_cards(q, limit=limit)
    return jsonify({"query": q, "results": results})


@app.route("/api/market/price/<cert>")
def api_market_price_cert(cert):
    """Index API lookup by grading serial (PSA/BGS/CGC cert) -> card name + live FMV."""
    data = renaiss_api.get_graded(cert)
    return jsonify(data)


@app.route("/api/market/price-by-id/<rid>")
def api_market_price_by_rid(rid):
    """Index API lookup of a card's live FMV by Renaiss tokenId."""
    data = renaiss_api.get_card_by_renaiss_id(rid)
    return jsonify(data)


@app.route("/api/market/fmv-series/<rid>")
def api_market_fmv_series(rid):
    """Index API lookup of a card's daily FMV history (for trend charts)."""
    series = renaiss_api.get_card_fmv_series(rid)
    return jsonify({"rid": rid, "series": series})


@app.route("/api/trades/live")
def api_trades_live():
    """Index API recent cross-platform trade records (snkrdunk, etc.). ?limit=50"""
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except Exception:
        limit = 50
    trades = renaiss_api.get_recent_trades(limit=limit)
    return jsonify({"trades": trades, "count": len(trades)})


@app.route("/api/index/<game>")
def api_index_overview(game):
    """Index API index overview (game = pokemon / one-piece)."""
    if game not in ("pokemon", "one-piece"):
        return jsonify({"error": "game must be pokemon or one-piece"}), 400
    data = renaiss_api.get_index_overview(game)
    return jsonify(data)


@app.route("/api/cache/stats")
def api_cache_stats():
    """Diagnostics: API cache status (age of each key, whether it's expired)."""
    return jsonify(renaiss_api.cache_stats())


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    """Force-clear the API cache (so the next request re-fetches from the CLI/Index API)."""
    renaiss_api.clear_cache()
    return jsonify({"status": "cleared"})


@app.route("/oracle")
def oracle_page():
    """Staking Oracle concept page: CollectIQ as a collateral-pricing oracle."""
    return render_template("oracle.html")


@app.route("/api/oracle/simulate")
def api_oracle_simulate():
    """Simulate the Oracle's collateral parameters for each card: verified_price, confidence, LTV, liquidation."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "comparison.json")
    if not os.path.exists(path):
        return jsonify({"error": "comparison.json not built yet"})
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("rows", [])
    priced = [r for r in rows if r.get("our_price") is not None and r.get("renaiss_fmv")]

    def _oracle_entry(r):
        delta = abs(r["delta_pct"]) if r.get("delta_pct") is not None else 999
        if delta <= 10:
            confidence = "high"
            ltv = 0.70
        elif delta <= 30:
            confidence = "medium"
            ltv = 0.50
        else:
            confidence = "low"
            ltv = 0.30
        verified_price = r["our_price"]
        collateral_value = round(verified_price * ltv, 2)
        liquidation_price = round(verified_price * 0.80, 2)
        return {
            "name": (r.get("name") or "")[:60],
            "grader": r.get("grader"),
            "grade": r.get("grade"),
            "renaiss_fmv": r["renaiss_fmv"],
            "verified_price": verified_price,
            "delta_pct": r.get("delta_pct"),
            "confidence": confidence,
            "ltv": ltv,
            "collateral_value": collateral_value,
            "liquidation_price": liquidation_price,
            "image_url": r.get("image_url"),
            "flag": r.get("flag"),
        }

    oracle_rows = [_oracle_entry(r) for r in priced]
    oracle_rows.sort(key=lambda x: x["collateral_value"], reverse=True)

    total_verified = len(oracle_rows)
    total_collateral = round(sum(r["collateral_value"] for r in oracle_rows), 2)
    by_conf = {}
    for r in oracle_rows:
        c = r["confidence"]
        by_conf[c] = by_conf.get(c, 0) + 1

    return jsonify({
        "total_verified": total_verified,
        "total_collateral_value": total_collateral,
        "by_confidence": by_conf,
        "avg_ltv": round(sum(r["ltv"] for r in oracle_rows) / len(oracle_rows), 3) if oracle_rows else 0,
        "rows": oracle_rows[:200],
        "generated_at": data.get("generated_at"),
    })


@app.route("/api-status")
def api_status_page():
    """API verification page: hit each endpoint one by one to confirm availability."""
    return render_template("api_status.html")


@app.route("/price-search")
def price_search_page():
    """PriceCharting live price-comparison search page."""
    return render_template("price_search.html")


def _pricecharting_list(query: str):
    """Return a list of PriceCharting search results (up to 20), each with title + url."""
    import re as _re
    from bs4 import BeautifulSoup as _BS
    import requests as _req

    _UA_PC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

    session = _req.Session()
    session.headers.update({"User-Agent": _UA_PC, "Accept": "text/html"})

    url = f"https://www.pricecharting.com/search-products?q={_req.utils.quote(query)}&type=prices"
    try:
        r = session.get(url, timeout=20, allow_redirects=True)
    except _req.RequestException:
        return [], None

    if r.status_code != 200:
        return [], None

    # Redirected directly to a product page
    if "/game/" in r.url and "/search-products" not in r.url:
        from bs4 import BeautifulSoup as BS2
        soup2 = BS2(r.text, "html.parser")
        title_el = soup2.find("title")
        title = title_el.get_text(strip=True) if title_el else r.url.split("/")[-1]
        return [{"title": title, "url": r.url}], r.url

    soup = _BS(r.text, "html.parser")
    results = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/game/" not in href:
            continue
        label = a.get_text(strip=True)
        if not label:  # skip icon/image links with no text
            continue
        if not href.startswith("http"):
            href = "https://www.pricecharting.com" + href
        if href in seen:
            continue
        seen.add(href)
        results.append({"title": label, "url": href})
        if len(results) >= 20:
            break
    return results, None


def _pricecharting_search_by_url(url: str):
    """Scrape the per-grade prices from a specific PriceCharting product page."""
    import re as _re
    from bs4 import BeautifulSoup as _BS
    import requests as _req

    _UA_PC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

    PC_ID_TO_GRADE = {
        "used_price": "Ungraded",
        "complete_price": "Grade 7",
        "new_price": "Grade 8",
        "graded_price": "Grade 9",
        "box_only_price": "Grade 9.5",
        "manual_only_price": "PSA 10",
    }

    def _parse_price(text):
        m = _re.search(r"\$([\d,]+\.?\d*)", text or "")
        if not m:
            return None
        try:
            v = float(m.group(1).replace(",", ""))
            return v if v > 0 else None
        except ValueError:
            return None

    def _scrape_product_page(url, session):
        r = session.get(url, timeout=20, allow_redirects=True)
        if r.status_code != 200:
            return None
        soup = _BS(r.text, "html.parser")
        if not soup.find(id="price_data"):
            return None
        grades = {}
        for cell_id, label in PC_ID_TO_GRADE.items():
            el = soup.find(id=cell_id)
            if not el:
                continue
            price = _parse_price(el.get_text(" ", strip=True))
            if price is not None:
                grades[label] = {"price": price}
        title_el = soup.find("title")
        return {
            "grades": grades,
            "matched_title": title_el.get_text(strip=True) if title_el else None,
            "source_url": r.url,
        }

    session = _req.Session()
    session.headers.update({"User-Agent": _UA_PC, "Accept": "text/html"})
    return _scrape_product_page(url, session)


@app.route("/api/price-search")
def api_price_search():
    """Search PriceCharting.
    Required: ?q=keyword
    Optional: ?grader=PSA|BGS|CGC  ?grade=10|9.5|...
    Returns: results (candidate list) or prices directly (on a unique match).
    """
    import re as _re
    from our_price import _grade_label
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "q 參數必填"}), 400

    grader = request.args.get("grader", "").strip()
    grade  = request.args.get("grade", "").strip()

    query = q if _re.search(r"\bpokemon\b", q, _re.I) else "pokemon " + q
    results, direct_url = _pricecharting_list(query)

    # Direct match — immediately fetch prices
    if direct_url or len(results) == 1:
        url = direct_url or results[0]["url"]
        pc = _pricecharting_search_by_url(url)
        grade_label = _grade_label(grader, grade)
        our_price = None
        grade_matched = None
        if pc and pc.get("grades"):
            grades = pc["grades"]
            if grade_label and grade_label in grades:
                grade_matched = grade_label
                our_price = grades[grade_label]["price"]
            elif "PSA 10" in grades:
                grade_matched = "PSA 10"
                our_price = grades["PSA 10"]["price"]
        return jsonify({
            "mode": "prices",
            "query": query,
            "matched_title":  pc.get("matched_title") if pc else None,
            "grade_matched":  grade_matched,
            "grade_requested": grade_label,
            "our_price":      our_price,
            "all_grades":     pc.get("grades", {}) if pc else {},
            "source":         "pricecharting" if pc else None,
            "source_url":     pc.get("source_url") if pc else None,
            "sources":        1 if our_price else 0,
        })

    # Multiple results — return list for user to pick
    return jsonify({
        "mode":    "list",
        "query":   query,
        "results": results,
        "grader":  grader,
        "grade":   grade,
    })


@app.route("/api/price-by-url")
def api_price_by_url():
    """Scrape the per-grade prices from a specific PriceCharting product page.
    Required: ?url=https://www.pricecharting.com/game/...
    Optional: ?grader=PSA  ?grade=10
    """
    from our_price import _grade_label
    url = request.args.get("url", "").strip()
    if not url or "pricecharting.com" not in url:
        return jsonify({"error": "url 必須是 pricecharting.com 的連結"}), 400

    grader = request.args.get("grader", "").strip()
    grade  = request.args.get("grade", "").strip()

    pc = _pricecharting_search_by_url(url)
    grade_label = _grade_label(grader, grade)
    our_price = None
    grade_matched = None
    if pc and pc.get("grades"):
        grades = pc["grades"]
        if grade_label and grade_label in grades:
            grade_matched = grade_label
            our_price = grades[grade_label]["price"]
        elif "PSA 10" in grades:
            grade_matched = "PSA 10"
            our_price = grades["PSA 10"]["price"]

    return jsonify({
        "mode": "prices",
        "query": url,
        "matched_title":  pc.get("matched_title") if pc else None,
        "grade_matched":  grade_matched,
        "grade_requested": grade_label,
        "our_price":      our_price,
        "all_grades":     pc.get("grades", {}) if pc else {},
        "source":         "pricecharting" if pc else None,
        "source_url":     pc.get("source_url") if pc else None,
        "sources":        1 if our_price else 0,
    })


@app.route("/cdp")
def cdp_page():
    """CDP simulator page: interactively pick cards -> see the borrowing limit in real time."""
    return render_template("cdp.html")


@app.route("/api/cdp/cards")
def api_cdp_cards():
    """Return the list of all collateralizable cards (those with an independently verified price), for the CDP simulator to pick from."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "comparison.json")
    if not os.path.exists(path):
        return jsonify({"error": "comparison.json not built yet"})
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("rows", [])
    priced = [r for r in rows if r.get("our_price") is not None and r.get("renaiss_fmv")]

    cards = []
    for r in priced:
        delta = abs(r["delta_pct"]) if r.get("delta_pct") is not None else 999
        if delta <= 10:
            confidence, ltv = "high", 0.70
        elif delta <= 30:
            confidence, ltv = "medium", 0.50
        else:
            confidence, ltv = "low", 0.30
        vp = r["our_price"]
        cards.append({
            "token_id": r.get("token_id"),
            "name": r.get("name", ""),
            "grader": r.get("grader"),
            "grade": r.get("grade"),
            "image_url": r.get("image_url"),
            "renaiss_fmv": r["renaiss_fmv"],
            "verified_price": vp,
            "delta_pct": r.get("delta_pct"),
            "confidence": confidence,
            "ltv": ltv,
            "max_borrow": round(vp * ltv, 2),
            "liquidation_price": round(vp * 0.80, 2),
            "flag": r.get("flag"),
            "source_url": r.get("source_url"),
        })
    cards.sort(key=lambda x: x["max_borrow"], reverse=True)
    return jsonify({"cards": cards, "total": len(cards)})


@app.route("/rwa-index")
def rwa_index_page():
    """RWA index page: price indices by set / IP."""
    return render_template("rwa_index.html")


@app.route("/api/rwa-index")
def api_rwa_index():
    """Produce the RWA index: price statistics grouped by set_name."""
    import sqlite3
    base = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(base, "data", "collectiq_core.db")
    if not os.path.exists(db_path):
        return jsonify({"error": "collectiq_core.db not found"})

    comp_path = os.path.join(base, "data", "comparison.json")
    verified_map = {}
    if os.path.exists(comp_path):
        with open(comp_path, encoding="utf-8") as f:
            comp = json.load(f)
        for r in comp.get("rows", []):
            if r.get("our_price") is not None:
                verified_map[r.get("token_id")] = r

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    cards = db.execute("""
        SELECT d.token_id, d.name, d.set_name, d.grader, d.grade,
               f.renaiss_fmv, f.index_price_usd
        FROM dim_card d
        LEFT JOIN fmv_snapshots f ON d.token_id = f.token_id
        WHERE d.set_name IS NOT NULL
    """).fetchall()

    series = {}
    for c in cards:
        sn = c["set_name"]
        if sn not in series:
            series[sn] = {
                "set_name": sn,
                "ip": "Pokemon" if "Pokemon" in sn else ("One Piece" if "One Piece" in sn else "Other"),
                "cards": 0,
                "verified": 0,
                "total_fmv": 0,
                "total_verified_price": 0,
                "total_delta": 0,
                "high_count": 0,
                "low_count": 0,
                "match_count": 0,
            }
        s = series[sn]
        s["cards"] += 1
        fmv = c["renaiss_fmv"] or 0
        s["total_fmv"] += fmv
        tid = c["token_id"]
        if tid in verified_map:
            v = verified_map[tid]
            s["verified"] += 1
            s["total_verified_price"] += v["our_price"]
            if v.get("delta_pct") is not None:
                s["total_delta"] += abs(v["delta_pct"])
            flag = v.get("flag", "")
            if flag == "renaiss_high":
                s["high_count"] += 1
            elif flag == "renaiss_low":
                s["low_count"] += 1
            elif flag == "match":
                s["match_count"] += 1

    indices = []
    for sn, s in series.items():
        if s["cards"] < 3:
            continue
        avg_fmv = round(s["total_fmv"] / s["cards"], 2) if s["cards"] else 0
        avg_verified = round(s["total_verified_price"] / s["verified"], 2) if s["verified"] else None
        avg_delta = round(s["total_delta"] / s["verified"], 1) if s["verified"] else None
        trust = round(
            ((s["match_count"] + s["low_count"]) / s["verified"]) * 100, 1
        ) if s["verified"] else None
        indices.append({
            "set_name": sn,
            "ip": s["ip"],
            "cards": s["cards"],
            "verified": s["verified"],
            "coverage_pct": round(s["verified"] / s["cards"] * 100, 1) if s["cards"] else 0,
            "avg_fmv": avg_fmv,
            "avg_verified_price": avg_verified,
            "avg_delta": avg_delta,
            "trust_score": trust,
            "high_count": s["high_count"],
            "low_count": s["low_count"],
            "match_count": s["match_count"],
            "total_fmv": round(s["total_fmv"], 2),
            "total_verified": round(s["total_verified_price"], 2),
        })
    db.close()

    indices.sort(key=lambda x: x["cards"], reverse=True)

    ip_summary = {}
    for idx in indices:
        ip = idx["ip"]
        if ip not in ip_summary:
            ip_summary[ip] = {"ip": ip, "series": 0, "cards": 0, "total_fmv": 0, "verified": 0}
        ip_summary[ip]["series"] += 1
        ip_summary[ip]["cards"] += idx["cards"]
        ip_summary[ip]["total_fmv"] += idx["total_fmv"]
        ip_summary[ip]["verified"] += idx["verified"]

    return jsonify({
        "indices": indices,
        "ip_summary": list(ip_summary.values()),
        "total_series": len(indices),
        "total_cards": sum(i["cards"] for i in indices),
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  Single-card price lookup + wallet tracking (for the public renaiss_lookup.html)
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/card-lookup")
def api_card_lookup():
    """A single card, "three sources in one query": paste a card name -> return side by side
      1) independent: PriceCharting (aggregated eBay sales, with verifiable links) = credible
      2) renaiss_index: Renaiss's own index FMV (Index API, not an independent third party)
      3) renaiss_buyback: Renaiss platform buyback price (from pack_content, per entry) + luck
    Params: ?q=card_name (required) ?grader=PSA ?grade=10
    Integrity rule per CLAUDE.md: independent and renaiss_index must not be mixed; label each with its source."""
    from our_price import _grade_label
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "q 參數必填（卡片名稱）"}), 400
    grader = request.args.get("grader", "").strip()
    grade = request.args.get("grade", "").strip()

    out = {"query": q, "grader": grader, "grade": grade,
           "independent": None, "renaiss_index": None, "renaiss_buyback": None}

    # ── 1) Independent source: PriceCharting (reuse existing helper) ──
    try:
        import re as _re
        query = q if _re.search(r"\bpokemon\b", q, _re.I) else "pokemon " + q
        results, direct_url = _pricecharting_list(query)
        if direct_url or (results and len(results) == 1):
            url = direct_url or results[0]["url"]
            pc = _pricecharting_search_by_url(url)
            glabel = _grade_label(grader, grade)
            price = None
            matched = None
            if pc and pc.get("grades"):
                g = pc["grades"]
                if glabel and glabel in g:
                    matched, price = glabel, g[glabel]["price"]
                elif "PSA 10" in g:
                    matched, price = "PSA 10", g["PSA 10"]["price"]
            out["independent"] = {
                "source": "pricecharting_ebay", "label": "PriceCharting（彙整 eBay 成交）",
                "price": price, "grade_matched": matched,
                "all_grades": pc.get("grades", {}) if pc else {},
                "url": pc.get("source_url") if pc else url,
                "matched_title": pc.get("matched_title") if pc else None,
                "independent": True,
            }
        elif results:
            out["independent"] = {"source": "pricecharting_ebay", "candidates": results[:12],
                                  "independent": True, "note": "多筆候選，請點選正確卡"}
    except Exception as e:
        out["independent"] = {"error": f"{type(e).__name__}: {e}"}

    # ── 2) Renaiss's own index FMV (Index API, not independent) ──
    try:
        hits = renaiss_api.search_cards(q, limit=6)
        out["renaiss_index"] = {
            "source": "renaiss_index", "label": "Renaiss 自家指數（非獨立第三方）",
            "independent": False,
            "results": [{"name": h.get("name"), "grade": h.get("gradeLabel"),
                         "price_usd": h.get("price_usd")} for h in hits],
        }
    except Exception as e:
        out["renaiss_index"] = {"error": f"{type(e).__name__}: {e}"}

    # ── 3) Renaiss buyback price + luck (pack_content, per entry) ──
    try:
        conn = _core_db()
        if conn:
            rows = conn.execute(
                """SELECT name, grader, grade, tier, pack_name,
                          renaiss_buyback_usd, market_price_usd, luck_value,
                          market_source, market_url, token_id
                   FROM pack_content
                   WHERE name LIKE ? AND renaiss_buyback_usd IS NOT NULL
                   ORDER BY renaiss_buyback_usd DESC LIMIT 8""",
                (f"%{q}%",)).fetchall()
            conn.close()
            out["renaiss_buyback"] = {
                "source": "pack_content",
                "label": "Renaiss 收購價（回購）＋ luck = 市價 / 收購",
                "results": [dict(r) for r in rows],
            }
    except Exception as e:
        out["renaiss_buyback"] = {"error": f"{type(e).__name__}: {e}"}

    return jsonify(out)


@app.route("/api/wallet/<addr>")
def api_wallet(addr):
    """Wallet tracking: given an address, return its activity in the Renaiss ecosystem
      · pulls   : cards pulled from packs (is_mint=1, to=addr) + FMV
      · received: cards transferred in from other wallets (is_mint=0, to=addr)
      · sent    : cards transferred out (from=addr)
      · listings: marketplace listing records (ledger_market, includes list price)
    Params: ?limit=100"""
    import sqlite3 as _sq
    addr = (addr or "").strip().lower()
    if not addr.startswith("0x") or len(addr) != 42:
        return jsonify({"error": "地址格式不正確（需 0x + 40 hex）"}), 400
    limit = min(int(request.args.get("limit", 100)), 500)
    base = os.path.dirname(os.path.abspath(__file__))

    out = {"address": addr, "pulls": [], "received": [], "sent": [],
           "listings": [], "summary": {}}

    # onchain_pulls: pulls / received / sent
    ocdb = os.path.join(base, "data", "onchain_pulls.db")
    if os.path.exists(ocdb):
        oc = _sq.connect(ocdb); oc.row_factory = _sq.Row
        def _q(where, params):
            return [dict(r) for r in oc.execute(
                f"""SELECT token_id, card_name, set_name, market_fmv,
                           from_addr, to_addr, is_mint, block_time, tx_hash
                    FROM onchain_pulls WHERE {where}
                    ORDER BY block_time DESC LIMIT ?""", params).fetchall()]
        out["pulls"]    = _q("LOWER(to_addr)=? AND is_mint=1", (addr, limit))
        out["received"] = _q("LOWER(to_addr)=? AND is_mint=0", (addr, limit))
        out["sent"]     = _q("LOWER(from_addr)=?", (addr, limit))
        # Summary stats (full dataset, unaffected by limit)
        s = oc.execute(
            """SELECT
                 SUM(CASE WHEN LOWER(to_addr)=? AND is_mint=1 THEN 1 ELSE 0 END) AS n_pulls,
                 SUM(CASE WHEN LOWER(to_addr)=? AND is_mint=1 THEN COALESCE(market_fmv,0) ELSE 0 END) AS pull_fmv,
                 SUM(CASE WHEN LOWER(to_addr)=? AND is_mint=0 THEN 1 ELSE 0 END) AS n_recv,
                 SUM(CASE WHEN LOWER(from_addr)=? THEN 1 ELSE 0 END) AS n_sent
               FROM onchain_pulls""",
            (addr, addr, addr, addr)).fetchone()
        out["summary"] = {"pulls": s["n_pulls"] or 0,
                          "pull_fmv_total": round(s["pull_fmv"] or 0, 2),
                          "received": s["n_recv"] or 0, "sent": s["n_sent"] or 0}
        oc.close()

    # ledger_market: this address's listings (intent to sell)
    conn = _core_db()
    if conn:
        try:
            rows = conn.execute(
                """SELECT token_id, serial, event_kind, price_usd, seller, buyer, ts, source
                   FROM ledger_market
                   WHERE LOWER(seller)=? OR LOWER(buyer)=?
                   ORDER BY ts DESC LIMIT ?""",
                (addr, addr, limit)).fetchall()
            out["listings"] = [dict(r) for r in rows]
        except Exception:
            pass
        conn.close()

    return jsonify(out)


# slug -> gacha pool contract (when is_mint=1, from_addr = these pools). Aligned with
# track_pulls_onchain.py's GACHA_CONTRACTS; limited/custody packs (World Cup) use vault custody and have no public on-chain pool feed.
_POOL_BY_SLUG = {
    "omega":          "0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910",
    "eden-pack":      "0xfda4a907d23d9f24271bc47483c5b983831e325e",
    "renacrypt-pack": "0xb2891022648c5fad3721c42c05d8d283d4d53080",
}


# Shared ERC721 NFT contract (all packs' cards are minted here)
_NFT_CONTRACT = "0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30"
_INV_CACHE = {}  # pool -> (ts, balance)


def _rpc_nodes():
    """Prefer BNB_RPC (with key, comma/space separated); otherwise fall back to public dataseed nodes."""
    env = os.getenv("BNB_RPC", "").replace(",", " ").split()
    return [r for r in env if r] or [
        "https://bsc-dataseed.binance.org",
        "https://bsc-dataseed1.defibit.io",
        "https://bsc-dataseed1.ninicoin.io",
    ]


def _pool_inventory(pool_addr, ttl=60):
    """How many NFTs this pack's pool still holds on-chain (= how many are left in the pool now). Cached 60s."""
    import requests as _req
    p = pool_addr.lower()
    now = time.time()
    hit = _INV_CACHE.get(p)
    if hit and now - hit[0] < ttl:
        return hit[1]
    data = "0x70a08231" + p[2:].rjust(64, "0")  # balanceOf(address)
    bal = None
    for node in _rpc_nodes():
        try:
            r = _req.post(node, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                          "params": [{"to": _NFT_CONTRACT, "data": data}, "latest"]}, timeout=8)
            res = r.json().get("result")
            if res and res != "0x":
                bal = int(res, 16); break
        except Exception:
            continue
    if bal is not None:
        _INV_CACHE[p] = (now, bal)
    return bal


def _pool_activity(pool_addr, recent_n=25):
    """Compute for a single gacha pool from onchain_pulls: total pulls, recent feed, high-value threshold (p99),
    high-value pull frequency, and the longest-dry-spell interval leaderboard. All real on-chain data."""
    import sqlite3 as _sq
    base = os.path.dirname(os.path.abspath(__file__))
    ocdb = os.path.join(base, "data", "onchain_pulls.db")
    if not os.path.exists(ocdb):
        return None
    oc = _sq.connect(ocdb); oc.row_factory = _sq.Row
    p = pool_addr.lower()
    total = oc.execute(
        "SELECT COUNT(*) n, MAX(block_time) t FROM onchain_pulls "
        "WHERE LOWER(from_addr)=? AND is_mint=1", (p,)).fetchone()
    if not total["n"]:
        oc.close(); return None
    # High-value threshold: the 99th percentile of this pool's fmv (dynamic, real)
    fmvs = [r[0] for r in oc.execute(
        "SELECT market_fmv FROM onchain_pulls WHERE LOWER(from_addr)=? AND is_mint=1 "
        "AND market_fmv IS NOT NULL ORDER BY market_fmv", (p,)).fetchall()]
    hi_thr = fmvs[int(len(fmvs) * 0.99)] if fmvs else 0
    hi_n = sum(1 for v in fmvs if v >= hi_thr) if hi_thr else 0
    # Full history (chronological) for the interval leaderboard; only pull necessary columns
    seq = oc.execute(
        "SELECT block_time, market_fmv, card_name, token_id, to_addr, tx_hash "
        "FROM onchain_pulls WHERE LOWER(from_addr)=? AND is_mint=1 "
        "ORDER BY block_number ASC, log_index ASC", (p,)).fetchall()
    # Longest-interval leaderboard: how many pulls elapsed between two high-value pulls
    leaderboard, since = [], 0
    for r in seq:
        since += 1
        if r["market_fmv"] is not None and hi_thr and r["market_fmv"] >= hi_thr:
            leaderboard.append({"gap": since, "card": r["card_name"],
                                "ts": r["block_time"], "token_id": r["token_id"]})
            since = 0
    leaderboard.sort(key=lambda x: x["gap"], reverse=True)
    recent = [{"ts": r["block_time"], "fmv": r["market_fmv"], "card": r["card_name"],
               "wallet": r["to_addr"], "token_id": r["token_id"], "tx": r["tx_hash"]}
              for r in seq[-recent_n:][::-1]]
    oc.close()
    return {
        "total_pulls": total["n"], "last_pull_ts": total["t"],
        "hi_threshold": hi_thr, "hi_count": hi_n,
        "avg_interval": round(total["n"] / hi_n, 1) if hi_n else None,
        "recent": recent, "leaderboard": leaderboard[:5],
    }


@app.route("/api/pack-activity")
def api_pack_activity():
    """Live activity for all packs: total pulls / recent pull feed (with wallets) / high-value pull frequency /
    longest-interval leaderboard. Perpetual pools use real on-chain data; limited/custody (World Cup) use vault
    custody with no public on-chain pull feed, so /api/prize-pool shows their prize-pool contents instead."""
    try:
        packs = renaiss_api.get_packs(include_inactive=False)
    except Exception as e:
        return jsonify({"error": f"取卡機清單失敗：{e}"}), 502
    machines = []
    for pk in packs:
        slug = pk.get("slug")
        m = {"slug": slug, "name": pk.get("name"),
             "packType": pk.get("packType"), "stage": pk.get("stage"),
             "price_usd": pk.get("price_usd"),
             "ev_usd": pk.get("official_ev_usd"),
             "featured_fmv_usd": pk.get("featured_fmv_usd"),
             "pool_addr": _POOL_BY_SLUG.get(slug),
             "custody": slug not in _POOL_BY_SLUG, "onchain": None}
        pool = _POOL_BY_SLUG.get(slug)
        if pool:
            m["pool_inventory"] = _pool_inventory(pool)
            act = _pool_activity(pool)
            if act:
                # Fill in the official tier from Renaiss recentOpenedPacks (matched to the recent feed by token_id)
                try:
                    detail = renaiss_api.get_pack_detail(slug)
                    tiers = {str(o.get("collectibleTokenId")): o.get("tier")
                             for o in (detail.get("recentOpenedPacks") or [])}
                    for r in act["recent"]:
                        r["tier"] = tiers.get(str(r["token_id"]))
                except Exception:
                    pass
                m["onchain"] = act
        machines.append(m)
    return jsonify({"machines": machines, "generated_ts": int(time.time())})


@app.route("/api/prize-pool")
def api_prize_pool():
    """Prize-pool contents overview (for limited/custody packs, e.g. World Cup): the pack_content catalog
    sorted by value, with tier / luck / source badges. Params: ?slug= or ?pack_name= ?limit=200"""
    slug = (request.args.get("slug") or "").strip()
    pack_name = (request.args.get("pack_name") or "").strip()
    limit = min(int(request.args.get("limit", 200)), 1000)
    conn = _core_db()
    if not conn:
        return jsonify({"error": "尚未建立 collectiq_core.db"}), 503
    # slug -> pack_name (matched against the Renaiss catalog name)
    if slug and not pack_name:
        try:
            for pk in renaiss_api.get_packs(include_inactive=True):
                if pk.get("slug") == slug:
                    pack_name = pk.get("name"); break
        except Exception:
            pass
    if not pack_name:
        conn.close()
        return jsonify({"error": "需提供 slug 或 pack_name"}), 400
    rows = conn.execute(
        """SELECT tier, name, cert, grader, grade, year, image_url,
                  renaiss_buyback_usd, market_price_usd, luck_value,
                  market_source, market_url
           FROM pack_content
           WHERE pack_name = ?
           ORDER BY CASE tier WHEN 'TOP' THEN 0 WHEN 'S' THEN 1 WHEN 'A' THEN 2
                              WHEN 'B' THEN 3 WHEN 'C' THEN 4 ELSE 5 END,
                    market_price_usd DESC NULLS LAST
           LIMIT ?""", (pack_name, limit)).fetchall()
    conn.close()
    # Tier summary
    summary = {}
    for r in rows:
        summary[r["tier"]] = summary.get(r["tier"], 0) + 1
    return jsonify({"pack_name": pack_name, "slug": slug,
                    "n": len(rows), "tier_summary": summary,
                    "cards": [dict(r) for r in rows]})


if __name__ == "__main__":
    # debug=False for deployment; port overridable via env (Docker maps 8502).
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    debug = os.getenv("DASHBOARD_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
