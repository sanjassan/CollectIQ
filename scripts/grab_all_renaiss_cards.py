#!/usr/bin/env python3
"""RenaCrypt 全卡抓取：自動登入 renaiss.xyz 抓取所有卡資訊"""
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# 設定
RENAISS_EMAIL = os.getenv("RENAISS_EMAIL", "kelly.renaiss.test@gmail.com")
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "renaiss_cards"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = OUTPUT_DIR / "renaiss_cards.db"
JSON_PATH = OUTPUT_DIR / "renaiss_cards.json"


def init_db():
    """初始化 SQLite 資料庫"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            psa_bgs_id TEXT UNIQUE,
            price REAL,
            rarity TEXT,
            image_url TEXT,
            screenshot_path TEXT,
            acquired_at TEXT
        )
    """)
    conn.commit()
    return conn


def save_to_db(conn, card):
    """儲存卡到 SQLite"""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO cards (id, name, psa_bgs_id, price, rarity, image_url, screenshot_path, acquired_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        card["id"],
        card["name"],
        card.get("psa_bgs_id"),
        card.get("price"),
        card.get("rarity"),
        card.get("image_url"),
        card.get("screenshot_path"),
        datetime.now().isoformat(),
    ))
    conn.commit()


def grab_renaiss_cards():
    """抓取 renaiss.xyz 的 RenaCrypt 卡列表"""
    print(f"開始抓取 RenaCrypt 卡列表...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # 非 headless 方便除錯
        context = browser.new_context()
        page = context.new_page()

        # 登入 renaiss.xyz
        print(f"開啟 renaiss.xyz 登入頁面...")
        page.goto("https://www.renaiss.xyz")
        page.wait_for_load_state("networkidle")

        # 點擊 Log in 按鈕
        print("點擊 Log in 按鈕...")
        page.wait_for_selector('button:has-text("Log in")', timeout=10000)
        page.click('button:has-text("Log in")')
        page.wait_for_load_state("networkidle", timeout=10000)

        # 等待登入視窗出現並輸入 Email
        print(f"輸入 Email: {RENAISS_EMAIL}")
        page.wait_for_selector('input[placeholder="abc@renaiss.xyz"]', timeout=10000)
        page.fill('input[placeholder="abc@renaiss.xyz"]', RENAISS_EMAIL)

        # 點擊 Send OTP
        print("點擊 Send OTP...")
        page.click('button:has-text("Send OTP")')
        page.wait_for_timeout(3000)  # 等待 OTP 訊息

        # 手動輸入 OTP（實際運行時需提示使用者輸入）
        print("請在 60 秒內手動輸入 OTP...")
        page.wait_for_timeout(60000)

        # 等待登入完成
        print("等待登入完成...")
        page.wait_for_load_state("networkidle", timeout=60000)

        # 跳轉到 RenaCrypt Pack 頁面
        print("跳轉到 RenaCrypt Pack 頁面...")
        page.goto("https://www.renaiss.xyz/gacha/renacrypt-pack")
        page.wait_for_load_state("networkidle", timeout=60000)

        # 等待卡列表載入
        print("等待卡列表載入...")
        page.wait_for_selector('.card-list, .card-item', timeout=30000)

        # 滾動頁面以載入全部卡
        print("滾動頁面以載入全部卡...")
        last_height = page.evaluate("document.body.scrollHeight")
        scroll_count = 0
        max_scrolls = 50
        while scroll_count < max_scrolls:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                print("已滾動至頁面底部")
                break
            last_height = new_height
            scroll_count += 1
            print(f"滾動次數: {scroll_count}")

        # 抓取所有卡
        cards = []
        card_elements = page.query_selector_all('.card-item, .card, .pokemon-card')

        print(f"找到 {len(card_elements)} 張卡，開始抓取資料...")

        for idx, card_elem in enumerate(card_elements, 1):
            try:
                # 抓取卡名
                name = card_elem.query_selector('.card-name, .name, h3') or card_elem.query_selector('span')
                card_name = name.inner_text().strip() if name else f"Card_{idx}"

                # 抓取 PSA/BGS ID（若有的話）
                psa_bgs_id_elem = card_elem.query_selector('.psa-id, .bgs-id, .certificate-id')
                psa_bgs_id = psa_bgs_id_elem.inner_text().strip() if psa_bgs_id_elem else ""

                # 抓取卡圖片 URL
                img_elem = card_elem.query_selector('img')
                img_url = img_elem.get_attribute('src') if img_elem else ""

                # 抓取價格（若有）
                price_elem = card_elem.query_selector('.card-price, .price, .value')
                price = float(price_elem.inner_text().replace('$', '').strip()) if price_elem else None

                # 抓取稀有度（若有）
                rarity_elem = card_elem.query_selector('.rarity, .tier')
                rarity = rarity_elem.inner_text().strip() if rarity_elem else "Unknown"

                # 儲存截圖
                screenshot_path = OUTPUT_DIR / f"card_{idx}.png"
                card_elem.screenshot(path=str(screenshot_path))

                # 儲存到資料結構
                card_id = f"rena_{idx:04d}"
                card = {
                    "id": card_id,
                    "name": card_name,
                    "psa_bgs_id": psa_bgs_id,
                    "price": price,
                    "rarity": rarity,
                    "image_url": img_url,
                    "screenshot_path": str(screenshot_path),
                }
                cards.append(card)
                print(f"[{idx}/{len(card_elements)}] {card_name} - {psa_bgs_id}")

            except Exception as e:
                print(f"抓取卡 {idx} 失敗: {e}")

        browser.close()

    # 儲存結果
    print(f"\n共抓取 {len(cards)} 張卡，儲存中...")

    # 儲存 JSON
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)
    print(f"JSON 已儲存至: {JSON_PATH}")

    # 儲存 SQLite
    conn = init_db()
    for card in cards:
        save_to_db(conn, card)
    conn.close()
    print(f"SQLite 已儲存至: {DB_PATH}")

    return cards


if __name__ == "__main__":
    try:
        cards = grab_renaiss_cards()
        print(f"\n完成！共抓取 {len(cards)} 張 RenaCrypt 卡。")
    except Exception as e:
        print(f"錯誤: {e}")
        raise
