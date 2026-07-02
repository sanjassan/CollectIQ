#!/usr/bin/env python3
"""
從 Renaiss 公開 Marketplace tRPC API 同步卡牌（無需 Wallet 登入）。

資料來源與 renaiss-scanner 相同：
  https://www.renaiss.xyz/api/trpc/collectible.list

open-monitor (https://open-monitor-rmrm.pages.dev) 則額外做：
  - 卡池 EV / 鏈上 5s 偵測 / WebSocket（Cloudflare Workers + D1 + Scraper）
  - /api/packs、/api/recent-pulls 為「抽卡歷史 + 卡池狀態」

本腳本：Marketplace 在售卡（含 FMV、掛單價、圖片）
sync_open_monitor.py：抽卡 pulls + 卡池 metadata
兩者合併 = 完整監控資料面。
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

import requests

MARKETPLACE_API = "https://www.renaiss.xyz/api/trpc/collectible.list"
PAGE_LIMIT = 50
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def _to_usd_wei(value: str | None) -> float | None:
    if not value or value in ("NO-ASK-PRICE", "NO-OFFER-PRICE"):
        return None
    try:
        num = int(value)
        return num / 1e18 if num > 0 else None
    except (TypeError, ValueError):
        return None


def _to_usd_cents(value) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
        return num / 100 if num > 0 else None
    except (TypeError, ValueError):
        return None


def _normalize(item: dict) -> dict:
    attrs = {a.get("trait"): a.get("value") for a in item.get("attributes") or []}
    serial = attrs.get("Serial") or ""
    serial_num = None
    for part in serial.replace("#", " ").split():
        if part.isdigit():
            serial_num = int(part)
            break
    ask = _to_usd_wei(item.get("askPriceInUSDT"))
    fmv = _to_usd_cents(item.get("fmvPriceInUSD"))
    return {
        "token_id": str(item.get("tokenId") or ""),
        "name": item.get("name") or "",
        "set_name": item.get("setName") or "",
        "card_number": item.get("cardNumber") or "",
        "character_name": item.get("pokemonName") or "",
        "serial": serial,
        "serial_num": serial_num,
        "grader": attrs.get("Grader") or item.get("gradingCompany") or "",
        "grade": attrs.get("Grade") or item.get("grade") or "",
        "image_url": item.get("frontImageUrl") or "",
        "ask_price": ask,
        "fmv": fmv,
        "is_listed": bool(ask),
        "marketplace_url": f"https://www.renaiss.xyz/card/{item.get('tokenId')}" if item.get("tokenId") else "",
        "remaining_quantity": 1,
        "market_price": fmv or ask or 0.0,
        "sources": 1 if (fmv or ask) else 0,
    }


def fetch_page(offset: int, listed_only: bool = True) -> tuple[list[dict], dict]:
    payload = {
        "json": {
            "limit": PAGE_LIMIT,
            "offset": offset,
            "sortBy": "listDate",
            "sortOrder": "desc",
            "listedOnly": listed_only,
            "characterFilter": "",
            "languageFilter": "",
            "gradingCompanyFilter": "",
            "gradeFilter": "",
            "yearRange": "",
            "priceRangeFilter": "",
        }
    }
    url = f"{MARKETPLACE_API}?input={urllib.parse.quote(json.dumps(payload))}"
    r = requests.get(url, headers={"accept": "application/json", "user-agent": "renaiss-ev-monitor-v2/1.0"}, timeout=30)
    r.raise_for_status()
    body = r.json()
    page = body.get("result", {}).get("data", {}).get("json") or {}
    collection = page.get("collection") or []
    return [_normalize(c) for c in collection], page.get("pagination") or {}


def sync_all(listed_only: bool = True, max_pages: int = 200) -> list[dict]:
    cards: list[dict] = []
    offset = 0
    for _ in range(max_pages):
        batch, pagination = fetch_page(offset, listed_only=listed_only)
        if not batch:
            break
        cards.extend(batch)
        if not pagination.get("hasMore"):
            break
        offset += len(batch)
    return cards


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    listed = sync_all(listed_only=True)
    all_cards = sync_all(listed_only=False, max_pages=400)

    (DATA / "marketplace_listed.json").write_text(json.dumps(listed, indent=2, ensure_ascii=False))
    (DATA / "marketplace_all.json").write_text(json.dumps(all_cards, indent=2, ensure_ascii=False))
    # 覆寫 pool_data 供 main.py EV 計算（使用 marketplace 價格）
    (DATA / "pool_data.json").write_text(json.dumps(all_cards, indent=2, ensure_ascii=False))

    print(f"✅ Marketplace 同步：在售 {len(listed)} · 索引 {len(all_cards)}")
    print(f"   → {DATA / 'marketplace_all.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
