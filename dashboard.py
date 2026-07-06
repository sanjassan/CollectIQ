#!/usr/bin/env python3
"""
Web Dashboard for Renaiss EV Monitor v2
整合 EV 計算、Pack 監控、外部比價功能
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
    """鏈上持有頁：每張卡的圖片 + 資訊 + 當前存放位置（持有者）"""
    return render_template("holdings.html")


@app.route("/api/holdings")
def api_holdings():
    """回傳 data/holdings.json（每個 token 的當前持有者 + 卡片資訊）。
    若檔案不存在則即時建立。可加 ?identified=1 只回傳已識別卡片。"""
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
    """鏈上抽卡紀錄（onchain_pulls.db）：每筆 = 哪台卡機(pool)、哪張卡、FMV、何時被抽走、抽走的錢包。
    參數：?min_fmv=（只看高於此 FMV，預設 0）、?limit=（預設 200）、?pool=slug（omega/eden/...）。
    回傳含 sync 進度（最後同步區塊時間、落後鏈頭多少），讓前端標示是否即時。"""
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
    synced_ts   = _state("synced_ts")     # 本輪 wall-clock：多久前確認追到 head
    synced_block = _state("synced_block") or last_block
    # 真實同步落後 = head 與已同步區塊差；synced_ts 老代表爬蟲本身停了。
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
        "last_block_time": last_block_time,   # 最近一次「抽卡」的時間（非同步延遲！）
        "last_block": last_block,
        "head_block": head_block,             # 爬蟲上輪看到的鏈頭
        "synced_block": synced_block,
        "synced_ts": synced_ts,               # 爬蟲上輪 wall-clock（判斷爬蟲是否還活著）
        "sync_behind_blocks": sync_behind_blocks,  # 真實同步落後區塊數（≈0 = 即時）
        "min_fmv": min_fmv,
    })


@app.route("/api/marketplace")
def api_marketplace():
    """市場上目前掛單的卡（data/marketplace_listed.json）。
    每張含 名稱 / 圖片 / 掛單價 ask_price / FMV / 連到 Renaiss 商店頁的連結。
    可加 ?limit=N（預設 500）、?sort=ask|fmv|discount（折價 = fmv/ask 由高到低）。"""
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
    """每個卡機的 官方EV / 經驗EV / 我們自算EV（外部校正後）+ 限量旗標。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pack_data.json")
    return jsonify(pack_ev.load_and_analyze(path))


def _limited_events():
    """track_limited_packs.py 產生的限量卡機偵測事件（含 ts / slug / remaining）。"""
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
    """依 pack_name 從 pack_content 取『最新一版』級距組成。
    以 updated_at（最後刷新）挑最新版本，而非 captured_at（首見時間）。
    回傳 (pack_id, total_cards, tiers, our_ev, featured)。找不到回傳 (None, 0, {}, None, None)。"""
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
    # 自算EV：整池 buyback 的加權平均（每抽一次的期望官方買回值）
    our_ev = conn.execute(
        "SELECT ROUND(AVG(renaiss_buyback_usd),2) FROM pack_content WHERE pack_id=?",
        (pid,)).fetchone()[0]
    feat = conn.execute(
        "SELECT MAX(renaiss_buyback_usd) FROM pack_content WHERE pack_id=?", (pid,)).fetchone()[0]
    return pid, total, tiers, our_ev, feat


@app.route("/api/new-pack")
def api_new_pack():
    """本週限量卡機。取用順序：
      ① live API 中開放/倒數中的 limited 卡機（is_open=true）；
      ② limited_events.json 最近偵測到的 limited 卡機（可能已結束，is_open=false）；
      ③ 空狀態（empty=true）—— 不再退回寫死的 06/26 靜態檔。
    級距一律以 pack_content 依 updated_at 取最新版本補上。"""
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

    # ① 開放/倒數中的限量卡機（理想狀態）
    live_open = [p for p in packs
                 if p.get("packType") == "limited"
                 and p.get("stage") in ("countdown", "active")]
    if live_open:
        chosen = live_open[0]
        is_open = True

    # ② 最近偵測到的限量卡機（即使已 archived，也比凍在 06/26 誠實）
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

    # ③ 空狀態：目前真的沒有開放中/近期限量卡機
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
    """各資料來源的新鮮度徽章來源：資料多舊、爬蟲是否還活著。
    每個來源回傳 ts(ISO) / age_sec / stale；stale=true 代表超過該來源容忍門檻。
    這是 06/26 卡住那類問題的通用防呆——頁面過期就該標紅，而非默默顯示舊資料。"""
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

    # 1) pack_content 目錄抓取時間（core.db meta）—— 本週限量卡機/卡機總覽的鮮度
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
    _emit("pack_catalog", grabbed_epoch, 8 * 3600)   # 6h 排程 → 8h 容忍

    # 2) 鏈上同步（onchain_pulls.db state.synced_ts / sync_behind_blocks）
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
    _emit("onchain_sync", synced_ts, 900, {"sync_behind_blocks": sync_behind})  # 300s → 15m 容忍

    # 3) 限量卡機事件（最後一次偵測到新/開放限量卡機）
    last_ev = None
    for e in _limited_events():
        ts = e.get("ts")
        if ts:
            last_ev = max(last_ev or 0, ts)
    _emit("limited_events", last_ev, 7 * 24 * 3600)  # 週更 → 一週容忍

    return jsonify({
        "now": datetime.utcnow().isoformat() + "Z",
        "sources": sources,
        "any_stale": any(s.get("stale") for s in sources.values()),
    })


@app.route("/healthz")
def healthz():
    """輕量存活探針，給外部 watchdog（scripts/healthcheck.py）用。
    只確認 Flask 活著 + core.db 可開；不打外部 API，維持毫秒級回應。"""
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
    """限量卡機歷史時間軸（誰在何時開放 / 新增 / 抽出 S 卡）。"""
    return render_template("limited_history.html")


@app.route("/api/limited-history")
def api_limited_history():
    """limited_events.json 的時間軸視圖：新到舊排序，附 ISO 時間與人類可讀類型。
    這是 06/26 那類「本週限量卡機卡住」問題的稽核面板——把每次偵測到的限量事件
    攤開，就能一眼看出上週到底開了哪些場、有沒有漏抓。"""
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
    """鏈上卡機合約地址（已知 + 疑似新卡機候選）。discover_pools.py 產生。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pool_addresses.json")
    if not os.path.exists(path):
        return jsonify({"known": [], "candidates": [], "note": "尚未產生，請跑 scripts/discover_pools.py"})
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/limited-events")
def api_limited_events():
    """限量卡機事件流（開放/新增/新S卡）。track_limited_packs.py 產生。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "limited_events.json")
    if not os.path.exists(path):
        return jsonify([])
    with open(path, encoding="utf-8") as f:
        events = json.load(f)
    return jsonify(events[-100:][::-1])  # 最新在前


# ═══════════════════════════════════════════════════════════════════════════════
#  LIVE POOL TRACKER — 限量卡機開放窗口即時獎池追蹤（data/live_pool.db）
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
    """挑最新更新的池（開放中/最近追蹤的）。"""
    return conn.execute(
        "SELECT * FROM pool_meta ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()


@app.route("/live")
def live_page():
    """限量卡機開放窗口即時獎池追蹤面板（暫時性高資訊量 → 獨立頁）。"""
    return render_template("live.html")


@app.route("/api/live/pool")
def api_live_pool():
    """目標池狀態彙整 + 倒數。可 ?address= 指定池。"""
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
    # 大獎統計
    pool = d["address"]
    d["big_in_pool"] = conn.execute(
        "SELECT COUNT(*) FROM tokens WHERE pool=? AND is_big=1 AND status='in_pool'", (pool,)).fetchone()[0]
    d["big_pulled"] = conn.execute(
        "SELECT COUNT(*) FROM tokens WHERE pool=? AND is_big=1 AND status='pulled'", (pool,)).fetchone()[0]
    # 池清單（供切換）
    pools = [dict(r) for r in conn.execute(
        "SELECT address, name, slug, loaded, pulled, remaining, updated_at FROM pool_meta ORDER BY updated_at DESC").fetchall()]
    conn.close()
    # 倒數（activeFrom）
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
    """最近抽卡事件流：哪張卡、哪個錢包抽走、時間、FMV。?address= ?limit=60 ?kind=pull|recycle|burn|all"""
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
    """抽最多的錢包（鯨魚）。?address= ?limit=20"""
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
    """大獎（FMV≥$300）：已被抽走 vs 仍在池內。?address="""
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
    """即時獎池 EV 反推 + 每張卡 FMV 對照（Renaiss FMV vs 真實市場 index_price）。

    回傳：
      remaining_ev_market / remaining_ev_renaiss  剩餘池每抽期望值（市場價 / Renaiss 價）
      pulled_ev_*                                 已抽出的平均值
      ev_ratio_live = remaining_ev_market / price 現在買還值不值（>1 值回票價）
      easter_eggs   幸運卡價（Renaiss 標低、市場高 ≥1.5x）→ 仍在池，可期待
      data_errors   疑似輸入錯誤（Renaiss ≫ 市場 3x 且信心低）→ 別信 Renaiss FMV
      recycled_gems 被回收回池的高市值卡（避免回收錯誤 / 撿漏）
      coverage      已有真實市場價的比例
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

    # 卡機價格（值不值回票價要用）
    price = None
    try:
        for p in renaiss_api.get_packs(include_inactive=True):
            if p.get("slug") == meta.get("slug"):
                price = p.get("price_usd")
                break
    except Exception:
        pass

    # join collectiq.db 取真實市場價 index_price_usd + 圖片
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
#  CORE LEDGER — 統一核心帳本（data/collectiq_core.db）
#  事件溯源後的成果：真持有者 / 幸運卡 / 獎勵進度 / 單卡完整轉移史
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
    """幸運卡（復活蛋）：真實市場價遠高於 Renaiss FMV，並附「現在在誰手中」。
    參數：?min_luck=1.5 ?limit=50"""
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
    """持有者排行 + 獎勵進度（伊布全款完成度）。?limit=30"""
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
    """獎勵狀態彙整：伊布全款 / 連號 / SBT。"""
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
    """單卡的完整鏈上轉移史（誰抽走、如何轉手、現在在誰手中）。"""
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
    """未開／已開抽卡機總覽（pack_content 快照）。
    每台卡機：slot 總數、distinct 卡數、buyback 區間、最高價卡縮圖、有無市價。
    ?stage=countdown 只看即將開的。"""
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
    """單台卡機內所有卡（distinct 物理卡，附 slot 權重 / buyback / 市價 / luck）。
    ?min_luck= 只看藏寶卡。"""
    conn = _core_db()
    if not conn:
        return jsonify({"available": False})
    min_luck = request.args.get("min_luck")
    # luck 必須與所顯示的 buyback 同源：同一 item_id 可跨多 tier/slot，
    # 各 slot 官方買回價不同。取 MAX(buyback)（最保守，luck 最低）並用同一值
    # 現算 luck，避免「MAX(buyback) 配 MAX(luck)」湊出不自洽的假高值。
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
    """單台卡機的級距組成（TOP/S/A/B/C/D）：每級 slot 數、佔比、buyback 區間、代表卡。
    給「這台卡機每個級距裝了什麼」用。pack_id 可傳 'countdown' 取當前倒數中卡機。"""
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
    """跨卡機藏寶卡榜：pack_content 內 luck_value 最高者（含尚未開的卡機）。
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
    """CollectIQ Price Intelligence — FMV 落差分析 / 鯨魚錢包 / True EV。"""
    return render_template("intelligence.html")


@app.route("/api/intelligence/fmv-gap")
def api_fmv_gap():
    """
    CollectIQ 核心：Renaiss FMV vs 真實市場價落差表。
    參數：?limit=500 ?min_gap=0 ?max_gap= ?confidence=high|medium|low
    同時回傳 stats（彙整統計）。
    """
    import sqlite3 as _sq
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "collectiq.db")

    # Fallback：DB 還沒建好就讀 fmv_gap.json
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
    """CollectIQ 鯨魚錢包彙整（持有 Renaiss tokens 的錢包統計）。"""
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
    """單張 token 的完整 CollectIQ 分析（FMV 比對 + cert 圖片 + 持有者）。"""
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
    """觸發背景重建 collectiq.db（只補查尚未查過的 cert）。"""
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
    """CollectIQ 背景 build 進度（DB 統計 + log tail + 是否正在跑）。"""
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
    """價格驗證頁：Renaiss 內部價(FMV/掛單) vs 我們自抓的外部公開市場價。"""
    return render_template("compare.html")


@app.route("/api/trust-score")
def api_trust_score():
    """CollectIQ 平台信任分數 — 一個 API 回傳所有 dashboard 需要的關鍵數字。"""
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
    """回傳 data/comparison.json（build_comparison.py 產生）。

    支援 ?flag=renaiss_high 篩選；?min_delta=20 只看絕對偏差 >=20% 的。
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
    """即時查單卡的「我們自己抓」的外部價（不碰 Renaiss 來源）。

    ?token_id=... 會先在 marketplace_all.json 找到該卡再比價；
    或直接帶 ?name=&set=&char=&number=&grader=&grade= 自訂查詢。
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
    """Fetch pack data — 優先用 Renaiss CLI 即時資料，fallback 到本地 JSON。"""
    try:
        # 嘗試 CLI 即時資料
        live_packs = renaiss_api.get_all_packs_ev()
        if live_packs:
            return jsonify({"results": live_packs, "source": "renaiss-cli-live"})
    except Exception as e:
        print(f"[WARN] CLI pack data failed, falling back: {e}")

    # Fallback：本地 JSON → MOCK
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
    """Renaiss 卡機資料。
    ?all=1 回傳全部 (含 archived 限量 + coming_soon)；預設只回傳 active。
    """
    try:
        show_all = request.args.get("all", "0") == "1"
        all_packs = renaiss_api.get_packs(include_inactive=True)
        results = []
        for p in all_packs:
            slug = p.get("slug", "")
            stage = p.get("stage", "")
            # coming_soon / archived → 只回傳基本資料，不呼叫 compute_pack_ev（會 404）
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
                # active perpetual → 完整 EV 計算
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
    """單一卡機完整即時資料（含最近 20+ 筆開包 fmv/tier 列表）。"""
    try:
        detail = renaiss_api.get_pack_detail(slug)
        ev = renaiss_api.compute_pack_ev(slug)
        return jsonify({"detail": detail, "ev": ev})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/market/search")
def api_market_search():
    """Index API 搜尋卡片 — ?q=卡名&limit=10，回傳含 price_usd / grade / spark。"""
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
    """Index API 查分級序號（PSA/BGS/CGC cert）→ 卡名 + 即時 FMV。"""
    data = renaiss_api.get_graded(cert)
    return jsonify(data)


@app.route("/api/market/price-by-id/<rid>")
def api_market_price_by_rid(rid):
    """Index API 用 Renaiss tokenId 查卡片即時 FMV。"""
    data = renaiss_api.get_card_by_renaiss_id(rid)
    return jsonify(data)


@app.route("/api/market/fmv-series/<rid>")
def api_market_fmv_series(rid):
    """Index API 查某張卡的每日 FMV 歷史（用於趨勢圖）。"""
    series = renaiss_api.get_card_fmv_series(rid)
    return jsonify({"rid": rid, "series": series})


@app.route("/api/trades/live")
def api_trades_live():
    """Index API 最近跨平台成交紀錄（snkrdunk 等）。?limit=50"""
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except Exception:
        limit = 50
    trades = renaiss_api.get_recent_trades(limit=limit)
    return jsonify({"trades": trades, "count": len(trades)})


@app.route("/api/index/<game>")
def api_index_overview(game):
    """Index API 指數總覽（game = pokemon / one-piece）。"""
    if game not in ("pokemon", "one-piece"):
        return jsonify({"error": "game must be pokemon or one-piece"}), 400
    data = renaiss_api.get_index_overview(game)
    return jsonify(data)


@app.route("/api/cache/stats")
def api_cache_stats():
    """診斷：API 快取狀態（每個 key 的年齡、是否過期）。"""
    return jsonify(renaiss_api.cache_stats())


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    """強制清除 API 快取（讓下次請求重新抓 CLI/Index API）。"""
    renaiss_api.clear_cache()
    return jsonify({"status": "cleared"})


@app.route("/oracle")
def oracle_page():
    """Staking Oracle 概念頁：CollectIQ 作為質押定價預言機。"""
    return render_template("oracle.html")


@app.route("/api/oracle/simulate")
def api_oracle_simulate():
    """模擬 Oracle 對每張卡的質押參數：verified_price, confidence, LTV, liquidation。"""
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
    """API 驗證頁：逐一打每個端點，確認可用。"""
    return render_template("api_status.html")


@app.route("/price-search")
def price_search_page():
    """PriceCharting 即時比價搜尋頁。"""
    return render_template("price_search.html")


def _pricecharting_list(query: str):
    """回傳 PriceCharting 搜尋結果清單（最多 20 個），每項含 title + url。"""
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
    """抓 PriceCharting 特定商品頁的各等級價格。"""
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
    """搜尋 PriceCharting。
    必填：?q=關鍵字
    選填：?grader=PSA|BGS|CGC  ?grade=10|9.5|...
    回傳：results（候選清單）或直接回傳 prices（唯一匹配）。
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
    """抓特定 PriceCharting 商品頁的各等級價格。
    必填：?url=https://www.pricecharting.com/game/...
    選填：?grader=PSA  ?grade=10
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
    """CDP 模擬器頁：互動式選卡 → 即時看到借款額度。"""
    return render_template("cdp.html")


@app.route("/api/cdp/cards")
def api_cdp_cards():
    """回傳所有可質押卡片清單（有獨立驗證價的），供 CDP 模擬器選卡。"""
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
    """RWA 指數頁：按系列 / IP 的價格指數。"""
    return render_template("rwa_index.html")


@app.route("/api/rwa-index")
def api_rwa_index():
    """產出 RWA 指數：按 set_name 分組的價格統計。"""
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
#  單卡查價站 + 錢包追蹤（對外 renaiss_lookup.html 專用）
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/card-lookup")
def api_card_lookup():
    """單張卡「一次查三來源」：貼卡名 → 併排回傳
      1) independent：PriceCharting（彙整 eBay 成交，有可查證連結）＝有公信力
      2) renaiss_index：Renaiss 自家指數 FMV（Index API，非獨立第三方）
      3) renaiss_buyback：Renaiss 平台收購價（來自 pack_content，逐 entry）＋ luck
    參數：?q=卡名（必填） ?grader=PSA ?grade=10
    誠信規則同 CLAUDE.md：independent 與 renaiss_index 不可混用，各自標來源。"""
    from our_price import _grade_label
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "q 參數必填（卡片名稱）"}), 400
    grader = request.args.get("grader", "").strip()
    grade = request.args.get("grade", "").strip()

    out = {"query": q, "grader": grader, "grade": grade,
           "independent": None, "renaiss_index": None, "renaiss_buyback": None}

    # ── 1) 獨立來源：PriceCharting（重用既有 helper）──
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

    # ── 2) Renaiss 自家指數 FMV（Index API，非獨立）──
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

    # ── 3) Renaiss 收購價 + luck（pack_content，逐 entry）──
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
    """錢包追蹤：給一個地址，回傳它在 Renaiss 生態的動作
      · pulls   ：從卡機抽出的卡（is_mint=1, to=addr）＋ FMV
      · received：別的錢包轉進來的卡（is_mint=0, to=addr）
      · sent    ：轉出去的卡（from=addr）
      · listings：在市場上架/掛單紀錄（ledger_market，含 list 價）
    參數：?limit=100"""
    import sqlite3 as _sq
    addr = (addr or "").strip().lower()
    if not addr.startswith("0x") or len(addr) != 42:
        return jsonify({"error": "地址格式不正確（需 0x + 40 hex）"}), 400
    limit = min(int(request.args.get("limit", 100)), 500)
    base = os.path.dirname(os.path.abspath(__file__))

    out = {"address": addr, "pulls": [], "received": [], "sent": [],
           "listings": [], "summary": {}}

    # onchain_pulls：pulls / received / sent
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
        # 摘要統計（全量，不受 limit 影響）
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

    # ledger_market：這個地址的上架/掛單（賣卡意圖）
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


# slug → 抽卡池合約（is_mint=1 時 from_addr = 這些池）。與 track_pulls_onchain.py 的
# GACHA_CONTRACTS 對齊；limited/custody 卡機（World Cup）走金庫託管，鏈上沒有公開池 feed。
_POOL_BY_SLUG = {
    "omega":          "0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910",
    "eden-pack":      "0xfda4a907d23d9f24271bc47483c5b983831e325e",
    "renacrypt-pack": "0xb2891022648c5fad3721c42c05d8d283d4d53080",
}


# 共用 ERC721 NFT 合約（所有卡機的卡都鑄在這）
_NFT_CONTRACT = "0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30"
_INV_CACHE = {}  # pool -> (ts, balance)


def _rpc_nodes():
    """優先用 BNB_RPC（帶 key，逗號/空白分隔）；沒有就退公用 dataseed。"""
    env = os.getenv("BNB_RPC", "").replace(",", " ").split()
    return [r for r in env if r] or [
        "https://bsc-dataseed.binance.org",
        "https://bsc-dataseed1.defibit.io",
        "https://bsc-dataseed1.ninicoin.io",
    ]


def _pool_inventory(pool_addr, ttl=60):
    """該卡機池目前在鏈上還握著幾張 NFT（= 現在池裡剩幾張）。快取 60s。"""
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
    """從 onchain_pulls 算單一抽卡池的：總抽數、最近 feed、高價門檻(p99)、
    高價抽頻率、最長乾旱間隔排行。全部是真實鏈上資料。"""
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
    # 高價門檻：該池 fmv 的 99 百分位（動態、真實）
    fmvs = [r[0] for r in oc.execute(
        "SELECT market_fmv FROM onchain_pulls WHERE LOWER(from_addr)=? AND is_mint=1 "
        "AND market_fmv IS NOT NULL ORDER BY market_fmv", (p,)).fetchall()]
    hi_thr = fmvs[int(len(fmvs) * 0.99)] if fmvs else 0
    hi_n = sum(1 for v in fmvs if v >= hi_thr) if hi_thr else 0
    # 全歷史（時間序）供間隔排行；只取必要欄位
    seq = oc.execute(
        "SELECT block_time, market_fmv, card_name, token_id, to_addr, tx_hash "
        "FROM onchain_pulls WHERE LOWER(from_addr)=? AND is_mint=1 "
        "ORDER BY block_number ASC, log_index ASC", (p,)).fetchall()
    # 最長間隔排行：兩次高價抽之間隔了幾抽
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
    """所有卡機的即時動態：總抽數 / 最近抽卡 feed（含錢包）/ 高價抽頻率 /
    最長間隔排行。perpetual 池走鏈上真實資料；limited/custody（World Cup）走金庫託管，
    鏈上沒有公開抽卡 feed，改由 /api/prize-pool 顯示獎池內容。"""
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
                # 用 Renaiss recentOpenedPacks 補上官方 tier（依 token_id 對應到最近 feed）
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
    """獎池內容一覽（給 limited/custody 卡機，如 World Cup）：pack_content 目錄
    依價值排序，附 tier / luck / 來源徽章。參數：?slug= 或 ?pack_name= ?limit=200"""
    slug = (request.args.get("slug") or "").strip()
    pack_name = (request.args.get("pack_name") or "").strip()
    limit = min(int(request.args.get("limit", 200)), 1000)
    conn = _core_db()
    if not conn:
        return jsonify({"error": "尚未建立 collectiq_core.db"}), 503
    # slug → pack_name（用 Renaiss 目錄名比對）
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
    # tier 摘要
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
