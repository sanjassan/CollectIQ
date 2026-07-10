#!/usr/bin/env python3
"""build_holdings.py — derive "where each card currently sits (its holder)" + card info from on-chain transfer events.

Data sources:
  - data/onchain_pulls.db   : every ERC721 Transfer (token_id → to_addr = current holder)
  - data/marketplace_all.json: card identity (name / set_name / image_url / fmv / grade / links)
  - data/pull_log.db        : supplementary identity (card_name / card_fmv_usd / card_tier)

Outputs:
  - data/holdings.json      : per-token "current holder + card info" for the dashboard /holdings view
  - Also backfills onchain_pulls.card_name / market_fmv (for the rows whose identity can be joined)

Each token's "current holder" = the to_addr of that token_id's row with the largest block_number in onchain_pulls.
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
    """Return (token_id→card, name→image_url). The latter backfills images when token_id doesn't match but the name does."""
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

    # Each token's latest transfer = current holder (largest block_number, ties broken by log_index)
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
        # Image: prefer the marketplace image matched by token_id; otherwise backfill by matching card name
        image = mk.get("image_url") or (name2img.get(name) if name else "") or ""
        identified = bool(name)

        # Backfill onchain_pulls identity columns (only those that can be joined)
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

    # Identified cards first, then by descending FMV
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
