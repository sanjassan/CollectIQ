#!/usr/bin/env python3
"""
Web Dashboard for Renaiss EV Monitor v2
整合 EV 計算、Pack 監控、外部比價功能
"""

import json
import os
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
    try:
        cur = conn.execute("SELECT v FROM state WHERE k='last_block'").fetchone()
        last_block = int(cur[0]) if cur else None
    except Exception:
        last_block = None
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
        "last_block_time": last_block_time,
        "last_block": last_block,
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


@app.route("/api/new-pack")
def api_new_pack():
    """最新限量卡機（如 Bowtie）的完整 scrape：分級組成、官方/自算EV、開抽時間、
    以及卡池上鏈後的鏈上錢包分析（由 watch_new_pool.py 產生）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "new_pack_bowtie.json")
    if not os.path.exists(path):
        return jsonify({"note": "尚無新限量卡機資料"})
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


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


if __name__ == "__main__":
    # debug=False for deployment; port overridable via env (Docker maps 8502).
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    debug = os.getenv("DASHBOARD_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
