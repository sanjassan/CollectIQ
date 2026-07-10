#!/usr/bin/env python3
"""
Limited-pack tracker -- detects the opening / appearance of "weekly limited packs."

Every Friday (or irregularly), Renaiss opens an is_limited limited pack, teased on Twitter/Discord,
though it usually shows up on chain / via the API earlier. This script watches open-monitor /api/packs:

Detected events:
  - NEW_PACK     : a never-before-seen slug appears (a brand-new pack, limited or not).
  - LIMITED_OPEN : an is_limited pack goes from "sold out" to "open" (is_sold_out 1->0)
                   or its remaining count goes from 0 to positive -> the limited drop opened.
  - NEW_S_PULL   : a limited pack pulls a new S card (last_s_token_id changed).

State is stored in data/pack_state.json; events are appended to data/limited_events.json.
On OPEN/NEW, a notification is sent via the existing TelegramAlert (just printed if not configured).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
STATE = DATA / "pack_state.json"
EVENTS = DATA / "limited_events.json"
BASE = "https://open-monitor-rmrm.pages.dev"


def fetch_packs() -> list[dict]:
    r = requests.get(f"{BASE}/api/packs", timeout=30)
    r.raise_for_status()
    return r.json().get("packs", [])


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def _capture_content(names: set[str]) -> None:
    """When a new/opened limited pack is detected, immediately grab its full pack card list into pack_content.
    A limited pack's countdown / open window lasts only a few hours, so the 6h-scheduled packgrab often misses
    it entirely, leaving /api/new-pack unable to catch the countdown and forcing a fallback. This grabs a version on the spot.
    Matched by name (tRPC cardPack.getAll's slug field is None; only id/name are reliable)."""
    if not names:
        return
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import grab_pack_contents as gpc
        import ledger
        core = ledger.init_db()
        catalog = {p.get("name"): p for p in gpc.list_packs()}
        for nm in names:
            p = catalog.get(nm)
            if not p:
                print(f"⚠️ 即時補抓找不到卡機：{nm}")
                continue
            res = gpc.grab_pack(core, p)
            core.commit()
            print(f"📸 即時補抓 {nm}：{res.get('cards', 0)} 張 "
                  f"[{p.get('stage')}] {res.get('error', '')}")
    except Exception as e:
        print(f"⚠️ 即時補抓失敗：{e}")


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    packs = fetch_packs()
    if not packs:
        print("❌ /api/packs 無資料")
        return 1

    prev = _load_json(STATE, {})
    events = _load_json(EVENTS, [])
    now = int(time.time())
    new_events: list[dict] = []

    cur_state = {}
    for p in packs:
        slug = p.get("slug")
        if not slug:
            continue
        st = {
            "name": p.get("name"),
            "is_limited": bool(p.get("is_limited")),
            "is_sold_out": bool(p.get("is_sold_out")),
            "remaining": p.get("current_remaining"),
            "platform_ev_usd": p.get("current_platform_ev_usd"),
            "last_s_token_id": p.get("last_s_token_id"),
            "last_s_card_name": p.get("last_s_card_name"),
        }
        cur_state[slug] = st
        old = prev.get(slug)

        if old is None:
            # Brand-new pack (don't spam on the first run; only treat as NEW when state already exists)
            if prev:
                new_events.append({"ts": now, "type": "NEW_PACK", "slug": slug,
                                   "name": st["name"], "is_limited": st["is_limited"],
                                   "remaining": st["remaining"]})
            continue

        # Limited drop opened: sold out -> open, or remaining 0 -> positive
        opened = (old.get("is_sold_out") and not st["is_sold_out"]) or \
                 ((old.get("remaining") or 0) <= 0 < (st["remaining"] or 0))
        if st["is_limited"] and opened:
            new_events.append({"ts": now, "type": "LIMITED_OPEN", "slug": slug,
                               "name": st["name"], "remaining": st["remaining"],
                               "platform_ev_usd": st["platform_ev_usd"]})

        # New S card in a limited drop
        if st["is_limited"] and st["last_s_token_id"] and \
           st["last_s_token_id"] != old.get("last_s_token_id"):
            new_events.append({"ts": now, "type": "NEW_S_PULL", "slug": slug,
                               "name": st["name"], "card": st["last_s_card_name"],
                               "token_id": st["last_s_token_id"]})

    STATE.write_text(json.dumps(cur_state, indent=2, ensure_ascii=False))
    if new_events:
        events.extend(new_events)
        EVENTS.write_text(json.dumps(events[-500:], indent=2, ensure_ascii=False))

    # New/opened limited pack -> grab a full pack card list right away; don't let the 6h schedule miss the countdown window
    capture = {e["name"] for e in new_events
               if e["type"] == "LIMITED_OPEN"
               or (e["type"] == "NEW_PACK" and e.get("is_limited"))}
    _capture_content(capture)

    # Notify (only OPEN / NEW_PACK are pushed; NEW_S_PULL is only recorded)
    alertable = [e for e in new_events if e["type"] in ("LIMITED_OPEN", "NEW_PACK")]
    if alertable:
        lines = ["🎰 *限量卡機動態*"]
        for e in alertable:
            if e["type"] == "LIMITED_OPEN":
                lines.append(f"🟢 *{e['name']}* 開放中！剩 {e['remaining']} 張 · 官方EV ${e.get('platform_ev_usd')}")
            else:
                tag = "限量" if e.get("is_limited") else ""
                lines.append(f"🆕 新卡機 {tag}：*{e['name']}* (剩 {e.get('remaining')})")
        msg = "\n".join(lines)
        try:
            from main import TelegramAlert
            tg = TelegramAlert()
            if tg.is_configured():
                tg.send_alert(msg)
                print("📨 已發 Telegram 通知")
            else:
                print("ℹ️ Telegram 未設定，僅輸出：")
        except Exception as e:
            print(f"⚠️ 通知發送失敗：{e}")
        print(msg)

    ts = time.strftime("%F %T")
    lim = [s for s, v in cur_state.items() if v["is_limited"]]
    openlim = [s for s, v in cur_state.items() if v["is_limited"] and not v["is_sold_out"]]
    print(f"[{ts}] 限量追蹤：{len(cur_state)} 卡機 · 限量 {len(lim)} · 開放中限量 {len(openlim)} · "
          f"本輪事件 {len(new_events)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
