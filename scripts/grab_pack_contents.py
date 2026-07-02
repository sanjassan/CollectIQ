#!/usr/bin/env python3
"""
grab_pack_contents.py — 抓「卡機內容目錄」（開抽前就能拿到的全池清單）。

背景：
  Renaiss 官網 /gacha/{slug} 頁面用 tRPC `cardPack.getContent`（packId + tiers）
  把整台卡機「宣稱裝了哪些卡」一次吐出來——連 countdown（尚未開抽、還沒上鏈）
  的限量卡機也有。每張卡含：名稱 / 圖片 / 分級 / 年份 / Renaiss 買回基準價，
  且圖片 URL 內嵌鑑定證號（PSA/BGS cert）——這就是對真實市場價的萬用鍵。

因此本檔可在「公告前、上鏈前」就把整池卡表落地，之後：
  1) 用 cert 對 Index API / 外部來源補真實市價 → 幸運值（藏寶卡）排序；
  2) 開抽上鏈後由 watch_new_pool 抓到卡池位址，再把 item_id ↔ token_id 串起來。

用法：
  python3 grab_pack_contents.py            # 抓全部卡機
  python3 grab_pack_contents.py <packId>   # 只抓單一卡機
  python3 grab_pack_contents.py --gems     # 印出目前藏寶卡排行（需已補市價）
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))          # renaiss_api.py 在專案根目錄
import ledger  # noqa: E402

TRPC = "https://www.renaiss.xyz/api/trpc"
HEADERS = {"accept": "application/json", "user-agent": "Mozilla/5.0",
           "referer": "https://www.renaiss.xyz/"}
TIERS = ["TOP", "S", "A", "B", "C", "D"]
# 圖片 URL 形如 .../pokemon-cards/PSA136046059/card.jpg → 取資料夾名當證號
CERT_RE = re.compile(r"/([A-Za-z]{2,5}\d{4,})/[^/]+\.(?:jpg|jpeg|png|webp)", re.I)


def _get(proc: str, payload: dict, retries: int = 3) -> dict:
    inp = urllib.parse.quote(json.dumps({"0": {"json": payload}}))
    url = f"{TRPC}/{proc}?batch=1&input={inp}"
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=45)
            if r.status_code in (429, 502, 503):
                last = f"{r.status_code}"; time.sleep(2 * (i + 1)); continue
            r.raise_for_status()
            return r.json()[0]["result"]["data"]["json"]
        except Exception as e:
            last = e; time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{proc} failed: {last}")


def list_packs() -> list[dict]:
    """所有卡機（含封存），回傳 id / name / packType / stage。"""
    data = _get("cardPack.getAll", {"includeInactive": True})
    return data.get("cardPacks", [])


def _cert_from_url(url: str) -> str | None:
    if not url:
        return None
    m = CERT_RE.search(url)
    return m.group(1).upper() if m else None


def grab_pack(core, pack: dict) -> dict:
    """抓單一卡機的全池卡表，寫入 pack_content（不覆蓋已補的市價/token_id）。"""
    pid = pack.get("id")
    if not pid:
        return {"cards": 0, "skipped": "no id"}
    name = pack.get("name")
    stage = pack.get("stage")
    now = datetime.now(timezone.utc).isoformat()
    try:
        content = _get("cardPack.getContent", {"packId": pid, "tiers": TIERS})
    except Exception as e:
        return {"cards": 0, "error": str(e)}
    tiers = content.get("tiers", {}) or {}

    payload = []
    tier_counts: dict[str, int] = {}
    for tier, blob in tiers.items():
        cards = (blob or {}).get("cards", []) or []
        tier_counts[tier] = len(cards)
        for c in cards:
            entry_id = c.get("id")          # 抽卡槽唯一鍵
            if not entry_id:
                continue
            item_id = c.get("itemId")        # 實體卡（可跨槽重複）
            cert = _cert_from_url(c.get("frontImageUrl"))
            try:
                buyback = float(c.get("buybackBaseValueInUSD") or 0) or None
            except (TypeError, ValueError):
                buyback = None
            payload.append((
                pid, entry_id, item_id, name, stage, tier, c.get("name"), cert,
                c.get("gradingCompany"), c.get("grade"), c.get("year"),
                c.get("frontImageUrl"), buyback, now, now,
            ))

    # UPSERT：目錄欄位刷新，但保留已補的 market_price_usd / luck_value / token_id
    core.executemany("""
        INSERT INTO pack_content
          (pack_id,entry_id,item_id,pack_name,pack_stage,tier,name,cert,grader,
           grade,year,image_url,renaiss_buyback_usd,captured_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pack_id,entry_id) DO UPDATE SET
          item_id=excluded.item_id,
          pack_name=excluded.pack_name, pack_stage=excluded.pack_stage,
          tier=excluded.tier, name=excluded.name, cert=excluded.cert,
          grader=excluded.grader, grade=excluded.grade, year=excluded.year,
          image_url=excluded.image_url,
          renaiss_buyback_usd=excluded.renaiss_buyback_usd,
          updated_at=excluded.updated_at
    """, payload)
    core.commit()
    return {"cards": len(payload), "tier_counts": tier_counts}


def grab_all(core, only: str | None = None) -> dict:
    packs = list_packs()
    if only:
        packs = [p for p in packs if p.get("id") == only]
    total = 0
    out = []
    for p in packs:
        res = grab_pack(core, p)
        n = res.get("cards", 0)
        total += n
        tc = res.get("tier_counts", {})
        tcs = " ".join(f"{t}{tc[t]}" for t in TIERS if tc.get(t))
        flag = res.get("error") or res.get("skipped") or ""
        print(f"  [{p.get('stage'):9}] {str(p.get('name'))[:34]:34} "
              f"{n:>5} 張  {tcs}  {flag}")
        out.append({"pack_id": p.get("id"), "name": p.get("name"),
                    "stage": p.get("stage"), **res})
        time.sleep(0.6)
    core.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('pack_content_grabbed_at',?)",
                 (datetime.now(timezone.utc).isoformat(),))
    core.commit()
    return {"packs": len(packs), "cards": total, "detail": out}


def enrich_market(core, limit: int = 90, pack_id: str | None = None) -> dict:
    """用鑑定證號對 Index API 補真實市價，回填 market_price_usd / luck_value。

    Index API 匿名每日 100 筆，故：
      - 依 DISTINCT cert 去重（同一張實體卡多槽只查一次）；
      - 優先序：countdown 卡機 > 高階(TOP>S>A>B>C) > 官方買回高者；
      - 連續 429 即停（配額用盡），已補的照樣落地。
    一次補會更新「所有同 cert 槽位」，故加權 EV 也一起受惠。
    """
    import renaiss_api as api
    now = datetime.now(timezone.utc).isoformat()
    where = "WHERE cert IS NOT NULL AND market_price_usd IS NULL"
    params: list = []
    if pack_id:
        where += " AND pack_id=?"
        params.append(pack_id)
    rows = core.execute(f"""
        SELECT cert,
               MIN(renaiss_buyback_usd) AS rb,
               MIN(CASE tier WHEN 'TOP' THEN 0 WHEN 'S' THEN 1 WHEN 'A' THEN 2
                             WHEN 'B' THEN 3 WHEN 'C' THEN 4 ELSE 5 END) AS trank,
               MAX(CASE pack_stage WHEN 'countdown' THEN 1 ELSE 0 END) AS live
        FROM pack_content {where}
        GROUP BY cert
        ORDER BY live DESC, trank ASC, rb DESC
        LIMIT ?
    """, (*params, limit)).fetchall()

    ok = miss = quota = 0
    consecutive_429 = 0
    for (cert, rb, _tr, _lv) in rows:
        try:
            g = api.get_graded(cert)
            price = (g.get("card") or {}).get("price_usd")
        except Exception:
            price = None
        if price is None:
            # 分辨配額用盡 vs 查無此卡：粗略以連續失敗判定
            miss += 1
            consecutive_429 += 1
            if consecutive_429 >= 8:
                quota = 1
                break
            continue
        consecutive_429 = 0
        luck = round(price / rb, 4) if rb else None
        core.execute("""
            UPDATE pack_content
            SET market_price_usd=?, luck_value=?, updated_at=?
            WHERE cert=?""", (price, luck, now, cert))
        ok += 1
    core.commit()
    return {"enriched": ok, "missed": miss, "quota_hit": bool(quota),
            "candidates": len(rows)}


def show_gems(core, limit: int = 30) -> None:
    """目前已知市價下的藏寶卡排行（幸運值 = 市價 / 官方買回）。"""
    rows = core.execute("""
        SELECT pack_name, tier, name, cert, renaiss_buyback_usd,
               market_price_usd, luck_value
        FROM pack_content
        WHERE luck_value IS NOT NULL
        ORDER BY luck_value DESC LIMIT ?
    """, (limit,)).fetchall()
    if not rows:
        print("尚無市價資料（market_price_usd 全空）——需先用 cert 補真實市價。")
        n = core.execute("SELECT COUNT(*), COUNT(cert) FROM pack_content").fetchone()
        print(f"目前 pack_content 共 {n[0]} 張，其中 {n[1]} 張有鑑定證號可對市場。")
        return
    print(f"{'幸運':>5}  {'官方買回':>10}  {'市場':>10}  分級 卡名")
    for (pn, tier, name, cert, rb, mp, lv) in rows:
        print(f"{lv:>5.2f}  ${rb or 0:>9,.0f}  ${mp or 0:>9,.0f}  [{tier}] {str(name)[:50]}")


def main() -> int:
    core = ledger.init_db()
    args = sys.argv[1:]
    if args and args[0] == "--gems":
        show_gems(core)
        return 0
    if args and args[0] == "--enrich":
        limit = int(args[1]) if len(args) > 1 else 90
        st = enrich_market(core, limit)
        print(f"[enrich] 補市價 {st['enriched']}/{st['candidates']} 張唯一卡"
              f"（查無 {st['missed']}"
              f"{'，配額用盡' if st['quota_hit'] else ''}）")
        show_gems(core)
        return 0
    only = args[0] if args else None
    t0 = time.time()
    st = grab_all(core, only)
    print(f"[grab] {datetime.now(timezone.utc):%F %T}Z 卡機 {st['packs']} 台 · "
          f"卡表 {st['cards']} 張入庫 ({time.time()-t0:.1f}s)")
    # 目錄統計
    n, ncert, nmkt = core.execute(
        "SELECT COUNT(*), COUNT(cert), COUNT(market_price_usd) FROM pack_content"
    ).fetchone()
    print(f"[grab] pack_content 總計 {n} 張 · 有證號 {ncert} · 已補市價 {nmkt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
