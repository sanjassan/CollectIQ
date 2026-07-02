#!/usr/bin/env python3
"""
rewards.py — 整合項目方獎勵機制到 reward_status（MART 層）。

三種：
  eevee_full   伊布全款：每個持有者集齊 9 種伊布進化的完成度與缺哪幾隻。
  serial_run   連號：同一持有者、同一角色、序號連續（>=2）的組合。
               註：目前 serial 來源是鑑定證號（PSA/BGS cert），故此為「連續證號」
               啟發式；待項目方提供印刷序號後可直接沿用同邏輯精修。
  sbt          SBT 靈魂綁定獎勵：彙總 sbt_awards（授予事件）到各錢包。
               sbt_awards 由監控 SBT 合約的 from=0x0 Transfer 填入（尚待接上合約位址）。

用法：python3 rewards.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ledger  # noqa: E402

# 角色家族定義：family → 成員基礎名（大小寫不敏感的子字串比對）
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
    """回填 dim_card.character_family（目前：eeveelution）。"""
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
    """每個持有者的伊布全款完成度。"""
    now = datetime.now(timezone.utc).isoformat()
    # 持有者 -> 擁有的伊布成員集合（held 狀態）
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
    """同持有者、同角色、序號連續（>=2）的連號組合。"""
    now = datetime.now(timezone.utc).isoformat()
    rows = core.execute("""
        SELECT fh.current_holder, d.character_name, d.serial_num, d.token_id
        FROM fact_holding fh
        JOIN dim_card d ON d.token_id = fh.token_id
        WHERE fh.status='held' AND fh.current_holder IS NOT NULL
          AND d.serial_num IS NOT NULL AND d.character_name IS NOT NULL
        ORDER BY fh.current_holder, d.character_name, d.serial_num
    """).fetchall()

    # 分組後找連續段
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
    """彙總 sbt_awards 到各錢包的 reward_status。"""
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

    # 展示：最接近伊布全款的前幾名
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
