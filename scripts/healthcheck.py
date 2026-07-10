#!/usr/bin/env python3
"""
Dashboard watchdog — periodically hits /healthz and sends a Telegram alert if it's down.

If dashboard.py stalls due to a crash or a corrupt plist (like the 06/26 incident where the plist
had only 3 stray keys left and the whole site returned 500), no one would know. This script runs
under launchd (ai.renaiss.healthcheck, every 300s) independently of the dashboard, actively probing
and alerting the moment it goes down.

Debounce: state is stored in data/health_state.json, and alerts fire only on the up→down and down→up
edges, not every round. DOWN is declared only after FAIL_THRESHOLD consecutive failures, to avoid
false alarms from a single blip.
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
FAIL_THRESHOLD = 2   # declare DOWN only after N consecutive failures (filters out single blips)


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"down": False, "consecutive_fails": 0}


def _probe() -> tuple[bool, str]:
    """Return (healthy, detail)."""
    try:
        r = requests.get(URL, timeout=TIMEOUT)
        if r.status_code == 200:
            return True, "200 ok"
        # Anything other than 200 (including /healthz self-reporting 503 degraded) counts as unhealthy
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
            print("📨 Telegram notification sent")
        else:
            print("ℹ️ Telegram not configured, output only")
    except Exception as e:
        print(f"⚠️ Notification send failed: {e}")


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    healthy, detail = _probe()
    ts = time.strftime("%F %T")

    if healthy:
        if state.get("down"):
            _notify(f"✅ *Renaiss Dashboard recovered*\n{URL}\n{ts}")
        state = {"down": False, "consecutive_fails": 0, "last_ok": ts}
    else:
        fails = state.get("consecutive_fails", 0) + 1
        state["consecutive_fails"] = fails
        state["last_detail"] = detail
        if fails >= FAIL_THRESHOLD and not state.get("down"):
            state["down"] = True
            _notify(f"🔴 *Renaiss Dashboard unresponsive*\n{URL}\n{fails} consecutive failures · {detail}\n{ts}")

    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    status = "OK" if healthy else f"FAIL({state.get('consecutive_fails')})"
    print(f"[{ts}] healthcheck {status} · {detail}")
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
