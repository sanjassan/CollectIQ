#!/usr/bin/env python3
"""
On-chain address discovery for gacha pools — locate each pool's contract address and detect new pools.

Background (user observation, verified):
  Renaiss first mints cards on-chain and loads them into a gacha contract (pool),
  then opens pulls; so a new pool's contract address usually appears on-chain
  *before* the official Twitter/Discord announcement.
  Watching Transfers on the shared NFT contract 0xF864…5b30 lets us catch new pools pre-announcement.

Known pool addresses (built into track_pulls_onchain.py):
  omega      0x94E7732B0B2E7c51FFD0D56580067d9c2e2B7910
  eden       0xfdA4a907D23d9f24271Bc47483C5B983831E325E
  renacrypt  0xb2891022648c5Fad3721C42C05d8d283D4d53080
  legacy/costume(old) 0xAAb5F5FA75437a6e9E7004c12C9c56CdA4b4885A

Discovery method (reads data/onchain_pulls.db, continuously accumulated by track_pulls_onchain.py):
  A pool contract's signature = sends many distinct tokens to many distinct buyers, while
  almost only sending and never receiving (or receiving freshly minted cards from 0x0 = pool being loaded).
  Candidates are scored accordingly, and high-scoring addresses not in the known list are
  flagged as "suspected new pools".

Outputs data/pool_addresses.json (known + candidates). Prints new candidates and can push notifications.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
DB = DATA / "onchain_pulls.db"
OUT = DATA / "pool_addresses.json"

KNOWN = {
    "0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910": "omega",
    "0xfda4a907d23d9f24271bc47483c5b983831e325e": "eden-pack",
    "0xb2891022648c5fad3721c42c05d8d283d4d53080": "renacrypt-pack",
    "0xaab5f5fa75437a6e9e7004c12c9c56cda4b4885a": "legacy/costume(old)",
}
ZERO = "0x0000000000000000000000000000000000000000"


def discover() -> dict:
    if not DB.exists():
        return {"error": "onchain_pulls.db does not exist (run track_pulls_onchain.py first)"}
    db = sqlite3.connect(DB)

    # Distribution profile per from_addr
    rows = db.execute("""
        SELECT from_addr,
               COUNT(DISTINCT to_addr)   AS distinct_to,
               COUNT(DISTINCT token_id)  AS distinct_tok,
               COUNT(*)                  AS sent
        FROM onchain_pulls
        GROUP BY from_addr
    """).fetchall()

    # Addresses that received mints from 0x0 (earliest signal of a pool being loaded)
    mint_recv = dict(db.execute(
        "SELECT to_addr, COUNT(*) FROM onchain_pulls WHERE from_addr=? GROUP BY to_addr",
        (ZERO,),
    ).fetchall())

    candidates = []
    knowns = []
    for frm, dto, dtok, sent in rows:
        if frm == ZERO:
            continue
        recv = db.execute("SELECT COUNT(*) FROM onchain_pulls WHERE to_addr=?", (frm,)).fetchone()[0]
        loaded = mint_recv.get(frm, 0)
        entry = {
            "address": frm,
            "distinct_recipients": dto,
            "distinct_tokens": dtok,
            "sent": sent,
            "received": recv,
            "minted_in_from_zero": loaded,
        }
        lname = frm.lower()
        if lname in KNOWN:
            entry["slug"] = KNOWN[lname]
            knowns.append(entry)
            continue
        # Pool scoring: sends to many distinct buyers and many distinct tokens, receives little itself (pure distribution), or has been loaded with mints.
        pool_like = (dto >= 80 and dtok >= 80 and recv < max(dto * 0.3, 10)) or loaded >= 20
        if pool_like:
            entry["confidence"] = round(min(1.0, dto / 300 + loaded / 50), 2)
            candidates.append(entry)

    db.close()
    candidates.sort(key=lambda e: -e["confidence"])
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "known": knowns,
        "candidates": candidates,
        "note": "candidates are heuristically suspected new pools; high distinct_recipients, low received, or minted_in_from_zero>0 are the most suspicious. A longer on-chain history is needed for Friday's new pools to surface.",
    }


def main() -> int:
    res = discover()
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    OUT.write_text(json.dumps(res, indent=2, ensure_ascii=False))

    if "error" in res:
        print("⚠️", res["error"])
        return 1

    prev_cand = {c["address"] for c in prev.get("candidates", [])}
    new_cand = [c for c in res["candidates"] if c["address"] not in prev_cand]

    print(f"[{time.strftime('%F %T')}] pool detection: known {len(res['known'])} · suspected candidates {len(res['candidates'])} · new candidates {len(new_cand)}")
    for c in res["candidates"][:8]:
        print(f"   {'🆕' if c['address'] not in prev_cand else '  '} {c['address']} "
              f"buyers={c['distinct_recipients']} tokens={c['distinct_tokens']} "
              f"recv={c['received']} mintIn={c['minted_in_from_zero']} conf={c['confidence']}")

    if new_cand:
        lines = ["🔎 *On-chain detection: suspected new pool contracts*"]
        for c in new_cand[:5]:
            lines.append(f"`{c['address']}` buyers {c['distinct_recipients']} / token {c['distinct_tokens']}"
                         + (f" · loaded mint {c['minted_in_from_zero']}" if c['minted_in_from_zero'] else ""))
        msg = "\n".join(lines)
        try:
            from main import TelegramAlert
            tg = TelegramAlert()
            if tg.is_configured():
                tg.send_alert(msg)
                print("📨 Telegram notification sent")
        except Exception as e:
            print(f"⚠️ Notification failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
