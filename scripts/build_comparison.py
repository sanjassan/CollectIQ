#!/usr/bin/env python3
"""
建立「Renaiss 內部價 vs 我們自抓外部價」比較表 → data/comparison.json

- 預設只跑 marketplace_listed.json（Renaiss 正在 SELL 掛單的卡，最有行動價值）。
- 透過 our_price.py 的快取可續跑（resumable）；重跑只補沒抓過/過期的。
- 對外部站台節流（throttle），不過度呼叫。

用法：
  python scripts/build_comparison.py            # 跑全部在售卡（會花時間）
  python scripts/build_comparison.py --limit 30 # 只跑前 30 張（驗證用）
  python scripts/build_comparison.py --all       # 跑 marketplace_all.json（4080 張，慎用）
  python scripts/build_comparison.py --force      # 忽略快取重抓
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
            # 增量落地，隨時可看
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
