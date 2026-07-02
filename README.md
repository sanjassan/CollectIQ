# Renaiss EV Monitor v2 - README

完整的 EV 監控系統，支援外部比價功能。

## 功能特色

- ✅ **EV 計算** - 預期價值計算與優化
- ✅ **Pack 監控** - 卡機剩餘卡池分析
- ✅ **外部比價** - 整合 snkr、price chart 等外部網站
- ✅ **Telegram alerts** - 即時通知
- ✅ **Web Dashboard** - Flask 網頁介面

## 安裝

```bash
# 克隆專案
cd renaiss_ev_monitor_v2

# 安裝依賴
pip install -r requirements.txt

# 設定環境變數
cp .env.example .env
# 編輯 .env 檔案，填入你的 Telegram bot token

# 運行設定
python setup.py
```

## 使用

### CLI 模式 (終端機)

```bash
# 運行一次 (包含外部比價)
python main.py --once

# 運行一次 (不使用外部比價)
python main.py --once --no-external

# 持續監控 (每 5 分鐘檢查一次)
python main.py
```

### Pack 監控

```bash
# 運行一次
python pack_monitor.py --once

# 持續監控
python pack_monitor.py --monitor
```

### Web Dashboard

```bash
# 啟動 Flask 伺服器
python dashboard.py
```

然後開啟 http://localhost:5000

## API 端點

| 端點 | 方法 | 說明 |
|------|------|------|
| `/` | GET | 主頁面 |
| `/api/pool-data` | GET | 取得卡池資料 |
| `/api/ev-calculate` | GET | 計算 EV |
| `/api/external-price` | GET | 取得外部價格 (`?name=卡名`) |
| `/api/update-pool` | POST | 更新卡池資料 |
| `/api/pack-data` | GET | 取得卡機資料 |

## 設定

編輯 `.env` 檔案：

```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
CHECK_INTERVAL=300
HIGH_EV_THRESHOLD=50.0
```

## 檔案結構

```
renaiss_ev_monitor_v2/
├── main.py                  # 主程式 (EV + external price)
├── pack_monitor.py          # Pack 監控
├── dashboard.py            # Web Dashboard
├── external_price.py       # 外部比價模組
├── setup.py                # 安裝設定
├── requirements.txt        # Python 依賴
├── .env.example           # 環境變數範例
├── data/                   # 資料目錄
│   ├── pool_data.json     # 卡池資料
│   ├── pack_data.json     # 卡機資料
│   └── price_cache.json   # 價格快取
└── image_cache/           # 下載的圖片
```

## 外部比價

### snkr.com
- 預設 API: `https://www.snkr.com/api/search?q={card_name}`
- 需要根據實際 API 調整解析邏輯

### pricechart.com
- 使用 BeautifulSoup 抓取價格
- 自動過濾價格元素

### 回退機制
如果外部來源都失敗：
-Legendary/特殊卡 = $500
-稀有卡 = $200
-普通卡 = $50

## 注意事項

1. **外部 API 有次數限制** - 檔案中已加入 0.5 秒延遲
2. **價格快取** - 1 小時內相同卡片會使用快取價格
3. **尊重網站規則** - 請不要過度頻繁呼叫外部 API

##License

MIT License
