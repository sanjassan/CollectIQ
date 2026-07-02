#!/usr/bin/env python3
"""Grab Renaiss card list from website"""
import json
import time
from pathlib import Path

# Use Playwright directly
from playwright.sync_api import sync_playwright

def main():
    output_dir = Path(__file__).parent.parent / "data"
    output_dir.mkdir(exist_ok=True)
    
    cards = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # Navigate to page
        page.goto("https://www.renaiss.xyz/gacha/renacrypt-pack", wait_until="networkidle")
        time.sleep(3)  # Wait for dynamic content
        
        # Extract card data from page
        card_data = page.evaluate("""
            () => {
                const results = [];
                const elements = document.querySelectorAll('*');
                
                for (const el of elements) {
                    const text = el.innerText;
                    if (!text || !text.includes('$') || text.includes('FMV')) continue;
                    
                    const lines = text.split('\\n').filter(l => l.trim());
                    if (lines.length < 2) continue;
                    
                    const name = lines[0];
                    const priceLine = lines.find(l => l.includes('$'));
                    if (!priceLine) continue;
                    
                    const priceMatch = priceLine.match(/\\$([\\d,\\.]+)/);
                    if (!priceMatch) continue;
                    
                    const price = parseFloat(priceMatch[1].replace(',', ''));
                    
                    if (name.includes('#') && price > 50) {
                        results.push({ name, price });
                    }
                }
                
                // Deduplicate by name
                const unique = {};
                for (const c of results) {
                    unique[c.name] = c;
                }
                return Object.values(unique).sort((a, b) => b.price - a.price);
            }
        """)
        
        cards.extend(card_data)
        
        browser.close()
    
    # Save to JSON
    output_file = output_dir / "renaiss_cards.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)
    
    print(f"Found {len(cards)} unique cards, saved to {output_file}")
    
    # Print top 10
    print("\nTop 10 cards by price:")
    for c in cards[:10]:
        print(f"  {c['name']}: ${c['price']}")

if __name__ == "__main__":
    main()
