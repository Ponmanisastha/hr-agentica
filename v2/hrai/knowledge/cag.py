"""CAG: cache-augmented generation for the policy handbook.

Why it helps here: the handbook is small (a few thousand tokens) and changes rarely, and most questions touch
it. Instead of retrieving 2-3 chunks (and sometimes missing the right one), the policy agent preloads the whole
handbook into its system prompt, and the AI gateway marks that prompt for Anthropic prompt caching, so repeat
calls read it from cache at about a tenth of the input price. On top of that, answers to generic questions
("how many sick days do we get?") are cached in SQLite and reused until the handbook changes.

When the handbook outgrows HRAI_CAG_MAX_CHARS, the agent switches to RAG over the vector store automatically.
"""

import hashlib
import re

from .. import config, db
from . import vectors

PERSONAL = re.compile(r"\b(i|me|my|mine)\b|\bE\d{3}\b|\bNH-\d+\b|\d{4}-\d{2}-\d{2}", re.I)


def max_chars():
    return int(config.env("HRAI_CAG_MAX_CHARS", "40000"))


def kb_hash():
    return hashlib.sha256(vectors.handbook_text().encode()).hexdigest()[:16]


def policy_context(question, k=3):
    """Return the policy context to ground an answer: the whole handbook (CAG) or top sections (RAG)."""
    text = vectors.handbook_text()
    if len(text) <= max_chars():
        return {"strategy": "cag", "context": text, "kb_hash": kb_hash()}
    hits = vectors.search("policy", question, k=k)
    return {"strategy": "rag", "context": "\n\n".join(f"## {h['text']}" for h in hits), "kb_hash": kb_hash(),
            "sources": [h["meta"].get("section") for h in hits]}


def _key(question):
    return " ".join(re.findall(r"[a-z0-9]+", question.lower()))


def is_generic(question):
    return not PERSONAL.search(question)


def cached_answer(question):
    if not is_generic(question):
        return None
    row = db.q1("SELECT * FROM answer_cache WHERE key=? AND kb_hash=?", (_key(question), kb_hash()))
    if row:
        db.x("UPDATE answer_cache SET hits=hits+1 WHERE key=?", (row["key"],))
        return row["answer"]
    return None


def store_answer(question, answer):
    if is_generic(question) and answer:
        db.x("INSERT OR REPLACE INTO answer_cache (key, answer, kb_hash, hits, ts) VALUES (?,?,?,0,?)",
             (_key(question), answer, kb_hash(), db.now()))
