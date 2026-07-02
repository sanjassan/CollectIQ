#!/usr/bin/env python3
"""
Test External Price Checker
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from external_price import ExternalPriceChecker

def test_price_checker():
    checker = ExternalPriceChecker()
    
    # Test with sample cards
    cards = [
        {"card_id": "TEST001", "name": "Legendary Eagle"},
        {"card_id": "TEST002", "name": "Mystic Phoenix"},
    ]
    
    print("Getting market prices...")
    results = checker.batch_get_prices(cards)
    
    for r in results:
        print(f"\n{r['name']}:")
        print(f"  Market Price: ${r['market_price']}")
        print(f"  Sources: {r.get('sources', 'N/A')}")

if __name__ == "__main__":
    test_price_checker()
