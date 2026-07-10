#!/usr/bin/env python3
"""hermes_notify.py — shared Telegram notifications (reads credentials from ~/.hermes/.env).

~/.hermes/.env has TELEGRAM_BOT_TOKEN and TELEGRAM_HOME_CHANNEL (= the user's own chat id),
but main.TelegramAlert expects TELEGRAM_CHAT_ID, so this maps HOME_CHANNEL over to it.
"""
from __future__ import annotations

import os
from pathlib import Path

_LOADED = False


def load_env() -> None:
    global _LOADED
    if _LOADED:
        return
    envp = Path.home() / ".hermes" / ".env"
    if envp.exists():
        for line in envp.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if k.startswith("TELEGRAM") and k not in os.environ:
                os.environ[k] = v
    # TelegramAlert uses TELEGRAM_CHAT_ID; fall back to HOME_CHANNEL if it's absent
    if not os.environ.get("TELEGRAM_CHAT_ID") and os.environ.get("TELEGRAM_HOME_CHANNEL"):
        os.environ["TELEGRAM_CHAT_ID"] = os.environ["TELEGRAM_HOME_CHANNEL"]
    _LOADED = True


def tg(message: str, image_path: str | None = None) -> bool:
    """Send a Telegram message (optionally with an image). Returns whether it was sent."""
    load_env()
    import sys
    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT))
    try:
        from main import TelegramAlert
        alert = TelegramAlert()
        if not alert.is_configured():
            print("[tg] 未設定（缺 BOT_TOKEN / CHAT_ID）")
            return False
        return bool(alert.send_alert(message, image_path=image_path))
    except Exception as e:
        print(f"[tg] 失敗：{e}")
        return False


if __name__ == "__main__":
    import sys
    msg = sys.argv[1] if len(sys.argv) > 1 else "CollectIQ 測試通知 ✅"
    print("sent:", tg(msg))
