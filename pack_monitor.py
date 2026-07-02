#!/usr/bin/env python3
"""
Pack Pool Monitor v2 - Updated
Analyzes pack pool remaining cards and calculates EV
External API independent
"""

import json
import os
import time
from datetime import datetime
from typing import Dict, List

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
PACK_DATA_PATH = os.path.join(DATA_DIR, "data", "pack_data.json")

# Pack Data - No external API dependency
MOCK_PACKS = [
    {
        "pack_id": "omega",
        "name": "OMEGA Pack",
        "price": 48.00,
        "top_prize": 1100.00,
        "pack_type": "Infinite",
        "total_cards": 10000,
        "remaining_cards": 9500,
        "pack_images": {
            "background": "https://example.com/bg.webp",
            "pack": "https://example.com/pack.gif"
        },
        "tier_probabilities": {
            "S": 0.01,
            "A": 0.04,
            "B": 0.11,
            "C": 0.84
        },
        "cards": [
            {"card_id": "S-001", "name": "Mew Ex (Special Art)", "fmv": 772.00, "tier": "S"},
            {"card_id": "A-001", "name": "Zekrom Ex (Special Art)", "fmv": 310.00, "tier": "B"},
            {"card_id": "C-001", "name": "Rocket's Mewtwo Ex", "fmv": 290.00, "tier": "C"},
        ]
    },
    {
        "pack_id": "renacrypt",
        "name": "RenaCrypt Pack",
        "price": 88.00,
        "top_prize": 2415.00,
        "pack_type": "Infinite",
        "total_cards": 8000,
        "remaining_cards": 7200,
        "pack_images": {
            "background": "https://example.com/renacrypt_bg.jpg",
            "pack": "https://example.com/renacrypt_pack.gif"
        },
        "tier_probabilities": {
            "S": 0.01,
            "A": 0.04,
            "B": 0.11,
            "C": 0.84
        },
        "cards": [
            {"card_id": "RC-S-001", "name": "Charizard VMAX", "fmv": 1800.00, "tier": "S"},
            {"card_id": "RC-B-001", "name": "Blastoise", "fmv": 280.00, "tier": "B"},
            {"card_id": "RC-C-001", "name": "Rattata", "fmv": 35.00, "tier": "C"},
        ]
    }
]


class PackPoolAnalyzer:
    """Analyze pack pool remaining cards and calculate EV"""

    def __init__(self, packs: List[Dict]):
        self.packs = packs

    def analyze_pack(self, pack: Dict) -> Dict:
        """Analyze a single pack.

        Robust to two schemas:
          - MOCK_PACKS / local fixtures: price, top_prize, pack_type, total_cards, cards[]
          - open-monitor sync (pack_data.json): pack_id, name, remaining_cards,
            total_cards (often None), platform_ev_usd, is_sold_out
        All field access is defensive; None totals never raise.
        """
        # Coerce possibly-missing / None numeric fields to safe numbers.
        total_cards = pack.get('total_cards') or 0
        remaining_cards = pack.get('remaining_cards') or 0
        price = pack.get('price') or 0
        top_prize = pack.get('top_prize') or 0

        remaining_pct = (remaining_cards / total_cards * 100) if total_cards > 0 else 0

        base_top_prize_prob = 0.05
        remaining_factor = remaining_pct / 100
        top_prize_remaining_prob = base_top_prize_prob * (1 + remaining_factor)

        optimized_ev = self._calculate_optimized_ev(pack)
        pack_type = pack.get('pack_type') or ('SoldOut' if pack.get('is_sold_out') else 'Pack')

        return {
            "pack_id": pack.get('pack_id', ''),
            "pack_name": pack.get('name') or pack.get('pack_id', 'Unknown'),
            "pack_type": pack_type,
            "price": price,
            "top_prize": top_prize,
            "total_cards": total_cards,
            "remaining_cards": remaining_cards,
            "remaining_percent": round(remaining_pct, 2),
            "drawn_cards": max(total_cards - remaining_cards, 0),
            "drawn_percent": round(100 - remaining_pct, 2),
            "top_prize_remaining_prob": round(top_prize_remaining_prob * 100, 4),
            "cards_count": len(pack.get('cards', []) or []),
            "tier_distribution": self._get_tier_distribution(pack.get('cards', []) or []),
            "optimized_ev": round(optimized_ev, 2),
            "platform_ev_usd": pack.get('platform_ev_usd'),
            "ev_improvement": round((optimized_ev - price) / price * 100, 2) if price > 0 else 0,
            "timestamp": datetime.now().isoformat()
        }

    def _calculate_optimized_ev(self, pack: Dict) -> float:
        """Calculate optimized EV considering pack remaining rate.

        Prefers explicit card FMVs; falls back to the platform-published EV
        (open-monitor's platform_ev_usd) before guessing from top_prize/price.
        """
        cards = pack.get('cards', []) or []
        total_cards = pack.get('total_cards') or 0
        remaining_cards = pack.get('remaining_cards') or 0

        if cards:
            total_fmv = sum((card.get('fmv') or 0) for card in cards)
            avg_card_value = total_fmv / len(cards)
            remaining_pct = (remaining_cards / total_cards) if total_cards > 0 else 0
            return avg_card_value * remaining_pct * 10

        # open-monitor packs: trust the platform-published EV when present.
        platform_ev = pack.get('platform_ev_usd')
        if platform_ev:
            return float(platform_ev)

        top_prize = pack.get('top_prize') or 0
        avg_card_value = top_prize / 10 if top_prize > 0 else (pack.get('price') or 0)
        remaining_pct = (remaining_cards / total_cards) if total_cards > 0 else 0
        return avg_card_value * remaining_pct * 10

    def _get_tier_distribution(self, cards: List[Dict]) -> Dict[str, int]:
        """Get tier distribution from cards"""
        distribution = {}
        for card in cards:
            tier = card.get('tier', 'Unknown')
            distribution[tier] = distribution.get(tier, 0) + 1
        return distribution

    def analyze_all(self) -> Dict:
        """Analyze all packs"""
        results = []
        for pack in self.packs:
            results.append(self.analyze_pack(pack))

        return {
            "analysis_timestamp": datetime.now().isoformat(),
            "total_packs": len(self.packs),
            "results": results
        }


class PackPoolMonitor:
    """Monitor pack pool changes"""

    def __init__(self, packs_path: str = PACK_DATA_PATH):
        self.packs_path = packs_path
        self.packs = self._load_packs()
        self.analyzer = PackPoolAnalyzer(self.packs)

    def _load_packs(self) -> List[Dict]:
        """Load packs from JSON file or use mock data"""
        if os.path.exists(self.packs_path):
            try:
                with open(self.packs_path, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return MOCK_PACKS

    def format_alert_message(self, analysis: Dict) -> str:
        """Format Telegram alert message"""
        lines = ["*🚀 Renaiss Pack Pool Monitor v2*"]
        lines.append(f"_Last Updated: {analysis['analysis_timestamp'][:16]}_")
        lines.append("")

        for result in analysis['results']:
            lines.append(f"📦 [{result['pack_type']}] *{result['pack_name']}*")
            lines.append(f"  💰 Price: ${result['price']:.2f} | 🏆 Prize: ${result['top_prize']:.2f}")
            lines.append(f"  📊 Remaining: {result['remaining_percent']:.1f}% | 🔎 Top Prize Prob: {result['top_prize_remaining_prob']:.2f}%")
            lines.append(f"  📈 Optimized EV: ${result['optimized_ev']:.2f} | ⚡ Improvement: {result['ev_improvement']:.1f}%")
            lines.append(f"  🎫 Total Cards: {result['total_cards']:,} | ✅ Remaining: {result['remaining_cards']:,}")
            lines.append("")

        return "\n".join(lines)

    def run_once(self) -> Dict:
        """Run monitoring once"""
        analysis = self.analyzer.analyze_all()
        message = self.format_alert_message(analysis)

        print("\n" + "=" * 70)
        print("RENAISS PACK POOL MONITOR v2 REPORT")
        print("=" * 70)
        print(message)
        print("=" * 70)

        return analysis


def run_monitor():
    """Run continuous monitoring"""
    monitor = PackPoolMonitor()

    while True:
        analysis = monitor.run_once()
        print("\n[INFO] Next check in 5 minutes...")
        time.sleep(300)


if __name__ == "__main__":
    import sys

    monitor = PackPoolMonitor()

    if "--once" in sys.argv:
        analysis = monitor.run_once()
        output_path = os.path.join(DATA_DIR, "pack_analysis.json")
        with open(output_path, 'w') as f:
            json.dump(analysis, f, indent=2)
        print(f"\n✅ Analysis saved to: {output_path}")
    elif "--monitor" in sys.argv:
        run_monitor()
    else:
        monitor.run_once()
