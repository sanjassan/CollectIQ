#!/usr/bin/env python3
"""
friday_patrol.py — 週五限量卡機開放窗口「保險巡檢」（08:00–10:00 UTC）。

機器時區是 PDT，不靠 launchd 本地日曆（易時區錯位）→ 本腳本自行用 UTC 判窗口：
只在「週五 08:00–10:00 UTC」動作，其餘時間立即結束（近乎零成本，可高頻排程）。

窗口內每次巡檢：
  1) 確認 pool_live_monitor 有在更新（live_pool.db 的 updated_at 夠新）；太舊就 kickstart 重啟。
  2) 確認 track_pulls_onchain（ai.renaiss.pulls）也活著；太舊就重啟。
  3) 確認是否已探測到本檔限量池（live_target.json）。
  4) 只在「狀態變化 / 重啟 / 窗口首次」時發 Telegram，避免洗頻。

排程：launchd ai.renaiss.fridaypatrol，StartInterval=300（每 5 分；窗口外瞬間結束）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
DATA = ROOT / "data"
STATE = DATA / "friday_patrol_state.json"
LIVE_DB = DATA / "live_pool.db"
TARGET = DATA / "live_target.json"

FRESH_LIMIT = 300  # 秒：monitor 更新超過此秒數視為卡住


def _uid() -> str:
    return str(os.getuid())


def _in_window() -> bool:
    now = datetime.now(timezone.utc)
    # isoweekday(): 週五=5
    return now.isoweekday() == 5 and 8 <= now.hour < 10


def _kickstart(label: str):
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{_uid()}/{label}"],
                   capture_output=True)


def _mtime_age(p: Path) -> float | None:
    return (time.time() - p.stat().st_mtime) if p.exists() else None


def _monitor_age() -> float | None:
    """live_pool.db 最新 pool_meta.updated_at 距今秒數。"""
    if not LIVE_DB.exists():
        return None
    import sqlite3
    try:
        db = sqlite3.connect(LIVE_DB)
        row = db.execute("SELECT MAX(updated_at) FROM pool_meta").fetchone()
        db.close()
        if not row or not row[0]:
            return None
        t = datetime.fromisoformat(row[0])
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return None


def _pool_status() -> dict:
    out = {"discovered": False, "address": None, "loaded": 0, "pulled": 0, "buyers": 0}
    try:
        tgt = json.loads(TARGET.read_text())
    except Exception:
        tgt = {}
    if tgt.get("address"):
        out["discovered"] = True
        out["address"] = tgt["address"]
        import sqlite3
        try:
            db = sqlite3.connect(LIVE_DB)
            r = db.execute("SELECT loaded,pulled,distinct_buyers FROM pool_meta WHERE address=?",
                           (tgt["address"],)).fetchone()
            db.close()
            if r:
                out.update(loaded=r[0] or 0, pulled=r[1] or 0, buyers=r[2] or 0)
        except Exception:
            pass
    return out


def main() -> int:
    if not _in_window():
        return 0  # 窗口外：瞬間結束

    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except Exception:
            prev = {}

    actions = []
    # 1) monitor 活著嗎？
    mage = _monitor_age()
    if mage is None or mage > FRESH_LIMIT:
        _kickstart("ai.renaiss.livepool")
        actions.append(f"重啟 livepool（上次更新 {('無' if mage is None else str(int(mage))+'s前')}）")
    # 2) pulls 追蹤活著嗎？
    pulls_age = _mtime_age(Path.home() / ".hermes" / "logs" / "renaiss-pulls.stdout.log")
    if pulls_age is None or pulls_age > 600:
        _kickstart("ai.renaiss.pulls")
        actions.append("重啟 pulls 追蹤")

    status = _pool_status()
    now = datetime.now(timezone.utc)

    # 決定要不要發 TG：窗口首次 / 池首次被探測到 / 抽卡數大幅推進 / 有重啟動作
    first_in_window = prev.get("window_date") != now.strftime("%F")
    newly_discovered = status["discovered"] and not prev.get("discovered")
    pulled_jump = status["pulled"] - prev.get("pulled", 0) >= 50
    should_notify = first_in_window or newly_discovered or pulled_jump or actions

    if should_notify:
        from hermes_notify import tg
        lines = [f"🛡️ *週五巡檢* {now:%H:%M}UTC"]
        if status["discovered"]:
            lines.append(f"✅ 已鎖定池 `{status['address']}`")
            lines.append(f"灌卡 {status['loaded']} · 已抽 {status['pulled']} · 買家 {status['buyers']}")
            lines.append("→ /live 面板即時追蹤")
        else:
            lines.append("⏳ 尚未偵測到已灌卡的新池（監控中，開池瞬間會通知）")
        if actions:
            lines.append("🔧 " + "；".join(actions))
        tg("\n".join(lines))

    STATE.write_text(json.dumps({
        "window_date": now.strftime("%F"), "checked_at": now.isoformat(),
        "discovered": status["discovered"], "pulled": status["pulled"],
        "monitor_age": mage, "actions": actions,
    }, ensure_ascii=False, indent=2))
    print(f"[{now:%F %T}Z] 巡檢：monitor_age={mage} pool={status['discovered']} "
          f"pulled={status['pulled']} actions={actions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
