#!/usr/bin/env python3
"""
Build a "Renaiss internal price vs. our independently scraped external price" comparison table → data/comparison.json

- By default only processes marketplace_listed.json (cards Renaiss is actively SELLING, the most actionable).
- Resumable via our_price.py's cache; a rerun only fills in what hasn't been scraped or has expired.
- Throttles external sites so they aren't over-called.

Usage:
  python scripts/build_comparison.py            # process all listed cards (takes a while)
  python scripts/build_comparison.py --limit 30 # only the first 30 cards (for validation)
  python scripts/build_comparison.py --all       # process marketplace_all.json (4080 cards, use with care)
  python scripts/build_comparison.py --force      # ignore the cache and re-scrape
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"

from our_price import OurPriceChecker, compare_card  # noqa: E402


def _summarize(rows: list[dict]) -> dict:
    priced = [r for r in rows if r.get("our_price") is not None]
    flags: dict[str, int] = {}
    for r in rows:
        flags[r["flag"]] = flags.get(r["flag"], 0) + 1
    return {
        "total": len(rows),
        "with_external_price": len(priced),
        "flags": flags,
    }


def main() -> int:
    args = sys.argv[1:]
    use_all = "--all" in args
    force = "--force" in args
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])

    src = DATA / ("marketplace_all.json" if use_all else "marketplace_listed.json")
    if not src.exists():
        print(f"❌ 找不到來源 {src}")
        return 1
    cards = json.loads(src.read_text(encoding="utf-8"))
    if limit:
        cards = cards[:limit]

    chk = OurPriceChecker()
    rows: list[dict] = []
    out = DATA / "comparison.json"
    print(f"[{time.strftime('%F %T')}] 比價開始：{len(cards)} 張 (來源 {src.name})")
    for i, card in enumerate(cards, 1):
        our = chk.get_independent_price(card, force=force)
        rows.append(compare_card(card, our))
        if i % 10 == 0:
            chk.save_cache()
            # Persist incrementally so it can be viewed at any time
            out.write_text(json.dumps({
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source_file": src.name,
                "partial": i < len(cards),
                "summary": _summarize(rows),
                "rows": rows,
            }, indent=2, ensure_ascii=False))
            print(f"  …{i}/{len(cards)} (已比到 {our.get('our_price')} for {card.get('name','')[:40]})")

    chk.save_cache()
    summary = _summarize(rows)
    out.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_file": src.name,
        "partial": False,
        "summary": summary,
        "rows": rows,
    }, indent=2, ensure_ascii=False))
    print(f"✅ 完成 → {out}")
    print(f"   {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
