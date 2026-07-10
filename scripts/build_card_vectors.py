#!/usr/bin/env python3
"""
Build the CollectIQ card vector store for semantic retrieval.

Embeds one document per marketplace card via an OpenAI-compatible
/v1/embeddings endpoint (e.g. Ollama bge-m3 or Qwen3-Embedding) and stores
the vectors in data/card_vectors.db. card_chat.py automatically prefers this
vector store for retrieval when it exists, and falls back to keyword search
otherwise.

Configuration (environment variables):
  CIQ_EMBED_BASE   OpenAI-compatible base URL  (default http://192.168.50.27:11434/v1)
  CIQ_EMBED_MODEL  embedding model name        (default bge-m3)
  CIQ_EMBED_KEY    API key, if the server needs one (default "")
  CIQ_EMBED_BATCH  cards per embeddings request (default 32)

Run:  /opt/anaconda3/bin/python3 scripts/build_card_vectors.py
"""

import json
import os
import sqlite3
import struct
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETPLACE = os.path.join(BASE_DIR, "data", "marketplace_all.json")
VEC_DB = os.path.join(BASE_DIR, "data", "card_vectors.db")

EMBED_BASE = os.environ.get("CIQ_EMBED_BASE", "http://192.168.50.27:11434/v1").rstrip("/")
EMBED_MODEL = os.environ.get("CIQ_EMBED_MODEL", "bge-m3")
EMBED_KEY = os.environ.get("CIQ_EMBED_KEY", "")
BATCH = int(os.environ.get("CIQ_EMBED_BATCH", "32"))


def card_doc(c):
    """Compact text representation of a card for embedding."""
    parts = [c.get("name") or ""]
    for f in ("set_name", "character_name", "grader", "grade"):
        if c.get(f):
            parts.append(str(c[f]))
    ask, fmv = c.get("ask_price"), c.get("fmv") or c.get("market_price")
    if ask is not None:
        parts.append(f"ask ${ask}")
    if fmv is not None:
        parts.append(f"index fmv ${fmv}")
    return " | ".join(parts)


def embed(texts):
    url = f"{EMBED_BASE}/embeddings"
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if EMBED_KEY:
        headers["Authorization"] = f"Bearer {EMBED_KEY}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return [d["embedding"] for d in body["data"]]


def pack_vec(vec):
    return struct.pack(f"<{len(vec)}f", *vec)


def main():
    with open(MARKETPLACE, encoding="utf-8") as f:
        data = json.load(f)
    cards = data if isinstance(data, list) else list(data.values())
    cards = [c for c in cards if c.get("token_id")]
    print(f"[vectors] {len(cards)} cards to embed via {EMBED_MODEL} @ {EMBED_BASE}")

    conn = sqlite3.connect(VEC_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS vectors("
                 "token_id TEXT PRIMARY KEY, name TEXT, dim INT, vec BLOB)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")

    t0, done, dim = time.time(), 0, 0
    for i in range(0, len(cards), BATCH):
        chunk = cards[i:i + BATCH]
        try:
            vecs = embed([card_doc(c) for c in chunk])
        except Exception as e:  # noqa: BLE001
            print(f"[vectors] embeddings request failed at {i}: {type(e).__name__}: {e}")
            print("[vectors] Is the embedding endpoint reachable? Aborting.")
            conn.commit(); conn.close(); sys.exit(1)
        for c, v in zip(chunk, vecs):
            dim = len(v)
            conn.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)",
                         (str(c["token_id"]), c.get("name"), dim, pack_vec(v)))
        done += len(chunk)
        conn.commit()
        print(f"[vectors] {done}/{len(cards)} ({time.time()-t0:.1f}s)", end="\r")

    conn.execute("INSERT OR REPLACE INTO meta VALUES ('model', ?)", (EMBED_MODEL,))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('dim', ?)", (str(dim),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('count', ?)", (str(done),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
                 (time.strftime("%F %T"),))
    conn.commit(); conn.close()
    print(f"\n[vectors] done: {done} vectors, dim={dim} -> {VEC_DB}")


if __name__ == "__main__":
    main()
