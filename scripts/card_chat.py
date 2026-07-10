#!/usr/bin/env python3
"""
CollectIQ card chatbot backend (retrieval + narrative generation).

This powers the floating chat widget. For any user question it:
  1. Retrieves the most relevant card(s) from the marketplace snapshot.
  2. Assembles grounded facts (ask vs. index FMV, grade, scarcity,
     cross-source agreement) and derives selling points + a price-driver note.
  3. Asks an OpenAI-compatible LLM (Qwen3-VL) to answer *only* from those facts.
     The card image is passed to the vision model when available.

If the LLM endpoint is unreachable the widget still works: a structured
fact-based answer is returned so the feature degrades gracefully.

Configuration (environment variables):
  CIQ_LLM_BASE     OpenAI-compatible base URL   (default http://192.168.50.27:8081/v1)
  CIQ_LLM_MODEL    chat model name              (default qwen3-vl-8b)
  CIQ_LLM_KEY      API key, if the server needs one (default "")
  CIQ_LLM_TIMEOUT  request timeout seconds      (default 30)
  CIQ_LLM_VISION   "1" to send the card image to the VL model (default 1)
"""

import json
import os
import re
import time
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETPLACE = os.path.join(BASE_DIR, "data", "marketplace_all.json")
VEC_DB = os.path.join(BASE_DIR, "data", "card_vectors.db")

EMBED_BASE = os.environ.get("CIQ_EMBED_BASE", "http://192.168.50.27:11434/v1").rstrip("/")
EMBED_MODEL = os.environ.get("CIQ_EMBED_MODEL", "bge-m3")
EMBED_KEY = os.environ.get("CIQ_EMBED_KEY", "")

LLM_BASE = os.environ.get("CIQ_LLM_BASE", "http://192.168.50.27:8081/v1").rstrip("/")
LLM_MODEL = os.environ.get("CIQ_LLM_MODEL", "qwen3-vl-8b")
LLM_KEY = os.environ.get("CIQ_LLM_KEY", "")
LLM_TIMEOUT = float(os.environ.get("CIQ_LLM_TIMEOUT", "30"))
LLM_VISION = os.environ.get("CIQ_LLM_VISION", "1") not in ("0", "false", "")

_STOP = {"the", "a", "an", "of", "for", "and", "is", "why", "what", "how",
         "much", "worth", "this", "card", "price", "tell", "me", "about",
         "does", "do", "it", "its", "value", "should", "i", "buy"}

# ── card snapshot (cached with mtime check) ──────────────────────────────
_cache = {"mtime": 0.0, "cards": []}


def _load_cards():
    try:
        mt = os.path.getmtime(MARKETPLACE)
    except OSError:
        return []
    if mt != _cache["mtime"]:
        try:
            with open(MARKETPLACE, encoding="utf-8") as f:
                data = json.load(f)
            _cache["cards"] = data if isinstance(data, list) else list(data.values())
            _cache["mtime"] = mt
        except Exception:
            _cache["cards"] = _cache.get("cards", [])
    return _cache["cards"]


def _tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in _STOP and len(t) > 1]


def _embed_query(text):
    """Embed a single string via the OpenAI-compatible embeddings endpoint."""
    url = f"{EMBED_BASE}/embeddings"
    payload = json.dumps({"model": EMBED_MODEL, "input": [text]}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if EMBED_KEY:
        headers["Authorization"] = f"Bearer {EMBED_KEY}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["data"][0]["embedding"]


def vector_search(query, k=5):
    """Semantic retrieval over data/card_vectors.db (built by build_card_vectors.py).

    Returns [] if the vector store or embedding endpoint is unavailable, so the
    caller can fall back to keyword search transparently.
    """
    if not os.path.exists(VEC_DB):
        return []
    try:
        import sqlite3
        import struct
        import numpy as np
        qv = np.asarray(_embed_query(query), dtype=np.float32)
        qn = np.linalg.norm(qv) or 1.0
        conn = sqlite3.connect(VEC_DB)
        rows = conn.execute("SELECT token_id, dim, vec FROM vectors").fetchall()
        conn.close()
        if not rows:
            return []
        ids, mat = [], []
        for tid, dim, blob in rows:
            mat.append(struct.unpack(f"<{dim}f", blob))
            ids.append(tid)
        mat = np.asarray(mat, dtype=np.float32)
        sims = (mat @ qv) / (np.linalg.norm(mat, axis=1) * qn + 1e-9)
        top = np.argsort(-sims)[:k]
        cards = _load_cards()
        by_id = {str(c.get("token_id")): c for c in cards}
        return [by_id[ids[i]] for i in top if ids[i] in by_id]
    except Exception:  # noqa: BLE001 — any failure -> keyword fallback
        return []


def retrieve(query, k=5):
    """Retrieve relevant cards: semantic vector search when available,
    otherwise keyword overlap on name / set / character.
    """
    hits = vector_search(query, k=k)
    if hits:
        return hits
    cards = _load_cards()
    qtok = _tokens(query)
    if not qtok or not cards:
        return []
    scored = []
    for c in cards:
        hay = " ".join(str(c.get(f, "")) for f in
                       ("name", "set_name", "character_name", "card_number", "grade", "grader"))
        htok = set(_tokens(hay))
        if not htok:
            continue
        score = sum(1 for t in qtok if t in htok)
        # small boost for exact phrase presence
        if query.lower().strip() and query.lower().strip() in hay.lower():
            score += 2
        if score:
            scored.append((score, c))
    scored.sort(key=lambda x: (-x[0], x[1].get("ask_price") or 1e9))
    return [c for _, c in scored[:k]]


def find_card(token_id=None, name=None):
    cards = _load_cards()
    if token_id:
        for c in cards:
            if str(c.get("token_id")) == str(token_id):
                return c
    if name:
        hits = retrieve(name, k=1)
        if hits:
            return hits[0]
    return None


# ── grounded facts + derived narrative ───────────────────────────────────
def card_facts(card):
    """Return (facts_text, selling_points, price_note) grounded in real data."""
    name = card.get("name") or "Unknown card"
    ask = card.get("ask_price")
    fmv = card.get("fmv") or card.get("market_price")
    grade = card.get("grade") or ""
    grader = card.get("grader") or ""
    sources = card.get("sources") or 0
    qty = card.get("remaining_quantity")

    lines = [f"Name: {name}"]
    if card.get("set_name"):
        lines.append(f"Set: {card['set_name']}")
    if grader or grade:
        lines.append(f"Grade: {grader} {grade}".strip())
    if ask is not None:
        lines.append(f"Ask price: ${ask:,.2f}")
    if fmv is not None:
        lines.append(f"Index FMV (Renaiss first-party): ${fmv:,.2f}")
    if sources:
        lines.append(f"Independent price sources agreeing: {sources}")
    if qty is not None:
        lines.append(f"Remaining quantity listed: {qty}")

    gap_pct = None
    if ask is not None and fmv:
        gap_pct = (ask - fmv) / fmv * 100.0
        lines.append(f"Ask vs FMV: {gap_pct:+.1f}%")

    selling = []
    if re.search(r"\b10\b", str(grade)) and grader.upper() in ("PSA", "BGS", "CGC"):
        selling.append(f"Top-tier {grader} 10 gem-mint grade — the most liquid, collectible tier")
    if gap_pct is not None and gap_pct <= -5:
        selling.append(f"Listed {abs(gap_pct):.0f}% below the platform index FMV — potential value entry")
    if gap_pct is not None and gap_pct >= 10:
        selling.append("Priced at a premium to index — signals scarcity or strong demand")
    if sources and sources > 1:
        selling.append(f"Cross-verified across {sources} independent sources — high price confidence")
    if qty == 1:
        selling.append("Only one listed — thin float / single available copy")
    if card.get("character_name"):
        selling.append(f"Features {card['character_name']} — character-driven collector demand")
    if not selling:
        selling.append("Graded real-world asset with verifiable on-chain provenance")

    # Price-driver note (honestly derived from current pricing signals, not intraday history)
    if gap_pct is None:
        note = "Not enough pricing signal to assess direction."
    elif gap_pct <= -8:
        note = ("Trading below the index suggests either a value opportunity or "
                "recent softening in demand for this grade.")
    elif gap_pct >= 12:
        note = ("Trading well above the index points to scarcity, a hot character, "
                "or seller premium on a thin supply.")
    else:
        note = "Priced close to the platform index — fairly valued on current signals."

    return "\n".join(lines), selling, note


def _fallback_answer(cards, question):
    """Structured, useful answer when the LLM endpoint is offline."""
    if not cards:
        return ("I couldn't find a matching card in the current marketplace snapshot. "
                "Try a more specific card name (set, number, or character).")
    parts = []
    for c in cards[:3]:
        facts, selling, note = card_facts(c)
        block = [facts, "Why it's priced this way: " + note,
                 "Selling points:"] + [f"  • {s}" for s in selling]
        parts.append("\n".join(block))
    return ("(Model offline — showing grounded facts)\n\n" + "\n\n———\n\n".join(parts))


# ── LLM call (OpenAI-compatible /chat/completions) ───────────────────────
def _llm_chat(messages, image_url=None, timeout=None):
    url = f"{LLM_BASE}/chat/completions"
    if image_url and LLM_VISION and messages:
        last = messages[-1]
        messages = messages[:-1] + [{
            "role": last["role"],
            "content": [
                {"type": "text", "text": last["content"]},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }]
    payload = {"model": LLM_MODEL, "messages": messages,
               "temperature": 0.4, "max_tokens": 700, "stream": False}
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if LLM_KEY:
        headers["Authorization"] = f"Bearer {LLM_KEY}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout or LLM_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"].strip()


SYSTEM = (
    "You are CollectIQ's card analyst for the Renaiss RWA card marketplace. "
    "Answer ONLY from the CARD FACTS provided. Explain price drivers using the "
    "ask-vs-index-FMV gap, grade, scarcity (remaining quantity), and cross-source "
    "agreement. When asked why a card rose or fell, reason from the current pricing "
    "signals and clearly say it is inferred from present pricing, not intraday history, "
    "unless history is given. Give concrete selling points. Be concise (<150 words), "
    "confident, and never invent numbers not in the facts."
)


def answer(message, token_id=None, card_name=None, history=None):
    """Main entry: returns {answer, sources, llm_online}."""
    focus = find_card(token_id=token_id, name=card_name) if (token_id or card_name) else None
    cards = [focus] if focus else retrieve(message, k=5)

    fact_blocks, sources_meta, image_url = [], [], None
    for c in cards[:3]:
        facts, selling, note = card_facts(c)
        fact_blocks.append(facts + "\nDerived price note: " + note +
                           "\nDerived selling points: " + "; ".join(selling))
        sources_meta.append({
            "name": c.get("name"), "token_id": c.get("token_id"),
            "ask_price": c.get("ask_price"), "fmv": c.get("fmv") or c.get("market_price"),
            "image_url": c.get("image_url"), "url": c.get("marketplace_url"),
        })
    if focus and focus.get("image_url"):
        image_url = focus["image_url"]

    if not cards:
        return {"answer": _fallback_answer([], message), "sources": [], "llm_online": False}

    facts_text = "\n\n---\n\n".join(fact_blocks)
    msgs = list(history or [])
    msgs = [{"role": "system", "content": SYSTEM}] + msgs + [
        {"role": "user", "content": f"CARD FACTS:\n{facts_text}\n\nQUESTION: {message}"}]

    try:
        text = _llm_chat(msgs, image_url=image_url)
        return {"answer": text, "sources": sources_meta, "llm_online": True}
    except Exception as e:  # noqa: BLE001 — degrade gracefully on any transport error
        return {"answer": _fallback_answer(cards, message), "sources": sources_meta,
                "llm_online": False, "error": f"{type(e).__name__}"}


def health():
    """Report LLM reachability + snapshot size for the widget/status page."""
    cards = _load_cards()
    online, detail = False, ""
    try:
        req = urllib.request.Request(f"{LLM_BASE}/models",
                                     headers={"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {})
        with urllib.request.urlopen(req, timeout=4) as resp:
            online = resp.status == 200
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}"
    vec_count = 0
    if os.path.exists(VEC_DB):
        try:
            import sqlite3
            conn = sqlite3.connect(VEC_DB)
            vec_count = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
            conn.close()
        except Exception:  # noqa: BLE001
            vec_count = 0
    return {"llm_online": online, "llm_base": LLM_BASE, "llm_model": LLM_MODEL,
            "cards_indexed": len(cards), "detail": detail,
            "retrieval": "vector" if vec_count else "keyword",
            "vectors": vec_count, "embed_model": EMBED_MODEL}


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "Why is this Meowth card priced the way it is?"
    print(json.dumps(answer(q), ensure_ascii=False, indent=2))
