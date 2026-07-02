#!/usr/bin/env python3
"""
enrich.py — 補資料管線：Index API 優先佇列 + 卡牌圖片本地快取。

兩個問題本檔各處理一半：
  1) Index API 每日僅 100 筆匿名配額。既有 build_universe.enrich_from_index
     只依 renaiss_fmv 排序，不知道「哪些卡正在活躍池內 / 剛被抽走 / 是大獎」。
     build_enrich_queue() 依池脈絡計算 priority，讓每日配額優先花在最有價值的卡。
  2) dim_card 只存 image_url，原始 URL 失效就丟圖。cache_images() 落地到
     data/img/{token_id}.jpg 並回填 dim_card.image_local。

用法：
  python3 enrich.py queue                 # 重建優先佇列
  python3 enrich.py images [--limit N]    # 下載前 N 個待快取圖片（依 priority）
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ledger  # noqa: E402

IMG_DIR = ROOT / "data" / "img"
BIG_FMV = 300.0


def build_enrich_queue(core) -> dict:
    """重建 enrich_queue：找出缺 Index 真實價的卡，依池脈絡排優先序。

    priority 分數（越大越先查）：
      + 2000  大獎（renaiss_fmv >= BIG_FMV）
      + 1000  目前在活躍池內（fact_holding.status='in_pool'）
      +  500  目前被錢包持有（可能剛被抽走）
      + min(renaiss_fmv, 900)  價值加權
    """
    # 每 token 最新 fmv 快照是否已有 index 價
    have_idx = {str(t) for (t,) in core.execute(
        "SELECT DISTINCT token_id FROM fmv_snapshots "
        "WHERE index_price_usd IS NOT NULL").fetchall()}

    # 候選：dim_card 有圖或有 renaiss 基準，且尚無 index 價
    rows = core.execute("""
        SELECT d.token_id, d.tier, d.image_url,
               fh.status,
               (SELECT renaiss_fmv FROM fmv_snapshots s
                WHERE s.token_id = d.token_id
                ORDER BY s.ts DESC LIMIT 1) AS rfmv
        FROM dim_card d
        LEFT JOIN fact_holding fh ON fh.token_id = d.token_id
    """).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for (tid, tier, img, status, rfmv) in rows:
        tid = str(tid)
        if tid in have_idx:
            continue
        rfmv = rfmv or 0
        prio, reasons = 0, []
        if rfmv >= BIG_FMV:
            prio += 2000; reasons.append("big")
        if status == "in_pool":
            prio += 1000; reasons.append("in_pool")
        elif status == "held":
            prio += 500; reasons.append("held")
        prio += min(int(rfmv), 900)
        payload.append((tid, prio, ",".join(reasons) or "base", now, 0))

    core.executemany(
        "INSERT OR REPLACE INTO enrich_queue"
        "(token_id,priority,reason,added_at,done) VALUES(?,?,?,?,?)", payload)
    core.commit()
    return {"queued": len(payload)}


def cache_images(core, limit: int = 50) -> dict:
    """下載尚未快取的圖片到 data/img/，回填 dim_card.image_local。依 priority。"""
    import requests
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    rows = core.execute("""
        SELECT d.token_id, d.image_url
        FROM dim_card d
        LEFT JOIN enrich_queue q ON q.token_id = d.token_id
        WHERE d.image_url IS NOT NULL AND d.image_url != ''
          AND (d.image_local IS NULL OR d.image_local = '')
        ORDER BY COALESCE(q.priority, 0) DESC
        LIMIT ?
    """, (limit,)).fetchall()
    ok = fail = 0
    for (tid, url) in rows:
        dest = IMG_DIR / f"{tid}.jpg"
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            dest.write_bytes(r.content)
            core.execute("UPDATE dim_card SET image_local=? WHERE token_id=?",
                         (str(dest.relative_to(ROOT)), str(tid)))
            ok += 1
        except Exception as e:
            print(f"  [fail] {str(tid)[:12]}… {e}")
            fail += 1
    core.commit()
    return {"cached": ok, "failed": fail, "attempted": len(rows)}


def main() -> int:
    core = ledger.init_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "queue"
    if cmd == "queue":
        st = build_enrich_queue(core)
        top = core.execute(
            "SELECT token_id, priority, reason FROM enrich_queue "
            "WHERE done=0 ORDER BY priority DESC LIMIT 5").fetchall()
        print(f"[enrich] 佇列 {st['queued']} 筆待補")
        for (t, p, r) in top:
            print(f"  prio={p:5} [{r}] {str(t)[:16]}…")
    elif cmd == "images":
        limit = 50
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        st = cache_images(core, limit)
        print(f"[enrich] 圖片快取：成功 {st['cached']} / 失敗 {st['failed']} "
              f"（嘗試 {st['attempted']}）")
    else:
        print(f"unknown cmd: {cmd}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
