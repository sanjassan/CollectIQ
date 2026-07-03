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
import random
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
# 擬真瀏覽器標頭，降低被 WAF/風控擋下的機率（僅送正常瀏覽會送的欄位）。
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "referer": "https://www.renaiss.xyz/",
    "origin": "https://www.renaiss.xyz",
    "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
}
TIERS = ["TOP", "S", "A", "B", "C", "D"]
# 圖片 URL 形如 .../pokemon-cards/PSA136046059/card.jpg → 取資料夾名當證號
CERT_RE = re.compile(r"/([A-Za-z]{2,5}\d{4,})/[^/]+\.(?:jpg|jpeg|png|webp)", re.I)

# 全域禮貌節流：所有對外請求共用同一個 Session，並在請求間插入抖動延遲，
# 避免對站方造成突發流量而被限流 / 封鎖。
_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)


def _polite_sleep(base: float = 0.8, jitter: float = 0.6) -> None:
    time.sleep(base + random.random() * jitter)


def _get(proc: str, payload: dict, retries: int = 5) -> dict:
    """打 Renaiss tRPC，遇 429/5xx 以指數退避 + 抖動重試，尊重 Retry-After。"""
    inp = urllib.parse.quote(json.dumps({"0": {"json": payload}}))
    url = f"{TRPC}/{proc}?batch=1&input={inp}"
    last = None
    for i in range(retries):
        try:
            r = _SESSION.get(url, timeout=45)
            if r.status_code in (403, 429) or r.status_code >= 500:
                ra = r.headers.get("retry-after")
                wait = float(ra) if (ra and ra.isdigit()) else (2 ** i + random.random() * 2)
                last = f"HTTP {r.status_code}"
                time.sleep(min(wait, 30)); continue
            r.raise_for_status()
            return r.json()[0]["result"]["data"]["json"]
        except Exception as e:
            last = e; time.sleep(min(2 ** i + random.random() * 2, 30))
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
                # buybackBaseValueInUSD 欄名雖寫 USD，實測為「美分」（且常是字串）。
                # 除以 100 還原成美元，與市價（get_graded 已是美元）同單位，luck 才正確。
                buyback = float(c.get("buybackBaseValueInUSD") or 0) / 100 or None
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
        _polite_sleep()
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
    consecutive_fail = 0
    for (cert, rb, _tr, _lv) in rows:
        try:
            g = api.get_graded(cert)
            price = (g.get("card") or {}).get("price_usd")
        except Exception:
            price = None
        if price is None:
            # 分辨配額用盡 vs 查無此卡：粗略以連續失敗判定
            miss += 1
            consecutive_fail += 1
            if consecutive_fail >= 8:
                quota = 1
                break
            _polite_sleep(0.5, 0.5)
            continue
        consecutive_fail = 0
        # luck_value 必須逐槽算：同一 cert 在不同 tier/卡機的官方買回價不同，
        # 用單一 cert 級比值覆蓋整批會污染（B 檔 $121 被 C 檔 $80 的比值蓋掉）。
        # 市價是每張實體卡共通、逐槽相同，故 market_price_usd 用 cert 級寫入無妨。
        # 誠實標註來源：renaissos Index 是 Renaiss 自家定價，非獨立第三方成交價。
        core.execute("""
            UPDATE pack_content
            SET market_price_usd=?,
                luck_value = CASE WHEN renaiss_buyback_usd > 0
                                  THEN ROUND(? / renaiss_buyback_usd, 4) END,
                market_source='renaiss_index', market_url=NULL, updated_at=?
            WHERE cert=?""", (price, price, now, cert))
        ok += 1
        _polite_sleep(0.5, 0.5)   # 對 Index API 也放慢，避免觸發風控
    core.commit()
    return {"enriched": ok, "missed": miss, "quota_hit": bool(quota),
            "candidates": len(rows)}


def enrich_independent(core, limit: int = 200, pack_id: str | None = None,
                       only_missing: bool = True) -> dict:
    """用『獨立第三方成交價』補市價 —— PriceCharting（彙整 eBay 已成交拍賣，
    依鑑定等級分欄）。這才是使用者要的『公信力 + 一般買家拍賣價』來源，
    與 Renaiss 自家 Index（renaiss_index）不同，可回答『幸運值是不是假的』。

    市價來源優先序：pricecharting_ebay（獨立）> renaiss_index（Renaiss 自家）。
    故預設 only_missing=True 只補「還沒有任何市價」的卡；獨立來源永遠可覆蓋
    非獨立來源，但這裡保守不主動覆蓋，交由呼叫端決定。

    PriceCharting 是公開站台、無 Index API 的每日 100 筆硬上限，但仍禮貌節流。
    比對不到（促銷卡 / 冷門日板）就留 None，絕不捏造。
    """
    import our_price
    now = datetime.now(timezone.utc).isoformat()
    src_filter = "AND market_price_usd IS NULL" if only_missing \
        else "AND (market_source IS NULL OR market_source!='pricecharting_ebay')"
    where = f"WHERE cert IS NOT NULL {src_filter}"
    params: list = []
    if pack_id:
        where += " AND pack_id=?"
        params.append(pack_id)
    rows = core.execute(f"""
        SELECT cert,
               MIN(name)                AS name,
               MIN(grader)              AS grader,
               MIN(grade)               AS grade,
               MIN(renaiss_buyback_usd) AS rb,
               MIN(CASE tier WHEN 'TOP' THEN 0 WHEN 'S' THEN 1 WHEN 'A' THEN 2
                             WHEN 'B' THEN 3 WHEN 'C' THEN 4 ELSE 5 END) AS trank,
               MAX(CASE pack_stage WHEN 'countdown' THEN 1 ELSE 0 END)  AS live
        FROM pack_content {where}
        GROUP BY cert
        ORDER BY live DESC, trank ASC, rb DESC
        LIMIT ?
    """, (*params, limit)).fetchall()

    chk = our_price.OurPriceChecker(throttle=1.2)
    ok = miss = mismatch = 0
    total = len(rows)
    for idx, (cert, name, grader, grade, rb, _tr, _live) in enumerate(rows, 1):
        card = {"name": name, "grader": grader or "PSA", "grade": grade}
        try:
            res = chk.get_independent_price(card)
        except Exception as e:
            print(f"[independent] {cert} 例外：{e}", flush=True)
            res = {}
        price = res.get("our_price")
        if price is None:
            miss += 1
            if res.get("title_mismatch"):
                mismatch += 1
        else:
            # 非 PSA 鑑定商：把 PriceCharting 的 PSA 基準價換算成該商等價市場價
            # （例 BGS 10 ≈ PSA 10 × 0.75）。換算不出係數就沿用原價。
            if (grader or "PSA").upper() != "PSA" and res.get("grade_matched") == "PSA 10":
                price = our_price.grader_convert(
                    price, "psa", 10.0, to_grader=(grader or "").lower()) or price
            price = round(float(price), 2)
            # luck_value 逐槽算（見 enrich_market 同段說明）：同 cert 不同 tier 的
            # 官方買回價不同，不可用單一比值覆蓋整批；市價逐槽共通故 cert 級寫入。
            core.execute("""
                UPDATE pack_content
                SET market_price_usd=?,
                    luck_value = CASE WHEN renaiss_buyback_usd > 0
                                      THEN ROUND(? / renaiss_buyback_usd, 4) END,
                    market_source='pricecharting_ebay', market_url=?, updated_at=?
                WHERE cert=?""", (price, price, res.get("source_url"), now, cert))
            ok += 1
        # 每 15 張落地一次：進度可見、可續跑、被中斷也不整批丟失。
        if idx % 15 == 0:
            core.commit()
            try:
                chk.save_cache()
            except Exception:
                pass
            print(f"[independent] {idx}/{total} · 命中 {ok} 查無 {miss}"
                  f"（假匹配擋 {mismatch}）", flush=True)
    core.commit()
    try:
        chk.save_cache()
    except Exception:
        pass
    return {"enriched": ok, "missed": miss, "title_mismatch": mismatch,
            "candidates": total}


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
    if args and args[0] == "--independent":
        # 用獨立第三方成交價（PriceCharting/eBay）補市價。
        # 可選 limit 與 pack_id：--independent 200 <packId>
        limit = int(args[1]) if len(args) > 1 else 200
        pid = args[2] if len(args) > 2 else None
        st = enrich_independent(core, limit, pid)
        print(f"[independent] 補獨立市價 {st['enriched']}/{st['candidates']} 張唯一卡"
              f"（查無 {st['missed']}，其中疑似同編號假匹配已擋 {st['title_mismatch']}）")
        show_gems(core)
        return 0
    if args and args[0] == "--daily":
        # 排程入口：先刷全卡機目錄（Renaiss tRPC，無每日上限），
        # 再優先用『獨立成交價』(PriceCharting/eBay，公開站台、無每日硬限) 補市價，
        # 最後用當日剩餘 Index 配額（renaiss_index，保守 90）補獨立來源查不到的缺口。
        limit = int(args[1]) if len(args) > 1 else 90
        indep_limit = int(args[2]) if len(args) > 2 else 250
        t0 = time.time()
        gs = grab_all(core)
        ei = enrich_independent(core, indep_limit)
        es = enrich_market(core, limit)
        n, ncert, nmkt = core.execute(
            "SELECT COUNT(*), COUNT(cert), COUNT(market_price_usd) FROM pack_content"
        ).fetchone()
        nindep = core.execute(
            "SELECT COUNT(*) FROM pack_content WHERE market_source='pricecharting_ebay'"
        ).fetchone()[0]
        print(f"[daily] {datetime.now(timezone.utc):%F %T}Z "
              f"目錄 {gs['packs']}台/{gs['cards']}張 · "
              f"獨立補 {ei['enriched']}（查無{ei['missed']}） · "
              f"Index補 {es['enriched']}（查無{es['missed']}"
              f"{'，配額盡' if es['quota_hit'] else ''}） · "
              f"累計市價 {nmkt}/{ncert}（獨立 {nindep}）({time.time()-t0:.1f}s)")
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
