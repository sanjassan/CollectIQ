#!/usr/bin/env python3
"""hermes_notify.py — 共用 Telegram 通知（讀 ~/.hermes/.env 憑證）。

~/.hermes/.env 有 TELEGRAM_BOT_TOKEN 與 TELEGRAM_HOME_CHANNEL（= 使用者本人的 chat id），
但 main.TelegramAlert 期待 TELEGRAM_CHAT_ID，故此處把 HOME_CHANNEL 映射過去。
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
    # TelegramAlert 用 TELEGRAM_CHAT_ID；沒有就用 HOME_CHANNEL
    if not os.environ.get("TELEGRAM_CHAT_ID") and os.environ.get("TELEGRAM_HOME_CHANNEL"):
        os.environ["TELEGRAM_CHAT_ID"] = os.environ["TELEGRAM_HOME_CHANNEL"]
    _LOADED = True


def tg(message: str, image_path: str | None = None) -> bool:
    """發 Telegram（可帶圖）。回傳是否送出。"""
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
