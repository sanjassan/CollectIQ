-- RenaCrypt 卡庫 SQLite schema
-- 資料庫: data/renaiss_cards.db

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
);

-- 建立索引
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name);
CREATE INDEX IF NOT EXISTS idx_cards_rarity ON cards(rarity);
CREATE INDEX IF NOT EXISTS idx_cards_price ON cards(price);
CREATE INDEX IF NOT EXISTS idx_cards_psa_bgs_id ON cards(psa_bgs_id);
