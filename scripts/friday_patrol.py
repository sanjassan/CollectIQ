#!/usr/bin/env python3
"""
friday_patrol.py — "insurance patrol" for the Friday limited-pool open window (08:00–10:00 UTC).

The machine's timezone is PDT; rather than relying on launchd's local calendar (prone to timezone
misalignment), this script decides the window itself in UTC: it only acts during "Friday 08:00–10:00 UTC"
and exits immediately otherwise (near-zero cost, so it can be scheduled at high frequency).

Each patrol within the window:
  1) Confirm pool_live_monitor is updating (live_pool.db's updated_at is fresh enough); kickstart-restart if too stale.
  2) Confirm track_pulls_onchain (ai.renaiss.pulls) is alive too; restart if too stale.
  3) Check whether this week's limited pool has been discovered (live_target.json).
  4) Only send Telegram on "state change / restart / first patrol of the window" to avoid spam.

Schedule: launchd ai.renaiss.fridaypatrol, StartInterval=300 (every 5 min; exits instantly outside the window).
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

FRESH_LIMIT = 300  # seconds: monitor is considered stuck if it hasn't updated within this many seconds


def _uid() -> str:
    return str(os.getuid())


def _in_window() -> bool:
    now = datetime.now(timezone.utc)
    # isoweekday(): Friday=5
    return now.isoweekday() == 5 and 8 <= now.hour < 10


def _kickstart(label: str):
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{_uid()}/{label}"],
                   capture_output=True)


def _mtime_age(p: Path) -> float | None:
    return (time.time() - p.stat().st_mtime) if p.exists() else None


def _monitor_age() -> float | None:
    """Seconds since live_pool.db's latest pool_meta.updated_at."""
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
        return 0  # outside the window: exit instantly

    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except Exception:
            prev = {}

    actions = []
    # 1) Is the monitor alive?
    mage = _monitor_age()
    if mage is None or mage > FRESH_LIMIT:
        _kickstart("ai.renaiss.livepool")
        actions.append(f"restart livepool (last update {('none' if mage is None else str(int(mage))+'s ago')})")
    # 2) Is pulls tracking alive?
    pulls_age = _mtime_age(Path.home() / ".hermes" / "logs" / "renaiss-pulls.stdout.log")
    if pulls_age is None or pulls_age > 600:
        _kickstart("ai.renaiss.pulls")
        actions.append("restart pulls tracking")

    status = _pool_status()
    now = datetime.now(timezone.utc)

    # Decide whether to send TG: first patrol of the window / pool first discovered / large jump in pulls / a restart happened
    first_in_window = prev.get("window_date") != now.strftime("%F")
    newly_discovered = status["discovered"] and not prev.get("discovered")
    pulled_jump = status["pulled"] - prev.get("pulled", 0) >= 50
    should_notify = first_in_window or newly_discovered or pulled_jump or actions

    if should_notify:
        from hermes_notify import tg
        lines = [f"🛡️ *Friday patrol* {now:%H:%M}UTC"]
        if status["discovered"]:
            lines.append(f"✅ Pool locked `{status['address']}`")
            lines.append(f"loaded {status['loaded']} · pulled {status['pulled']} · buyers {status['buyers']}")
            lines.append("→ /live panel live tracking")
        else:
            lines.append("⏳ No loaded new pool detected yet (monitoring; will notify the moment a pool opens)")
        if actions:
            lines.append("🔧 " + "; ".join(actions))
        tg("\n".join(lines))

    STATE.write_text(json.dumps({
        "window_date": now.strftime("%F"), "checked_at": now.isoformat(),
        "discovered": status["discovered"], "pulled": status["pulled"],
        "monitor_age": mage, "actions": actions,
    }, ensure_ascii=False, indent=2))
    print(f"[{now:%F %T}Z] patrol: monitor_age={mage} pool={status['discovered']} "
          f"pulled={status['pulled']} actions={actions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
