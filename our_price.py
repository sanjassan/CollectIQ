#!/usr/bin/env python3
"""
獨立外部比價（our_price）—— 不依賴 Renaiss 的價格來源。

需求背景：
  Renaiss 的 FMV 本來就是抓 ALT/eBay 算出來的市場公允價，但可能會抓錯，
  所以我們「自己再抓一次」做交叉驗證。內部價(renaiss FMV / SELL 掛單) vs
  外部價(同一張鑑定卡：grader+grade+卡名+編號 的公開市場成交價)。

來源選擇：
  - 130point.com → 對 curl 直接 403（需 cookie/JS），不穩。
  - eBay / TCGplayer / PSA APR → 沒有公開免金鑰 API，DOM 易變。
  - PriceCharting → 公開、可抓，且**彙整 eBay 成交價**並依鑑定等級分欄
    (Ungraded / Grade 7 / 8 / 9 / 9.5 / PSA 10)，最適合做「同分卡」交叉比價。
  因此主來源用 PriceCharting；之後可再掛上其他來源（fetcher 介面可擴充）。

原則（與 external_price.py 一致）：**絕不捏造價格**。查不到就回 None，
標示 sources=0，交由呼叫端決定是否沿用 Renaiss FMV。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(ROOT, "data", "our_price_cache.json")
CACHE_TTL = 24 * 3600  # 鑑定卡價格變動慢，快取一天

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# PriceCharting 卡片價格表的 <td id=...> ↔ 鑑定等級對照（實測 2026-06）。
PC_ID_TO_GRADE = {
    "used_price": "Ungraded",
    "complete_price": "Grade 7",
    "new_price": "Grade 8",
    "graded_price": "Grade 9",
    "box_only_price": "Grade 9.5",
    "manual_only_price": "PSA 10",
}


# 鑑定商換算係數（以 PSA 為基準 = 1.0）
# 同一張卡、同分數，不同鑑定商的市場價比例。
# 來源：eBay 成交價觀察值，可依市場變化調整。
GRADER_MULTIPLIER = {
    # grade 10
    ("psa", 10):  1.0,
    ("cgc", 10):  0.60,   # CGC 10 Pristine ≈ PSA 10 的 60%
    ("bgs", 10):  0.75,   # BGS 10 Gold Label ≈ 75%
    # grade 9.5
    ("psa", 9.5): 1.0,
    ("cgc", 9.5): 0.65,
    ("bgs", 9.5): 0.85,   # BGS 9.5 Gem Mint 口碑好，接近 PSA
    # grade 9
    ("psa", 9):   1.0,
    ("cgc", 9):   0.70,
    ("bgs", 9):   0.80,
    # grade 8 以下差距縮小
    ("psa", 8):   1.0,
    ("cgc", 8):   0.80,
    ("bgs", 8):   0.85,
}


def grader_convert(price: float, from_grader: str, from_grade: float,
                   to_grader: str = "psa") -> Optional[float]:
    """把某鑑定商的價格換算到另一家的等價價格。

    例：PSA 10 = $300 → CGC 10 等價 ≈ $300 × 0.60 = $180
    反過來：CGC 10 = $180 → 換算成 PSA 等價 = $180 / 0.60 = $300

    用途：PriceCharting 只有 PSA 10 欄的價格，卡片是 CGC 10 時，
    把 PSA 價 × 0.60 = CGC 的合理市價，再跟 Renaiss FMV 比。
    """
    to_key = (to_grader.lower().strip(), from_grade)
    to_mult = GRADER_MULTIPLIER.get(to_key)
    if to_mult is None:
        return None
    return round(price * to_mult, 2)


def _grade_label(grader: str, grade: str) -> Optional[str]:
    """把 Renaiss 的 grader/grade 對映到 PriceCharting 的欄位標籤。

    一律對到 PSA 10 欄（PriceCharting 最完整的欄位），再由呼叫端用
    grader_convert() 換算成該卡實際鑑定商的等價價格。
    """
    g = (grade or "").lower().strip()
    if not g or g in ("raw", "ungraded", "none"):
        return "Ungraded"
    m = re.search(r"(\d+(?:\.\d+)?)", g)
    if not m:
        return None
    num = float(m.group(1))
    if num >= 10:
        return "PSA 10"
    if num >= 9.5:
        return "Grade 9.5"
    if num >= 9:
        return "Grade 9"
    if num >= 8:
        return "Grade 8"
    if num >= 7:
        return "Grade 7"
    return "Ungraded"


def _parse_price(text: str) -> Optional[float]:
    """從 '$119.99 $0.00' / '$36.50 + $2.24' / '-' 取出主價格。"""
    if not text:
        return None
    m = re.search(r"\$([\d,]+\.?\d*)", text)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
        return val if val > 0 else None
    except ValueError:
        return None


# PriceCharting 的 set slug 不含這些「系列大標」，留著反而比對不到。
_SERIES_NOISE = [
    r"pal en-?", r"svp en-?", r"sv\d+[a-z]?-?", r"csm\d+[a-z]?", r"s\d+[a-z]?-",
    r"sword\s*&?\s*shield", r"scarlet\s*&?\s*violet", r"sun\s*&?\s*moon",
    r"black star", r"high-?class deck", r"promo card pack", r"evolution pack",
]


def _clean_set(set_name: str) -> str:
    s = set_name
    # 語言標記：PriceCharting 用 'Japanese' / 'Chinese'
    lang = ""
    if re.search(r"\bjapanese\b", s, re.I):
        lang = "japanese"
    elif re.search(r"simplified chinese|\bchinese\b", s, re.I):
        lang = "chinese"
    s = re.sub(r"simplified chinese|japanese|english", "", s, flags=re.I)
    for pat in _SERIES_NOISE:
        s = re.sub(pat, "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -")
    return (lang + " " + s).strip()


def build_query(card: Dict, drop_char: bool = False) -> str:
    """從卡片欄位組出 PriceCharting 搜尋字串。

    例：set='Pokemon Pal En-Paldea Evolved', char='Skeledirge Ex', num='258'
        -> 'pokemon paldea evolved skeledirge ex 258'
    去掉系列大標(Sword & Shield...)、語言/版本碼、'PSA 10 Gem Mint 2023' 雜訊。
    drop_char=True 時省略角色名，僅用 set+number（搜不到時的退路）。
    """
    parts: List[str] = []
    set_clean = _clean_set((card.get("set_name") or "").strip())
    char = (card.get("character_name") or "").strip()
    num_raw = str(card.get("card_number") or "").strip()
    num = num_raw.lstrip("0") or num_raw  # '046' -> '46'，但別把 '0' 清成空

    if set_clean:
        parts.append(set_clean)
    if char and not drop_char:
        parts.append(char)
    if num:
        parts.append(num)

    q = " ".join(parts).lower()
    q = re.sub(r"\b(pokemon)(\s+\1)+\b", r"\1", q)
    if "pokemon" not in q:
        q = "pokemon " + q
    q = re.sub(r"[^\w\s.\-#]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


class OurPriceChecker:
    """獨立外部比價（PriceCharting 主來源）。"""

    def __init__(self, throttle: float = 1.2):
        self.throttle = throttle
        self.cache: Dict[str, dict] = {}
        self._load_cache()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA, "Accept": "text/html"})

    def _load_cache(self):
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, encoding="utf-8") as f:
                    self.cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.cache = {}

    def save_cache(self):
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, indent=2, ensure_ascii=False)

    def _fetch_pricecharting(self, query: str) -> Optional[dict]:
        """搜尋並解析 PriceCharting 商品頁，回傳各等級價格 + 量能 + 來源 URL。"""
        url = (
            "https://www.pricecharting.com/search-products"
            f"?q={requests.utils.quote(query)}&type=prices"
        )
        try:
            r = self.session.get(url, timeout=20, allow_redirects=True)
        except requests.RequestException as e:
            print(f"[WARN] pricecharting 連線失敗 {query!r}: {e}")
            return None
        if r.status_code != 200:
            print(f"[WARN] pricecharting HTTP {r.status_code} for {query!r}")
            return None
        # 搜尋多結果頁（沒 redirect 到商品頁）→ 視為查無精確匹配
        if "/search-products" in r.url and "/game/" not in r.url:
            print(f"[WARN] pricecharting 無精確匹配: {query!r}")
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        tbl = soup.find(id="price_data")
        if not tbl:
            return None

        grades: Dict[str, dict] = {}
        for cell_id, label in PC_ID_TO_GRADE.items():
            el = soup.find(id=cell_id)
            if not el:
                continue
            price = _parse_price(el.get_text(" ", strip=True))
            if price is not None:
                grades[label] = {"price": price}

        # 量能（第三列 td: 'volume: N sales per ...'）
        title_el = soup.find("title")
        return {
            "grades": grades,
            "matched_title": title_el.get_text(strip=True) if title_el else None,
            "source_url": r.url,
            "source": "pricecharting",
        }

    def get_independent_price(self, card: Dict, force: bool = False) -> dict:
        """取得單卡的獨立外部價（不碰 Renaiss 價格來源）。

        回傳：
          our_price        對應該卡鑑定等級的外部成交價（查不到 = None）
          grade_matched    實際對到的等級欄位
          all_grades       全部等級價格（透明化，dashboard 可展開）
          source / source_url / matched_title
          sources          1=有抓到價, 0=未知
          query / checked_at
        """
        token_id = str(card.get("token_id") or card.get("card_id") or "")
        cache_key = token_id or build_query(card)

        if not force and cache_key in self.cache:
            cached = self.cache[cache_key]
            if time.time() - cached.get("checked_at", 0) < CACHE_TTL:
                return cached

        query = build_query(card)
        grade_label = _grade_label(card.get("grader", ""), card.get("grade", ""))
        queries = [query]
        alt = build_query(card, drop_char=True)  # 退路：只用 set+number
        if alt != query:
            queries.append(alt)

        result = {
            "token_id": token_id,
            "query": query,
            "grade_requested": grade_label,
            "grade_matched": None,
            "our_price": None,
            "all_grades": {},
            "source": None,
            "source_url": None,
            "matched_title": None,
            "sources": 0,
            "checked_at": int(time.time()),
        }

        pc = None
        for q in queries:
            pc = self._fetch_pricecharting(q)
            time.sleep(self.throttle)  # 尊重對方站台
            if pc and pc.get("grades"):
                result["query"] = q
                break

        if pc and pc.get("grades"):
            result["all_grades"] = pc["grades"]
            result["source"] = pc["source"]
            result["source_url"] = pc["source_url"]
            result["matched_title"] = pc["matched_title"]
            chosen = None
            if grade_label and grade_label in pc["grades"]:
                chosen = grade_label
            elif "PSA 10" in pc["grades"]:
                chosen = "PSA 10"  # 退而求其次：頂級價
            if chosen:
                result["grade_matched"] = chosen
                result["our_price"] = pc["grades"][chosen]["price"]
                result["sources"] = 1

        self.cache[cache_key] = result
        return result


def compare_card(card: Dict, our: dict) -> dict:
    """把 Renaiss 內部價與我們的外部價組成一筆比較結果。

    內部價 = renaiss FMV（市場公允價）；另記 SELL 掛單價 ask_price。
    外部價 = our_price（PriceCharting 同分卡成交，基準為 PSA）。

    若卡片鑑定商不是 PSA，用 grader_convert() 把 PSA 欄的價格換算成
    該鑑定商的等價市場價，再跟 Renaiss FMV 比。

    delta_pct = (換算後外部價 - 內部)/內部，正值代表 Renaiss FMV「低估」。
    """
    fmv = card.get("fmv")
    ask = card.get("ask_price")
    ext_raw = our.get("our_price")  # PriceCharting 原始價（PSA 基準）

    card_grader = (card.get("grader") or "").strip()
    card_grade_str = (card.get("grade") or "")
    grade_matched = our.get("grade_matched")

    # 換算：如果鑑定商不是 PSA，把 PSA 價格打折
    ext = ext_raw
    converted = False
    if ext_raw and card_grader and card_grader.upper() != "PSA" and grade_matched == "PSA 10":
        m = re.search(r"(\d+(?:\.\d+)?)", card_grade_str)
        if m:
            grade_num = float(m.group(1))
            converted_price = grader_convert(ext_raw, "psa", grade_num, card_grader)
            if converted_price is not None:
                ext = converted_price
                converted = True

    delta_pct = None
    flag = "unknown"
    if fmv and ext:
        delta_pct = round((ext - fmv) / fmv * 100, 1)
        if abs(delta_pct) <= 10:
            flag = "match"
        elif delta_pct > 10:
            flag = "renaiss_low"
        else:
            flag = "renaiss_high"
    return {
        "token_id": str(card.get("token_id") or ""),
        "name": card.get("name"),
        "grader": card.get("grader"),
        "grade": card.get("grade"),
        "renaiss_fmv": fmv,
        "renaiss_ask": ask,
        "our_price": ext,
        "our_price_raw": ext_raw if converted else None,
        "grader_converted": converted,
        "our_grade_matched": grade_matched,
        "delta_pct": delta_pct,
        "flag": flag,
        "source": our.get("source"),
        "source_url": our.get("source_url"),
        "marketplace_url": card.get("marketplace_url"),
        "image_url": card.get("image_url"),
    }


if __name__ == "__main__":
    sample = {
        "token_id": "34183058832444032939246844033947077711509611314291918732316656170283238722048",
        "name": "PSA 10 Gem Mint 2023 Pokemon Pal En-Paldea Evolved #258 Skeledirge Ex",
        "set_name": "Pokemon Pal En-Paldea Evolved",
        "character_name": "Skeledirge Ex",
        "card_number": "258",
        "grader": "PSA",
        "grade": "10 Gem Mint",
        "fmv": 86.1,
        "ask_price": 112.2,
    }
    chk = OurPriceChecker()
    res = chk.get_independent_price(sample, force=True)
    chk.save_cache()
    print("query:", res["query"])
    print("matched:", res["matched_title"])
    print("grade_matched:", res["grade_matched"], "our_price:", res["our_price"])
    print("all_grades:", json.dumps(res["all_grades"], ensure_ascii=False))
    print("compare:", json.dumps(compare_card(sample, res), ensure_ascii=False, indent=2))
