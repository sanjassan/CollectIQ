#!/usr/bin/env python3
"""
卡機（pack）EV 計算 —— 把「官方 EV」與「我們自己算的 EV」清楚分開。

三個數字：
  1. official_ev   = Renaiss/平台公布的 current_platform_ev_usd（官方，不是我們算的）。
  2. empirical_ev  = 我們從觀察到的抽卡分佈算的平均 FMV（獨立重算，但仍用對方逐張 FMV）。
  3. our_ev        = empirical_ev × 外部校正係數。
                     校正係數 = comparison.json 裡 median(我們自抓外部價 / Renaiss FMV)，
                     代表「Renaiss FMV 相對真實外部市場平均偏離多少」。
                     <1 表示 Renaiss FMV 整體偏高（灌水），我們把 EV 往下修；反之往上。

這樣 our_ev 才是真正「不靠 Renaiss 價格來源、用我們自抓外部市場價校正過」的期望值。
"""
from __future__ import annotations

import json
import os
import statistics
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")


def external_correction_factor(comparison_path: Optional[str] = None) -> dict:
    """從 comparison.json 算外部校正係數 median(our_price / renaiss_fmv)。

    回傳 {factor, sample_n, source}；資料不足時 factor=1.0（不調整）。
    """
    path = comparison_path or os.path.join(DATA, "comparison.json")
    if not os.path.exists(path):
        return {"factor": 1.0, "sample_n": 0, "source": "none (comparison.json 不存在)"}
    with open(path, encoding="utf-8") as f:
        rows = json.load(f).get("rows", [])
    ratios = [
        r["our_price"] / r["renaiss_fmv"]
        for r in rows
        if r.get("our_price") and r.get("renaiss_fmv") and r["renaiss_fmv"] > 0
    ]
    if len(ratios) < 5:
        return {"factor": 1.0, "sample_n": len(ratios), "source": "樣本不足，未校正"}
    return {
        "factor": round(statistics.median(ratios), 4),
        "sample_n": len(ratios),
        "source": "median(our_price / renaiss_fmv) from comparison.json",
    }


def analyze_packs(pack_data: List[Dict], correction: Optional[dict] = None) -> dict:
    """對每個 pack 算 official / empirical / our EV。"""
    corr = correction or external_correction_factor()
    cf = corr["factor"]

    results = []
    for p in pack_data:
        official = p.get("platform_ev_usd")
        empirical = p.get("empirical_ev_usd")
        our = round(empirical * cf, 2) if empirical is not None else None

        # 與官方的落差（用我們校正後的 EV 對比官方公布值）
        delta_vs_official = None
        if our is not None and official:
            delta_vs_official = round((our - official) / official * 100, 1)

        results.append({
            "pack_id": p.get("pack_id"),
            "name": p.get("name"),
            "is_limited": p.get("is_limited"),
            "is_sold_out": p.get("is_sold_out"),
            "remaining_cards": p.get("remaining_cards"),
            "official_ev_usd": official,
            "empirical_ev_usd": empirical,
            "empirical_median_usd": p.get("empirical_median_usd"),
            "our_ev_usd": our,
            "delta_vs_official_pct": delta_vs_official,
            "pull_sample_n": p.get("pull_sample_n"),
            "pulls_last_30min": p.get("pulls_last_30min"),
            "last_s_card_name": p.get("last_s_card_name"),
        })

    # 排序：有官方 EV 的（在售卡機）優先，EV 高到低
    results.sort(key=lambda r: (r["official_ev_usd"] is None, -(r["official_ev_usd"] or 0)))
    return {
        "correction": corr,
        "packs": results,
    }


def load_and_analyze(pack_data_path: Optional[str] = None) -> dict:
    path = pack_data_path or os.path.join(DATA, "pack_data.json")
    pack_data = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                pack_data = json.load(f)
            except json.JSONDecodeError:
                pack_data = []
    return analyze_packs(pack_data)


if __name__ == "__main__":
    out = load_and_analyze()
    print("校正係數:", out["correction"])
    print(f"{'pack':<18}{'官方EV':>9}{'經驗EV':>9}{'自算EV':>9}{'vs官方':>9}  限量")
    for p in out["packs"]:
        print(f"{p['pack_id']:<18}"
              f"{str(p['official_ev_usd']):>9}"
              f"{str(p['empirical_ev_usd']):>9}"
              f"{str(p['our_ev_usd']):>9}"
              f"{(str(p['delta_vs_official_pct'])+'%') if p['delta_vs_official_pct'] is not None else '—':>9}"
              f"  {'LIM' if p['is_limited'] else ''}{' SOLD' if p['is_sold_out'] else ''}")
