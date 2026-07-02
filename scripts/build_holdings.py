#!/usr/bin/env python3
"""build_holdings.py — 由鏈上轉移事件推導「每張卡目前的存放位置（持有者）」+ 卡片資訊。

資料來源：
  - data/onchain_pulls.db   : 每筆 ERC721 Transfer（token_id → to_addr = 當前持有者）
  - data/marketplace_all.json: 卡片身分（name / set_name / image_url / fmv / grade / 連結）
  - data/pull_log.db        : 補充身分（card_name / card_fmv_usd / card_tier）

產出：
  - data/holdings.json      : 每個 token 的「當前持有者 + 卡片資訊」，給 dashboard /holdings 用
  - 同時回填 onchain_pulls.card_name / market_fmv（能 join 到身分的部分）

每個 token 的「當前持有者」= 該 token_id 在 onchain_pulls 中 block_number 最大那筆的 to_addr。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ONCHAIN_DB = ROOT / "data" / "onchain_pulls.db"
PULL_DB = ROOT / "data" / "pull_log.db"
MARKET_JSON = ROOT / "data" / "marketplace_all.json"
OUT_JSON = ROOT / "data" / "holdings.json"

BSCSCAN_TOKEN = "https://bscscan.com/token/0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30?a="
BSCSCAN_ADDR = "https://bscscan.com/address/"


def _load_market() -> tuple[dict[str, dict], dict[str, str]]:
    """回傳 (token_id→卡片, 卡名→image_url)。後者用於 token_id 對不上、但卡名相同時補圖。"""
    if not MARKET_JSON.exists():
        return {}, {}
    by_tid: dict[str, dict] = {}
    name2img: dict[str, str] = {}
    for c in json.loads(MARKET_JSON.read_text(encoding="utf-8")):
        tid = str(c.get("token_id") or "")
        if tid:
            by_tid[tid] = c
        nm = (c.get("name") or "").strip()
        if nm and c.get("image_url") and nm not in name2img:
            name2img[nm] = c["image_url"]
    return by_tid, name2img


def _load_pulllog() -> dict[str, dict]:
    if not PULL_DB.exists():
        return {}
    out: dict[str, dict] = {}
    c = sqlite3.connect(PULL_DB)
    try:
        for tid, name, fmv, tier in c.execute(
            "SELECT token_id, card_name, card_fmv_usd, card_tier FROM pull_log "
            "WHERE token_id IS NOT NULL AND token_id!='' AND card_name IS NOT NULL"
        ):
            if tid and str(tid) not in out:
                out[str(tid)] = {"name": name, "fmv": fmv, "tier": tier}
    finally:
        c.close()
    return out


def build() -> dict:
    if not ONCHAIN_DB.exists():
        raise SystemExit(f"找不到 {ONCHAIN_DB}")
    market, name2img = _load_market()
    pulllog = _load_pulllog()

    conn = sqlite3.connect(ONCHAIN_DB)
    conn.row_factory = sqlite3.Row

    # 每個 token 的最新一筆轉移 = 當前持有者（block_number 最大，平手用 log_index）
    rows = conn.execute(
        """
        SELECT t.token_id, t.to_addr, t.block_number, t.block_time, t.is_mint
        FROM onchain_pulls t
        JOIN (
            SELECT token_id, MAX(block_number * 100000 + log_index) AS rk
            FROM onchain_pulls GROUP BY token_id
        ) m ON t.token_id = m.token_id
           AND (t.block_number * 100000 + t.log_index) = m.rk
        """
    ).fetchall()

    holdings = []
    backfilled = 0
    for r in rows:
        tid = r["token_id"]
        mk = market.get(tid) or {}
        pl = pulllog.get(tid) or {}
        name = mk.get("name") or pl.get("name")
        fmv = mk.get("fmv") if mk.get("fmv") is not None else pl.get("fmv")
        # 圖片：優先 token_id 對到的 marketplace 圖；否則用卡名比對補圖
        image = mk.get("image_url") or (name2img.get(name) if name else "") or ""
        identified = bool(name)

        # 回填 onchain_pulls 的身分欄位（只回填能 join 到的）
        if name:
            cur = conn.execute(
                "UPDATE onchain_pulls SET card_name=?, market_fmv=COALESCE(market_fmv,?), "
                "set_name=COALESCE(set_name,?) WHERE token_id=? AND (card_name IS NULL OR card_name='')",
                (name, fmv, mk.get("set_name"), tid),
            )
            backfilled += cur.rowcount

        holder = r["to_addr"]
        holdings.append({
            "token_id": tid,
            "holder": holder,
            "holder_short": (holder[:8] + "…" + holder[-4:]) if holder else "-",
            "card_name": name or f"未知卡片 #{tid[:10]}…",
            "identified": identified,
            "set_name": mk.get("set_name") or "",
            "grade": (f"{mk.get('grader','')} {mk.get('grade','')}").strip(),
            "fmv": fmv,
            "image_url": image or "",
            "tier": pl.get("tier") or "",
            "last_block": r["block_number"],
            "last_time": datetime.fromtimestamp(r["block_time"], timezone.utc).isoformat()
                         if r["block_time"] else None,
            "from_mint": bool(r["is_mint"]),
            "bscscan_token": BSCSCAN_TOKEN + tid,
            "bscscan_holder": (BSCSCAN_ADDR + holder) if holder else "",
            "marketplace_url": mk.get("marketplace_url") or "",
        })
    conn.commit()
    conn.close()

    # 已識別、FMV 高者排前
    holdings.sort(key=lambda h: (h["identified"], h["fmv"] or 0), reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_tokens": len(holdings),
        "identified": sum(1 for h in holdings if h["identified"]),
        "with_image": sum(1 for h in holdings if h["image_url"]),
        "holdings": holdings,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[build_holdings] tokens={payload['total_tokens']} "
          f"identified={payload['identified']} with_image={payload['with_image']} "
          f"backfilled_rows={backfilled} → {OUT_JSON}")
    return payload


if __name__ == "__main__":
    build()
