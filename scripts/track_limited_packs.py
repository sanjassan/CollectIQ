#!/usr/bin/env python3
"""
限量卡機追蹤器 —— 偵測「每周限量卡機」的開放 / 新增。

每周五（或不定期）Renaiss 會開一個 is_limited 的限量卡機，會在 Twitter/Discord
預告，但鏈上 / API 通常會更早出現。本腳本盯著 open-monitor /api/packs：

偵測事件：
  - NEW_PACK     ：出現從沒見過的 slug（全新卡機，含限量）。
  - LIMITED_OPEN ：某個 is_limited 卡機從「售罄」變成「開放中」(is_sold_out 1→0)
                   或剩餘數從 0 變正 → 限量場開抽了。
  - NEW_S_PULL   ：限量卡機抽出新的 S 卡（last_s_token_id 改變）。

狀態存 data/pack_state.json；事件 append 到 data/limited_events.json。
偵測到 OPEN/NEW 時透過既有 TelegramAlert 發通知（沒設定就只印出）。
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
    """偵測到新/開放限量卡機時，立刻抓一次該卡機全池卡表寫入 pack_content。
    限量卡機的 countdown / 開放窗口只有數小時，等 6h 排程的 packgrab 常整台錯過，
    導致 /api/new-pack 永遠抓不到 countdown 而 fallback。這裡即時補抓一版。
    以 name 對應（tRPC cardPack.getAll 的 slug 欄位為 None，只有 id/name 可靠）。"""
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
            # 全新卡機（首跑時別狂報，只有 state 已存在時才當 NEW）
            if prev:
                new_events.append({"ts": now, "type": "NEW_PACK", "slug": slug,
                                   "name": st["name"], "is_limited": st["is_limited"],
                                   "remaining": st["remaining"]})
            continue

        # 限量場開抽：售罄→開放，或剩餘 0→正
        opened = (old.get("is_sold_out") and not st["is_sold_out"]) or \
                 ((old.get("remaining") or 0) <= 0 < (st["remaining"] or 0))
        if st["is_limited"] and opened:
            new_events.append({"ts": now, "type": "LIMITED_OPEN", "slug": slug,
                               "name": st["name"], "remaining": st["remaining"],
                               "platform_ev_usd": st["platform_ev_usd"]})

        # 限量場新 S 卡
        if st["is_limited"] and st["last_s_token_id"] and \
           st["last_s_token_id"] != old.get("last_s_token_id"):
            new_events.append({"ts": now, "type": "NEW_S_PULL", "slug": slug,
                               "name": st["name"], "card": st["last_s_card_name"],
                               "token_id": st["last_s_token_id"]})

    STATE.write_text(json.dumps(cur_state, indent=2, ensure_ascii=False))
    if new_events:
        events.extend(new_events)
        EVENTS.write_text(json.dumps(events[-500:], indent=2, ensure_ascii=False))

    # 新/開放限量卡機 → 立刻補抓一版全池卡表，別等 6h 排程錯過 countdown 窗口
    capture = {e["name"] for e in new_events
               if e["type"] == "LIMITED_OPEN"
               or (e["type"] == "NEW_PACK" and e.get("is_limited"))}
    _capture_content(capture)

    # 通知（OPEN / NEW_PACK 才推播；NEW_S_PULL 只記錄）
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
