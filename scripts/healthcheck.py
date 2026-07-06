#!/usr/bin/env python3
"""
Dashboard watchdog —— 定時打 /healthz，掛了就發 Telegram。

dashboard.py 若因當機 / plist 損毀而停擺（就像 06/26 那次 plist 只剩 3 個
散 key 導致整站 500），沒人會知道。這支腳本由 launchd（ai.renaiss.healthcheck，
每 300s）獨立於 dashboard 之外運行，主動探測，掛了立即通知。

去抖：狀態存 data/health_state.json，只在「上→下」與「下→上」的邊緣發通知，
不會每輪都轟炸。連續失敗達 FAIL_THRESHOLD 次才判定為 DOWN，避免單次抖動誤報。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

DATA = ROOT / "data"
STATE = DATA / "health_state.json"

PORT = os.getenv("DASHBOARD_PORT", "8502")
URL = os.getenv("HEALTHCHECK_URL", f"http://127.0.0.1:{PORT}/healthz")
TIMEOUT = 10
FAIL_THRESHOLD = 2   # 連續 N 次失敗才判 DOWN（濾掉單次抖動）


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"down": False, "consecutive_fails": 0}


def _probe() -> tuple[bool, str]:
    """回 (healthy, detail)。"""
    try:
        r = requests.get(URL, timeout=TIMEOUT)
        if r.status_code == 200:
            return True, "200 ok"
        # 200 以外（含 /healthz 自報 503 degraded）都算不健康
        detail = f"HTTP {r.status_code}"
        try:
            detail += f" {json.dumps(r.json(), ensure_ascii=False)}"
        except Exception:
            pass
        return False, detail
    except requests.RequestException as e:
        return False, f"unreachable: {e}"


def _notify(msg: str) -> None:
    print(msg)
    try:
        from main import TelegramAlert
        tg = TelegramAlert()
        if tg.is_configured():
            tg.send_alert(msg)
            print("📨 已發 Telegram 通知")
        else:
            print("ℹ️ Telegram 未設定，僅輸出")
    except Exception as e:
        print(f"⚠️ 通知發送失敗：{e}")


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    healthy, detail = _probe()
    ts = time.strftime("%F %T")

    if healthy:
        if state.get("down"):
            _notify(f"✅ *Renaiss Dashboard 已恢復*\n{URL}\n{ts}")
        state = {"down": False, "consecutive_fails": 0, "last_ok": ts}
    else:
        fails = state.get("consecutive_fails", 0) + 1
        state["consecutive_fails"] = fails
        state["last_detail"] = detail
        if fails >= FAIL_THRESHOLD and not state.get("down"):
            state["down"] = True
            _notify(f"🔴 *Renaiss Dashboard 無回應*\n{URL}\n連續失敗 {fails} 次 · {detail}\n{ts}")

    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    status = "OK" if healthy else f"FAIL({state.get('consecutive_fails')})"
    print(f"[{ts}] healthcheck {status} · {detail}")
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
