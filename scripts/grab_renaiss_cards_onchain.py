#!/usr/bin/env python3
"""Scrape Renaiss gacha contract events and reverse-derive all card info.

⚠️ This on-chain track is optional/experimental: the contract ABI is inferred, and public BSC RPCs
often time out. For primary data sources, use sync_renaiss_marketplace.py (tRPC) and sync_open_monitor.py.
On failure this script exits gracefully (return) without interrupting the overall pipeline.
"""
from web3 import Web3
import json
import os

# Renaiss contract address (found in the official site's source)
RENAISS_CONTRACT = "0x4D7b5dE3188323f44a741C712336c8549C9f9F26"
# Multiple public BSC RPC fallbacks: proceed on the first that works, give up only if all time out.
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


# Contract ABI (gacha functions found on the Renaiss site; inferred, may not match the real contract)
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

    # Fetch the total card count
    try:
        total = contract.functions.totalCards().call()
        print(f"✅ 總卡數: {total}")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ totalCards() 失敗: {e}")
        total = 100  # default to fetching 100

    # Fetch info for all cards
    cards = []
    for i in range(total):
        try:
            card = contract.functions.cards(i).call()
            card_id, name, rarity, price = card
            cards.append({
                "id": int(card_id),
                "name": name,
                "rarity": int(rarity),
                "price": int(price) / 1e18  # convert from BNB units to ETH
            })
        except Exception as e:  # noqa: BLE001
            print(f"❌ card {i} 抓取失敗: {e}")

    print(f"✅ 抓到 {len(cards)} 張卡")
    if not cards:
        print("⚠️  鏈上未取得任何卡（ABI 可能不符或合約無此介面）；不覆寫資料。")
        return 1

    # Save to file
    os.makedirs("data", exist_ok=True)
    with open("data/renaiss_cards_onchain.json", "w", encoding="utf-8") as f:
        json.dump(cards, f, indent=2, ensure_ascii=False)
    print("✅ 已存 data/renaiss_cards_onchain.json")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
