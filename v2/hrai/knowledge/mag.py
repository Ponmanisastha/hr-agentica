"""MAG: memory-augmented generation. Each user gets a long-term memory the agents read before answering.

Why it helps here: HR conversations are follow-ups ("and my sick leave?", "do the same for Vikram",
"I prefer email over calls"). Short-term memory is the conversation thread (LangGraph state); long-term memory
is stored here: one row per remembered fact in SQLite, embedded in the vector store for semantic recall.
Memories are per user and never shown to other users.
"""

import json

from .. import db
from . import vectors


def remember(username, text, kind="interaction"):
    mid = db.x("INSERT INTO memories (username, kind, text, ts) VALUES (?,?,?,?)", (username, kind, text[:1000], db.now()))
    vectors.upsert("memories", [{"id": f"m{mid}", "text": text[:1000], "meta": {"username": username, "kind": kind}}])
    return mid


def recall(username, query, k=3):
    recent = [r["text"] for r in db.q("SELECT text FROM memories WHERE username=? ORDER BY id DESC LIMIT 3", (username,))]
    similar = [h["text"] for h in vectors.search("memories", query, k=k, where={"username": username})]
    seen, out = set(), []
    for t in similar + recent:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[: k + 3]


def forget(username):
    db.x("DELETE FROM memories WHERE username=?", (username,))
    try:
        vectors.collection("memories").delete(where={"username": username})
    except Exception:
        pass


def learn_from_turn(username, request, answer, agent):
    """Store a short record of the turn, plus durable preferences/facts the fast model spots (if available)."""
    remember(username, f"[{agent}] asked: {request[:300]} | answered: {answer[:300]}")
    from ..gateway import llm
    if not llm.any_model_available():
        return
    try:
        res = llm.complete("memory", [{"role": "user", "content":
            "From this HR conversation turn, list durable facts or preferences about the user worth remembering "
            "next time (for example preferred contact channel, upcoming plans). Reply with a JSON list of short "
            f"strings, or [] if none.\n\nUser: {request}\nAssistant: {answer}"}], tier="fast", max_tokens=300)
        start, end = res.text.find("["), res.text.rfind("]")
        for fact in json.loads(res.text[start:end + 1])[:3] if start >= 0 else []:
            remember(username, str(fact), kind="fact")
    except Exception:
        pass  # memory is best effort
