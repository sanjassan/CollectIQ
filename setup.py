#!/usr/bin/env python3
"""
Install and Setup Script for Renaiss EV Monitor v2
"""

import json
import os
import sys
import subprocess

def install_dependencies():
    """Install required Python packages"""
    print("[INFO] Installing dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

def setup_data_files():
    """Create initial data files if they don't exist"""
    import json

    print("[INFO] Setting up data files...")
    
    # Create directories
    os.makedirs("data", exist_ok=True)
    os.makedirs("image_cache", exist_ok=True)
    
    # Create default pool data
    pool_data = [
        {
            "card_id": "E001",
            "name": "Legendary Eagle",
            "remaining_quantity": 5,
            "market_price": 1500.00,
            "image_url": "https://example.com/images/legendary_eagle.png"
        },
        {
            "card_id": "E002",
            "name": "Mystic Phoenix",
            "remaining_quantity": 15,
            "market_price": 500.00,
            "image_url": "https://example.com/images/mystic_phoenix.png"
        }
    ]
    
    with open("data/pool_data.json", "w") as f:
        json.dump(pool_data, f, indent=2)
        print("[INFO] Created data/pool_data.json")
    
    # Create default pack data
    pack_data = [
        {
            "pack_id": "omega",
            "name": "OMEGA Pack",
            "price": 48.00,
            "top_prize": 1100.00,
            "pack_type": "Infinite",
            "total_cards": 10000,
            "remaining_cards": 9500
        }
    ]
    
    with open("data/pack_data.json", "w") as f:
        json.dump(pack_data, f, indent=2)
        print("[INFO] Created data/pack_data.json")

def main():
    """Main setup function"""
    print("=" * 50)
    print("RENAISS EV MONITOR v2 - Setup")
    print("=" * 50)
    
    # Install dependencies
    try:
        install_dependencies()
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed to install dependencies: {e}")
        return 1
    
    # Setup data files
    setup_data_files()
    
    print("\n" + "=" * 50)
    print("Setup complete!")
    print("=" * 50)
    print("\nNext steps:")
    print("1. Edit .env with your Telegram bot token")
    print("2. Run: python main.py --once")
    print("3. For web dashboard: python dashboard.py")
    print("4. For continuous monitoring: python main.py")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
