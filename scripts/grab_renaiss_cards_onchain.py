#!/usr/bin/env python3
"""抓取 Renaiss gacha 合約事件，反向推導所有卡資訊。

⚠️ 此鏈上軌道為「選用/實驗性」：合約 ABI 為推測，且公開 BSC RPC 常逾時。
主要資料來源請用 sync_renaiss_marketplace.py（tRPC）與 sync_open_monitor.py。
本腳本失敗時會優雅退出（return），不會中斷整體流程。
"""
from web3 import Web3
import json
import os

# Renaiss 合約地址（從官網源碼找到）
RENAISS_CONTRACT = "0x4D7b5dE3188323f44a741C712336c8549C9f9F26"
# 多個公開 BSC RPC 後援：任一可用即繼續，全部逾時才放棄。
BNB_RPCS = [
    os.getenv("BNB_RPC", "").strip(),
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed1.defibit.io",
    "https://rpc.ankr.com/bsc",
    "https://bscrpc.com",
]


def _connect():
    for rpc in [r for r in BNB_RPCS if r]:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                print(f"✅ 連線 BNB RPC 成功 ({rpc}), block: {w3.eth.block_number}")
                return w3
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  RPC {rpc} 失敗: {type(e).__name__}: {e}")
    return None


def main() -> int:
    w3 = _connect()
    if w3 is None:
        print("❌ 所有 BNB RPC 都無法連線；跳過鏈上軌道（請改用 marketplace/open-monitor 同步）。")
        return 1
    return _scrape(w3)


# 合約 ABI（從 Renaiss 官網找到的 gacha 函數，屬推測，可能與實際合約不符）
ABI = [
    {
        "constant": False,
        "inputs": [{"name": "_cardIndex", "type": "uint256"}],
        "name": "gacha",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "user", "type": "address"},
            {"indexed": False, "name": "cardId", "type": "uint256"},
            {"indexed": False, "name": "rarity", "type": "uint8"}
        ],
        "name": "CardDrawn",
        "type": "event"
    },
    {
        "constant": True,
        "inputs": [{"name": "", "type": "uint256"}],
        "name": "cards",
        "outputs": [
            {"name": "id", "type": "uint256"},
            {"name": "name", "type": "string"},
            {"name": "rarity", "type": "uint8"},
            {"name": "price", "type": "uint256"}
        ],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "totalCards",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    }
]


def _scrape(w3) -> int:
    contract = w3.eth.contract(address=RENAISS_CONTRACT, abi=ABI)

    # 抓總卡數
    try:
        total = contract.functions.totalCards().call()
        print(f"✅ 總卡數: {total}")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ totalCards() 失敗: {e}")
        total = 100  # 預設抓 100 張

    # 抓所有卡資訊
    cards = []
    for i in range(total):
        try:
            card = contract.functions.cards(i).call()
            card_id, name, rarity, price = card
            cards.append({
                "id": int(card_id),
                "name": name,
                "rarity": int(rarity),
                "price": int(price) / 1e18  # BNB 單位轉 ETH
            })
        except Exception as e:  # noqa: BLE001
            print(f"❌ card {i} 抓取失敗: {e}")

    print(f"✅ 抓到 {len(cards)} 張卡")
    if not cards:
        print("⚠️  鏈上未取得任何卡（ABI 可能不符或合約無此介面）；不覆寫資料。")
        return 1

    # 存檔
    os.makedirs("data", exist_ok=True)
    with open("data/renaiss_cards_onchain.json", "w", encoding="utf-8") as f:
        json.dump(cards, f, indent=2, ensure_ascii=False)
    print("✅ 已存 data/renaiss_cards_onchain.json")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
