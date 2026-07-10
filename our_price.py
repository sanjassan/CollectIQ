#!/usr/bin/env python3
"""
Independent external price check (our_price) -- a price source that does
not depend on Renaiss.

Background:
  Renaiss's FMV is itself a fair market value derived from scraping ALT/eBay,
  but it can be wrong, so we "scrape it ourselves again" for cross-validation.
  Internal price (renaiss FMV / SELL listing) vs external price (the public
  market sale price of the same graded card: grader+grade+name+number).

Source selection:
  - 130point.com -> returns 403 directly to curl (needs cookie/JS); unreliable.
  - eBay / TCGplayer / PSA APR -> no public keyless API, and the DOM changes often.
  - PriceCharting -> public, scrapable, and it **aggregates eBay sale prices**
    split into columns by grade (Ungraded / Grade 7 / 8 / 9 / 9.5 / PSA 10),
    which is ideal for "same-grade card" cross-comparison.
  So the primary source is PriceCharting; other sources can be added later
  (the fetcher interface is extensible).

Principle (consistent with external_price.py): **never fabricate a price**.
When nothing is found, return None and mark sources=0, leaving it to the
caller to decide whether to keep the Renaiss FMV.
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
CACHE_TTL = 24 * 3600  # graded card prices move slowly; cache for a day

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Mapping of PriceCharting price-table <td id=...> to grade (verified 2026-06).
PC_ID_TO_GRADE = {
    "used_price": "Ungraded",
    "complete_price": "Grade 7",
    "new_price": "Grade 8",
    "graded_price": "Grade 9",
    "box_only_price": "Grade 9.5",
    "manual_only_price": "PSA 10",
}


# Grader conversion factors (PSA as the baseline = 1.0).
# The market price ratio between graders for the same card at the same grade.
# Source: observed eBay sale prices; adjust as the market shifts.
GRADER_MULTIPLIER = {
    # grade 10
    ("psa", 10):  1.0,
    ("cgc", 10):  0.60,   # CGC 10 Pristine ≈ 60% of PSA 10
    ("bgs", 10):  0.75,   # BGS 10 Gold Label ≈ 75%
    # grade 9.5
    ("psa", 9.5): 1.0,
    ("cgc", 9.5): 0.65,
    ("bgs", 9.5): 0.85,   # BGS 9.5 Gem Mint has a strong reputation, close to PSA
    # grade 9
    ("psa", 9):   1.0,
    ("cgc", 9):   0.70,
    ("bgs", 9):   0.80,
    # gap narrows at grade 8 and below
    ("psa", 8):   1.0,
    ("cgc", 8):   0.80,
    ("bgs", 8):   0.85,
}


def grader_convert(price: float, from_grader: str, from_grade: float,
                   to_grader: str = "psa") -> Optional[float]:
    """Convert a price from one grader to the equivalent price of another.

    e.g. PSA 10 = $300 -> CGC 10 equivalent ≈ $300 × 0.60 = $180
    and back: CGC 10 = $180 -> converted to PSA equivalent = $180 / 0.60 = $300

    Use case: PriceCharting only has the PSA 10 column price. When the card is
    CGC 10, PSA price × 0.60 = the fair CGC market price, then compare against
    the Renaiss FMV.
    """
    to_key = (to_grader.lower().strip(), from_grade)
    to_mult = GRADER_MULTIPLIER.get(to_key)
    if to_mult is None:
        return None
    return round(price * to_mult, 2)


def _grade_label(grader: str, grade: str) -> Optional[str]:
    """Map Renaiss's grader/grade to a PriceCharting column label.

    Always maps to the PSA 10 column (PriceCharting's most complete column);
    the caller then uses grader_convert() to convert to the equivalent price
    for the card's actual grader.
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
    """Extract the primary price from '$119.99 $0.00' / '$36.50 + $2.24' / '-'."""
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


# PriceCharting set slugs don't include these "series headings"; keeping them
# actually prevents a match.
_SERIES_NOISE = [
    r"pal en-?", r"svp en-?", r"sv\d+[a-z]?-?", r"csm\d+[a-z]?", r"s\d+[a-z]?-",
    r"sword\s*&?\s*shield", r"scarlet\s*&?\s*violet", r"sun\s*&?\s*moon",
    r"black star", r"high-?class deck", r"promo card pack", r"evolution pack",
]


def _clean_set(set_name: str) -> str:
    s = set_name
    # Language markers: PriceCharting uses 'Japanese' / 'Chinese'
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
    """Build a PriceCharting search string from the card fields.

    e.g. set='Pokemon Pal En-Paldea Evolved', char='Skeledirge Ex', num='258'
        -> 'pokemon paldea evolved skeledirge ex 258'
    Strips series headings (Sword & Shield...), language/edition codes, and
    'PSA 10 Gem Mint 2023' noise. When drop_char=True, omits the character
    name and uses only set+number (a fallback when nothing is found).
    """
    parts: List[str] = []
    set_clean = _clean_set((card.get("set_name") or "").strip())
    char = (card.get("character_name") or "").strip()
    num_raw = str(card.get("card_number") or "").strip()
    num = num_raw.lstrip("0") or num_raw  # '046' -> '46', but don't turn '0' into ''

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


# pack_content only has one combined name string (e.g. "PSA 10 Gem Mint 2014
# Pokemon Japanese Xy Promo #XY-P Pitch's Pikachu-Holo") with no separate
# set/char/number fields; feeding it straight into build_query would collapse
# it down to just "pokemon". Here we first split it back into structured fields.
_GRADE_PREFIX_RE = re.compile(
    r"^\s*(?:PSA|BGS|CGC|SGC|ACE)\s*[\d.]+"
    r"(?:\s+(?:Gem\s*Mint|Gem\s*MT|Mint|Pristine|NM-?MT|Near\s*Mint|"
    r"Gold\s*Label|Black\s*Label|Perfect))*\s+",
    re.I,
)
_CARD_SUFFIX_RE = re.compile(
    r"[-\s]+(reverse\s*holo|holo(?:foil)?|full\s*art|alt(?:ernate)?\s*art|"
    r"secret\s*rare|special\s*illustration(?:\s*rare)?|character\s*rare|"
    r"trainer\s*gallery)\b.*$",
    re.I,
)


def parse_card_name(name: str) -> Dict[str, str]:
    """Split the combined pack_content name into the structured fields build_query needs.

    'PSA 10 Gem Mint 2014 Pokemon Japanese Xy Promo #XY-P Pitch's Pikachu-Holo'
      -> set_name='Japanese Xy Promo', character_name="Pitch's Pikachu",
         card_number='XY-P', year='2014'
    When a part can't be parsed, its field is left as an empty string
    (build_query can still degrade gracefully).
    """
    s = (name or "").strip()
    s = _GRADE_PREFIX_RE.sub("", s)                       # strip the grade prefix
    year = ""
    ym = re.match(r"(\d{4})\s+", s)
    if ym:
        year = ym.group(1)
        s = s[ym.end():]
    s = re.sub(r"^pokemon\s+", "", s, flags=re.I)         # strip 'Pokemon'
    lang = ""
    lm = re.match(r"(japanese|chinese|english)\s+", s, re.I)
    if lm:
        lang = lm.group(1).title()
        s = s[lm.end():]
    number = ""
    set_part = s
    char_part = ""
    nm = re.search(r"#(\S+)", s)
    if nm:
        number = nm.group(1)
        set_part = s[:nm.start()].strip()
        char_part = s[nm.end():].strip()
    char_part = _CARD_SUFFIX_RE.sub("", char_part).strip(" -")
    set_name = (lang + " " + set_part).strip()
    return {
        "year": year,
        "set_name": set_name,
        "character_name": char_part,
        "card_number": number,
    }


def _title_matches_card(matched_title: str, character_name: str) -> bool:
    """Sanity check: confirm the matched PriceCharting product title contains
    the card's main character name, to block "same number, different card"
    false matches (e.g. searching Blastoise #61 but matching Regirock #61).

    At least one of the character name's main tokens (words of length >= 3)
    must appear in the title. When the character name is empty (a few purely
    promotional cards), this isn't enforced and set+number decides.
    """
    if not character_name:
        return True
    if not matched_title:
        return False
    title_l = matched_title.lower()
    tokens = [t for t in re.findall(r"[a-z0-9]+", character_name.lower()) if len(t) >= 3]
    if not tokens:
        return True
    return any(t in title_l for t in tokens)


class OurPriceChecker:
    """Independent external price check (PriceCharting as the primary source)."""

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
        """Search and parse a PriceCharting product page; return per-grade prices + volume + source URL."""
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
        # Multi-result search page (no redirect to a product page) -> treat as no exact match
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

        # Volume (third row td: 'volume: N sales per ...')
        title_el = soup.find("title")
        return {
            "grades": grades,
            "matched_title": title_el.get_text(strip=True) if title_el else None,
            "source_url": r.url,
            "source": "pricecharting",
        }

    def get_independent_price(self, card: Dict, force: bool = False) -> dict:
        """Get a single card's independent external price (without touching Renaiss price sources).

        Returns:
          our_price        external sale price for the card's grade (None if not found)
          grade_matched    the grade column actually matched
          all_grades       prices for all grades (transparency; the dashboard can expand)
          source / source_url / matched_title
          sources          1=price found, 0=unknown
          query / checked_at
        """
        # A pack_content card only has the combined name string; when the
        # set/char/number fields are missing, parse it first, otherwise
        # build_query collapses the whole string down to just "pokemon"
        # (which never matches).
        if card.get("name") and not (card.get("set_name") or card.get("character_name")):
            card = {**card, **parse_card_name(card.get("name", ""))}

        token_id = str(card.get("token_id") or card.get("card_id") or "")
        cache_key = token_id or build_query(card)

        if not force and cache_key in self.cache:
            cached = self.cache[cache_key]
            if time.time() - cached.get("checked_at", 0) < CACHE_TTL:
                return cached

        query = build_query(card)
        grade_label = _grade_label(card.get("grader", ""), card.get("grade", ""))
        character_name = str(card.get("character_name") or "")
        queries = [query]
        alt = build_query(card, drop_char=True)  # fallback: use only set+number
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
            time.sleep(self.throttle)  # be respectful to the remote site
            if pc and pc.get("grades"):
                result["query"] = q
                break

        if pc and pc.get("grades"):
            result["all_grades"] = pc["grades"]
            result["source"] = pc["source"]
            result["source_url"] = pc["source_url"]
            result["matched_title"] = pc["matched_title"]
            # Sanity check: the title must contain the card's main character
            # name, otherwise treat it as a "same number, different card" false
            # match. Better to return None (sources=0) than give a wrong price
            # (which would pollute the luck metric).
            if not _title_matches_card(pc.get("matched_title", ""), character_name):
                result["title_mismatch"] = pc.get("matched_title")
                self.cache[cache_key] = result
                return result
            chosen = None
            if grade_label and grade_label in pc["grades"]:
                chosen = grade_label
            elif grade_label in ("PSA 10", None) and "PSA 10" in pc["grades"]:
                # Only fall back to PSA 10 when we wanted the top price anyway
                # or the grade is unknown; for lower-grade cards (Grade 9/8),
                # if that column is missing return None -- don't apply the
                # PSA 10 price and inflate the luck metric.
                chosen = "PSA 10"
            if chosen:
                result["grade_matched"] = chosen
                result["our_price"] = pc["grades"][chosen]["price"]
                result["sources"] = 1

        self.cache[cache_key] = result
        return result


def compare_card(card: Dict, our: dict) -> dict:
    """Assemble a comparison record from the Renaiss internal price and our external price.

    Internal price = renaiss FMV (fair market value); the SELL listing price
    ask_price is also recorded.
    External price = our_price (PriceCharting same-grade sales, PSA baseline).

    If the card's grader isn't PSA, use grader_convert() to convert the PSA
    column price to that grader's equivalent market price, then compare against
    the Renaiss FMV.

    delta_pct = (converted external price - internal) / internal; a positive
    value means the Renaiss FMV "undervalues" the card.
    """
    fmv = card.get("fmv")
    ask = card.get("ask_price")
    ext_raw = our.get("our_price")  # raw PriceCharting price (PSA baseline)

    card_grader = (card.get("grader") or "").strip()
    card_grade_str = (card.get("grade") or "")
    grade_matched = our.get("grade_matched")

    # Conversion: if the grader isn't PSA, discount the PSA price
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
