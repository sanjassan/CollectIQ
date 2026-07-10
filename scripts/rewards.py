#!/usr/bin/env python3
"""
rewards.py — integrate project reward mechanics into reward_status (MART layer).

Three types:
  eevee_full   Eeveelution full set: each holder's completion of all 9 Eevee evolutions and which are missing.
  serial_run   Serial run: combinations of same holder, same character, consecutive serial numbers (>=2).
               Note: the serial source is currently the grading cert number (PSA/BGS cert), so this is a
               "consecutive cert number" heuristic; once the project provides print serials, the same logic
               can be reused and refined directly.
  sbt          SBT (soulbound) reward: aggregate sbt_awards (grant events) per wallet.
               sbt_awards is populated by monitoring from=0x0 Transfers on the SBT contract (contract address not yet wired up).

Usage: python3 rewards.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ledger  # noqa: E402

# Character family definitions: family → member base names (case-insensitive substring match)
FAMILIES = {
    "eeveelution": ["Eevee", "Vaporeon", "Jolteon", "Flareon", "Espeon",
                    "Umbreon", "Leafeon", "Glaceon", "Sylveon"],
}
EEVEE_MEMBERS = FAMILIES["eeveelution"]


def _match_member(character_name: str, members: list[str]) -> str | None:
    cn = (character_name or "").lower()
    for m in members:
        if m.lower() in cn:
            return m
    return None


def classify_families(core) -> dict:
    """Backfill dim_card.character_family (currently: eeveelution)."""
    updated = 0
    for family, members in FAMILIES.items():
        rows = core.execute(
            "SELECT token_id, character_name FROM dim_card "
            "WHERE character_name IS NOT NULL").fetchall()
        payload = [(family, str(t)) for (t, cn) in rows
                   if _match_member(cn, members)]
        core.executemany(
            "UPDATE dim_card SET character_family=? WHERE token_id=?", payload)
        updated += len(payload)
    core.commit()
    return {"classified": updated}


def compute_eevee_full(core) -> dict:
    """Each holder's Eeveelution full-set completion."""
    now = datetime.now(timezone.utc).isoformat()
    # holder -> set of owned Eevee members (held status)
    rows = core.execute("""
        SELECT fh.current_holder, d.character_name
        FROM fact_holding fh
        JOIN dim_card d ON d.token_id = fh.token_id
        WHERE fh.status='held' AND fh.current_holder IS NOT NULL
          AND d.character_family='eeveelution'
    """).fetchall()
    owned: dict[str, set] = {}
    for (holder, cname) in rows:
        m = _match_member(cname, EEVEE_MEMBERS)
        if m:
            owned.setdefault(holder, set()).add(m)

    payload = []
    achieved = 0
    for holder, members in owned.items():
        missing = [m for m in EEVEE_MEMBERS if m not in members]
        done = 1 if not missing else 0
        achieved += done
        detail = json.dumps({"count": len(members),
                             "owned": sorted(members), "missing": missing},
                            ensure_ascii=False)
        payload.append(("eevee_full", holder, holder, done, detail, now))
    core.executemany(
        "INSERT OR REPLACE INTO reward_status"
        "(reward_type,key,holder,achieved,detail,updated_at) "
        "VALUES(?,?,?,?,?,?)", payload)
    core.commit()
    return {"holders": len(payload), "completed": achieved}


def compute_serial_runs(core) -> dict:
    """Serial-run combinations: same holder, same character, consecutive serial numbers (>=2)."""
    now = datetime.now(timezone.utc).isoformat()
    rows = core.execute("""
        SELECT fh.current_holder, d.character_name, d.serial_num, d.token_id
        FROM fact_holding fh
        JOIN dim_card d ON d.token_id = fh.token_id
        WHERE fh.status='held' AND fh.current_holder IS NOT NULL
          AND d.serial_num IS NOT NULL AND d.character_name IS NOT NULL
        ORDER BY fh.current_holder, d.character_name, d.serial_num
    """).fetchall()

    # Group, then find consecutive runs
    from itertools import groupby
    payload = []
    for (holder, cname), grp in groupby(rows, key=lambda r: (r[0], r[1])):
        nums = sorted({r[2] for r in grp})
        run = [nums[0]]
        runs = []
        for n in nums[1:]:
            if n == run[-1] + 1:
                run.append(n)
            else:
                if len(run) >= 2:
                    runs.append(list(run))
                run = [n]
        if len(run) >= 2:
            runs.append(list(run))
        for r in runs:
            key = f"{holder}:{cname}:{r[0]}-{r[-1]}"
            detail = json.dumps({"character": cname, "length": len(r),
                                 "from": r[0], "to": r[-1]}, ensure_ascii=False)
            payload.append(("serial_run", key, holder, 1, detail, now))
    core.executemany(
        "INSERT OR REPLACE INTO reward_status"
        "(reward_type,key,holder,achieved,detail,updated_at) "
        "VALUES(?,?,?,?,?,?)", payload)
    core.commit()
    return {"runs": len(payload)}


def compute_sbt(core) -> dict:
    """Aggregate sbt_awards into per-wallet reward_status."""
    now = datetime.now(timezone.utc).isoformat()
    rows = core.execute(
        "SELECT wallet, COUNT(*), GROUP_CONCAT(reason) FROM sbt_awards "
        "WHERE wallet IS NOT NULL GROUP BY wallet").fetchall()
    payload = [
        ("sbt", w, w, 1,
         json.dumps({"count": n, "reasons": (reasons or "").split(",")},
                    ensure_ascii=False), now)
        for (w, n, reasons) in rows
    ]
    core.executemany(
        "INSERT OR REPLACE INTO reward_status"
        "(reward_type,key,holder,achieved,detail,updated_at) "
        "VALUES(?,?,?,?,?,?)", payload)
    core.commit()
    return {"wallets": len(payload)}


def main() -> int:
    core = ledger.init_db()
    cf = classify_families(core)
    ev = compute_eevee_full(core)
    sr = compute_serial_runs(core)
    sb = compute_sbt(core)
    print(f"[rewards] character_family 標記 {cf['classified']} 張")
    print(f"[rewards] 伊布全款：{ev['holders']} 位持有者有伊布，其中 {ev['completed']} 位集滿 9 種")
    print(f"[rewards] 連號：偵測到 {sr['runs']} 組連續序號")
    print(f"[rewards] SBT：{sb['wallets']} 個錢包有 SBT 獎勵（sbt_awards 尚待接上合約）")

    # Display: the closest to a full Eeveelution set
    top = core.execute(
        "SELECT holder, detail FROM reward_status WHERE reward_type='eevee_full' "
        "ORDER BY json_extract(detail,'$.count') DESC LIMIT 5").fetchall()
    if top:
        print("[rewards] 最接近全款：")
        for (h, d) in top:
            dd = json.loads(d)
            print(f"  {h[:14]}… {dd['count']}/9 缺 {dd['missing']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
