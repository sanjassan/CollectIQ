#!/usr/bin/env python3
"""
Easy EV Monitor v2 - External Price Checker
整合 snkr、price chart 等外部網站進行比價
"""

import json
import os
import time
from datetime import datetime
from typing import Dict, List, Optional
import requests
from bs4 import BeautifulSoup


class ExternalPriceChecker:
    """
    從外部來源取得市場價格
    支援: snkr、price chart 等平台
    """

    def __init__(self):
        self.price_cache = {}
        self.cache_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "data", "price_cache.json"
        )
        self._load_cache()

    def _load_cache(self):
        """Load cached prices"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r") as f:
                    self.price_cache = json.load(f)
            except json.JSONDecodeError:
                self.price_cache = {}

    def _save_cache(self):
        """Save cache to file"""
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        with open(self.cache_file, "w") as f:
            json.dump(self.price_cache, f, indent=2)

    def get_market_price(self, card_name: str, card_id: str = None) -> Dict:
        """
        獲取卡片的市場價格（僅彙整「真實」外部來源的平均）。

        重要：若沒有任何外部來源回傳有效價格，回傳 market_price=None 且
        sources=0，明確標示「未知」，**不再捏造** $500/$200/$50 之類的假價格。
        由呼叫端決定要不要沿用既有 FMV（見 batch_get_prices）。
        """
        cache_key = card_id or card_name

        # Check cache first (1 hour)
        if cache_key in self.price_cache:
            cached = self.price_cache[cache_key]
            if time.time() - cached["timestamp"] < 3600:
                return cached

        # Fetch from external sources
        prices = []

        # Try snkr.com
        snkr_price = self._get_snkr_price(card_name, card_id)
        if snkr_price:
            prices.append(snkr_price)

        # Try price chart
        price_chart_price = self._get_price_chart_price(card_name, card_id)
        if price_chart_price:
            prices.append(price_chart_price)

        # No fabrication: if no real source returned a price, market_price is None.
        avg_price = round(sum(prices) / len(prices), 2) if prices else None

        # Update cache
        result = {
            "card_id": cache_key,
            "card_name": card_name,
            "market_price": avg_price,
            "sources": len(prices),
            "price_list": prices,
            "source_details": {
                "snkr": snkr_price,
                "pricechart": price_chart_price
            },
            "timestamp": int(time.time())
        }
        self.price_cache[cache_key] = result
        self._save_cache()

        return result

    def _get_snkr_price(self, card_name: str, card_id: str = None) -> Optional[float]:
        """
        Get price from snkr.com.

        The real snkr API/DOM schema is not known to this codebase (the URL
        below is only an example), so we MUST NOT fabricate a number. We only
        return a price when the response contains a valid, strictly-positive
        numeric "price". Any missing/zero/non-numeric value -> None (unknown)
        with a logged warning, so EV is never computed on a fake figure.
        """
        try:
            # Example API call (update with actual snkr API)
            api_url = f"https://www.snkr.com/api/search?q={requests.utils.quote(card_name)}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json"
            }
            response = requests.get(api_url, headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()
                # Pull a candidate price from the two known example shapes.
                # Use None (not 0) as the default so a missing field is treated
                # as "unknown" rather than a fabricated $0 price.
                raw_price = None
                if isinstance(data, dict):
                    items = data.get("items")
                    if isinstance(items, list) and items:
                        raw_price = items[0].get("price")
                    elif "price" in data:
                        raw_price = data.get("price")

                # Only accept a real, strictly-positive number.
                try:
                    if raw_price is not None:
                        price = float(raw_price)
                        if price > 0:
                            print(f"[snkr] Found {card_name}: ${price}")
                            return price
                except (TypeError, ValueError):
                    pass

            # No usable price -> honestly report unknown.
            print(f"[WARN] snkr returned no usable price for {card_name}; treating as unknown.")
        except Exception as e:
            print(f"[WARN] snkr API failed: {e}")

        return None

    def _get_price_chart_price(self, card_name: str, card_id: str = None) -> Optional[float]:
        """
        Get price from pricechart.com
        Uses web scraping
        """
        try:
            search_url = f"https://www.pricechart.com/search?q={requests.utils.quote(card_name)}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }

            response = requests.get(search_url, headers=headers, timeout=10)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")

                # Try to find price elements (adapt based on actual site structure)
                price_elements = soup.find_all(class_=["price", "current-price", "price-value"])

                for elem in price_elements:
                    price_text = elem.get_text().strip()
                    # Extract numeric price
                    import re
                    match = re.search(r'\$[\d,]+\.?\d*', price_text)
                    if match:
                        price = float(match.group().replace("$", "").replace(",", ""))
                        print(f"[pricechart] Found {card_name}: ${price}")
                        return price
        except Exception as e:
            print(f"[WARN] pricechart scraping failed: {e}")

        return None

    def _get_fallback_price(self, card_name: str) -> Optional[float]:
        """
        DEPRECATED keyword-based guess. Kept only for explicit opt-in
        debugging; it fabricates prices from card-name keywords and must NOT
        feed EV calculations. Returns None by default so callers treat the
        price as unknown rather than silently mis-pricing the card.
        """
        return None

    def batch_get_prices(self, cards: List[Dict]) -> List[Dict]:
        """
        Batch update prices for multiple cards.

        Only overrides a card's existing price when a *real* external source
        returns one. If external lookup yields nothing (sources == 0), the
        card keeps its original market_price (e.g. the Renaiss FMV from the
        marketplace/open-monitor sync) instead of being clobbered with a
        fabricated number.
        """
        results = []
        for card in cards:
            card_result = self.get_market_price(
                card.get("name", ""),
                card.get("card_id")
            )
            ext_price = card_result["market_price"]
            ext_sources = card_result["sources"]

            if ext_sources > 0 and ext_price is not None:
                market_price = ext_price
                sources = ext_sources
            else:
                # No real external data: preserve the card's own price/source.
                market_price = card.get("market_price", 0.0)
                sources = card.get("sources", 0)

            results.append({
                **card,
                "market_price": market_price,
                "sources": sources,
            })
            # Be respectful to APIs (0.5 second delay)
            time.sleep(0.5)

        return results


if __name__ == "__main__":
    # Test the price checker
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
        print(f"  Detail: {r.get('source_details', {})}")
