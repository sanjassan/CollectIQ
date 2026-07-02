#!/usr/bin/env python3
"""儲存 RenaCrypt 卡資料到 SQLite"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "renaiss_cards.db"


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
            rarity TEXT CHECK(rarity IN ('Legendary', 'Epic', 'Rare', 'Uncommon', 'Common')),
            image_url TEXT,
            screenshot_path TEXT,
            acquired_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def load_json(json_path):
    """從 JSON 檔案載入卡資料"""
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_to_db(conn, cards):
    """儲存卡到 SQLite"""
    cursor = conn.cursor()
    for card in cards:
        cursor.execute("""
            INSERT OR REPLACE INTO cards (id, name, psa_bgs_id, price, rarity, image_url, screenshot_path, acquired_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            card["id"],
            card["name"],
            card.get("psa_bgs_id"),
            card.get("price"),
            card.get("rarity"),
            card.get("image_url"),
            card.get("screenshot_path"),
            card.get("acquired_at", datetime.now().isoformat()),
        ))
    conn.commit()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python save_renaiss_cards.py <json_file>")
        sys.exit(1)

    json_path = sys.argv[1]
    if not Path(json_path).exists():
        print(f"檔案不存在: {json_path}")
        sys.exit(1)

    # 載入 JSON
    cards = load_json(json_path)
    print(f"載入 {len(cards)} 張卡")

    # 儲存到 SQLite
    conn = init_db()
    save_to_db(conn, cards)
    conn.close()
    print(f"已儲存到 SQLite: {DB_PATH}")
