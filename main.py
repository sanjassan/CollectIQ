#!/usr/bin/env python3
"""
Easy EV Monitor v2 - Complete System
整合: EV 計算、Pack 監控、外部比價 (snkr/pricechart)
無 Renaiss API 依賴，使用本地 JSON + 外部比價
"""

import json
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import requests
from dotenv import load_dotenv

from external_price import ExternalPriceChecker


# Load environment variables
load_dotenv()


class LocalPoolDataLoader:
    """Load pool data from local JSON file"""

    def __init__(self, pool_data_path: str = "data/pool_data.json"):
        self.pool_data_path = pool_data_path

    def load_pool_data(self) -> List[Dict]:
        """Load and parse pool data from JSON file"""
        if not os.path.exists(self.pool_data_path):
            return []

        with open(self.pool_data_path, "r") as f:
            try:
                data = json.load(f)
                return data if isinstance(data, list) else [data]
            except json.JSONDecodeError:
                return []

    def save_pool_data(self, data: List[Dict]) -> bool:
        """Save pool data to local JSON file"""
        try:
            os.makedirs(os.path.dirname(self.pool_data_path), exist_ok=True)
            with open(self.pool_data_path, "w") as f:
                json.dump(data, f, indent=2)
            return True
        except (IOError, OSError):
            return False


def display_card_info(card: Dict) -> None:
    """Display card information with price source info"""
    image_url = card.get("image_url")
    market_price = card.get("market_price", 0)
    source_count = card.get("sources", 0)

    print(f"[CARD] {card['name']} (ID: {card.get('card_id', 'N/A')})")
    if image_url:
        print(f"        [IMAGE] {image_url}")
    # Bug-2 safety: market_price may be None (price unknown); don't format None.
    if market_price is None:
        print(f"        [MARKET PRICE] unknown (from {source_count} sources)")
    else:
        print(f"        [MARKET PRICE] ${market_price:.2f} (from {source_count} sources)")


class EVCalculator:
    """Calculate Expected Value (EV) for card pool"""

    def __init__(self, pool_data: List[Dict]):
        self.pool_data = pool_data

    def calculate_single_card_ev(
        self, card: Dict, total_remaining: Optional[int] = None
    ) -> Dict:
        """
        Calculate EV for a single card
        EV = P(draw) × Market Price
        """
        card_id = card.get("card_id", "unknown")
        name = card.get("name", "Unknown Card")
        remaining_qty = card.get("remaining_quantity", 0)
        market_price = card.get("market_price", 0.0)

        if total_remaining is None:
            total_remaining = sum(c.get("remaining_quantity", 0) for c in self.pool_data)

        # Bug-2 safety: a None market_price means "price unknown" (no real
        # external/FMV source). Never fabricate or compute EV from it -- mark
        # the item as price_unknown so it can't be flagged as a "good deal".
        if market_price is None:
            return {
                "card_id": card_id,
                "name": name,
                "ev": 0.0,
                "probability": round(remaining_qty / total_remaining, 4) if total_remaining > 0 else 0.0,
                "remaining": remaining_qty,
                "market_price": None,
                "status": "price_unknown",
            }

        if total_remaining <= 0:
            return {
                "card_id": card_id,
                "name": name,
                "ev": 0.0,
                "probability": 0.0,
                "remaining": remaining_qty,
                "market_price": market_price,
                "status": "invalid_data",
            }

        probability = remaining_qty / total_remaining
        ev = probability * market_price

        return {
            "card_id": card_id,
            "name": name,
            "ev": round(ev, 4),
            "probability": round(probability, 4),
            "remaining": remaining_qty,
            "market_price": market_price,
            "status": "calculated",
        }

    def calculate_all_ev(self) -> pd.DataFrame:
        """Calculate EV for all cards in pool"""
        total_remaining = sum(c.get("remaining_quantity", 0) for c in self.pool_data)

        results = []
        for card in self.pool_data:
            result = self.calculate_single_card_ev(card, total_remaining)
            results.append(result)

        df = pd.DataFrame(results)
        df = df.sort_values("ev", ascending=False).reset_index(drop=True)
        return df


class ExternalPriceOptimizer:
    """Optimize pool data with external price data"""

    def __init__(self):
        self.price_checker = ExternalPriceChecker()

    def optimize_pool_prices(self, pool_data: List[Dict]) -> List[Dict]:
        """
        Update pool data with external market prices
        """
        print("[INFO] Fetching external market prices...")
        optimized_data = self.price_checker.batch_get_prices(pool_data)
        print("[INFO] External price update complete!")
        return optimized_data


class TelegramAlert:
    """Send Telegram notifications for EV alerts"""

    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.download_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "image_cache"
        )
        os.makedirs(self.download_dir, exist_ok=True)

    def is_configured(self) -> bool:
        """Check if Telegram is properly configured"""
        return bool(self.bot_token and self.chat_id)

    def download_image(self, image_url: str) -> Optional[str]:
        """Download image from URL"""
        try:
            response = requests.get(image_url, timeout=30)
            response.raise_for_status()

            import hashlib
            url_hash = hashlib.md5(image_url.encode()).hexdigest()[:12]
            filename = f"card_{url_hash}.jpg"
            filepath = os.path.join(self.download_dir, filename)

            with open(filepath, "wb") as f:
                f.write(response.content)

            return filepath
        except (requests.RequestException, OSError):
            return None

    def send_alert(self, message: str, image_path: Optional[str] = None) -> bool:
        """Send alert message to Telegram"""
        if not self.is_configured():
            print("[INFO] Telegram not configured, alert sent to console only")
            return False

        if image_path and os.path.exists(image_path):
            url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
            with open(image_path, "rb") as photo:
                files = {"photo": photo}
                payload = {"chat_id": self.chat_id, "caption": message, "parse_mode": "Markdown"}
                try:
                    response = requests.post(url, files=files, data=payload, timeout=30)
                    return response.status_code == 200
                except requests.RequestException:
                    pass

        # Fallback to text
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}

        try:
            response = requests.post(url, json=payload, timeout=10)
            return response.status_code == 200
        except requests.RequestException:
            return False


class RenaissEVMonitor:
    """Main monitor class - Easy EV Monitor v2"""

    def __init__(
        self,
        pool_data_path: str = "data/pool_data.json",
        check_interval: int = 300,
        use_external_prices: bool = True,
    ):
        self.pool_data_path = pool_data_path
        self.check_interval = check_interval
        self.use_external_prices = use_external_prices
        self.loader = LocalPoolDataLoader(pool_data_path)
        self.telegram = TelegramAlert()
        self.price_optimizer = ExternalPriceOptimizer()

    def _load_and_optimize_pool(self) -> List[Dict]:
        """Load pool data and optimize with external prices"""
        pool_data = self.loader.load_pool_data()

        if not pool_data:
            print("[INFO] No pool data found. Using mock data.")
            pool_data = self._get_mock_data()

        # Apply external price optimization
        if self.use_external_prices:
            pool_data = self.price_optimizer.optimize_pool_prices(pool_data)

        return pool_data

    def _get_mock_data(self) -> List[Dict]:
        """Return mock data if no pool data available"""
        return [
            {
                "card_id": "E001",
                "name": "Legendary Eagle",
                "remaining_quantity": 5,
                "market_price": 1500.00,
                "image_url": "https://example.com/images/legendary_eagle.png",
            },
            {
                "card_id": "E002",
                "name": "Mystic Phoenix",
                "remaining_quantity": 15,
                "market_price": 500.00,
                "image_url": "https://example.com/images/mystic_phoenix.png",
            },
            {
                "card_id": "E003",
                "name": "Shadow Wolf",
                "remaining_quantity": 50,
                "market_price": 100.00,
                "image_url": "https://example.com/images/shadow_wolf.png",
            },
        ]

    def run_once(self) -> None:
        """Run monitoring cycle once"""
        pool_data = self._load_and_optimize_pool()

        calculator = EVCalculator(pool_data)
        ev_df = calculator.calculate_all_ev()

        print("\n" + "=" * 70)
        print("RENAISS EV MONITOR v2 - REPORT")
        print(f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"External Price Integration: {'Enabled' if self.use_external_prices else 'Disabled'}")
        print("=" * 70)
        print(ev_df.to_string(index=False))
        print("=" * 70)

        print("\n[CARDS WITH MARKET INFORMATION]")
        for card in pool_data:
            display_card_info(card)

        # Check for high-value opportunities
        high_ev_cards = ev_df[ev_df["ev"] > 50]
        if not high_ev_cards.empty:
            alert_message = self._format_alert_message(high_ev_cards)
            print(f"\n[ALERT] High EV opportunities detected!")

            highest_card = high_ev_cards.iloc[0]
            image_path = self._find_and_download_card_image(pool_data, highest_card)

            self.telegram.send_alert(alert_message, image_path=image_path)

    def _format_alert_message(self, high_ev_df: pd.DataFrame) -> str:
        """Format alert message for Telegram"""
        lines = ["*🚀 HIGH EV OPPORTUNITIES DETECTED*"]
        for _, row in high_ev_df.iterrows():
            line = (
                f"*{row['name']}* (ID: {row['card_id']})\n"
                f"EV: ${row['ev']:.2f} | P(draw): {row['probability']*100:.1f}%\n"
                f"Remaining: {row['remaining']} | Price: ${row['market_price']:.2f}\n"
                f"Market Check: Enabled"
            )
            lines.append(line)
        return "\n\n".join(lines)

    def _find_and_download_card_image(self, pool_data: list, card_row: pd.Series) -> Optional[str]:
        """Find card in pool data and download its image"""
        for card in pool_data:
            if card.get("card_id") == card_row.get("card_id"):
                image_url = card.get("image_url")
                if image_url:
                    return self.telegram.download_image(image_url)
        return None

    def run(self) -> None:
        """Continuous monitoring loop with crash-resistant backoff.

        A single API timeout or parse error in run_once() must never kill the
        monitor. Each cycle is wrapped; on failure we log and apply exponential
        backoff (capped) before retrying, then reset the backoff on success.
        """
        print("[INFO] Starting RENAISS EV MONITOR v2...")
        print(f"[INFO] Data source: Local JSON files")
        print(f"[INFO] External price integration: {'Enabled' if self.use_external_prices else 'Disabled'}")
        print(f"[INFO] Check interval: {self.check_interval} seconds")

        backoff = self.check_interval
        max_backoff = max(self.check_interval * 8, 3600)
        while True:
            try:
                self.run_once()
                backoff = self.check_interval  # reset after a healthy cycle
                wait = self.check_interval
                print(f"\n[INFO] Waiting {wait} seconds before next check...")
            except KeyboardInterrupt:
                print("\n[INFO] Interrupted by user, shutting down.")
                break
            except Exception as exc:  # noqa: BLE001 - monitor must survive any cycle error
                wait = backoff
                print(f"\n[ERROR] Monitoring cycle failed: {type(exc).__name__}: {exc}")
                print(f"[INFO] Backing off {wait} seconds before retry...")
                backoff = min(backoff * 2, max_backoff)
            time.sleep(wait)


if __name__ == "__main__":
    import sys

    # Parse command line arguments
    use_external_prices = "--no-external" not in sys.argv
    once_only = "--once" in sys.argv

    monitor = RenaissEVMonitor(
        use_external_prices=use_external_prices
    )

    if once_only:
        monitor.run_once()
    else:
        monitor.run()
