"""Local vector database (Chroma, persisted under var/chroma) used for RAG and memory search.

Embeddings, picked by HRAI_EMBEDDINGS:
- auto (default): Chroma's built-in all-MiniLM-L6-v2 ONNX model (free, local; downloads ~80 MB once),
  falling back to `hash` if that cannot load.
- ollama: an Ollama embedding model (HRAI_OLLAMA_EMBED_MODEL, default nomic-embed-text).
- hash: a dependency-free hashed bag-of-words embedding. Lower quality; used offline and in tests.
"""

import hashlib
import logging
import math
import re

from .. import config, db

log = logging.getLogger(__name__)
DIM = 384
_state = {"embedder": None, "kind": None, "client": None, "path": None}


def _hash_embed(texts):
    out = []
    for text in texts:
        vec = [0.0] * DIM
        words = re.findall(r"[a-z0-9]+", text.lower())
        grams = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
        for g in grams:
            h = int(hashlib.md5(g.encode()).hexdigest(), 16)
            vec[h % DIM] += 1.0 if (h >> 8) % 2 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out.append([v / norm for v in vec])
    return out


def _embedder():
    if _state["embedder"]:
        return _state["embedder"], _state["kind"]
    kind = config.env("HRAI_EMBEDDINGS", "auto").lower()
    fn = None
    if kind == "ollama":
        from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
        ef = OllamaEmbeddingFunction(url=config.OLLAMA_BASE,
                                     model_name=config.env("HRAI_OLLAMA_EMBED_MODEL", "nomic-embed-text"))
        fn = lambda texts: [list(map(float, v)) for v in ef(texts)]
    elif kind in ("auto", "minilm"):
        try:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
            ef = DefaultEmbeddingFunction()
            ef(["warm up"])
            fn, kind = (lambda texts: [list(map(float, v)) for v in ef(texts)]), "minilm"
        except Exception as exc:
            log.warning("MiniLM embeddings unavailable (%s); using hash embeddings", exc)
    if fn is None:
        fn, kind = _hash_embed, "hash"
    _state["embedder"], _state["kind"] = fn, kind
    return fn, kind


def client():
    path = str(config.home() / "chroma")
    if _state["client"] is None or _state["path"] != path:
        import chromadb
        from chromadb.config import Settings
        _state["client"] = chromadb.PersistentClient(path=path, settings=Settings(anonymized_telemetry=False))
        _state["path"] = path
    return _state["client"]


def collection(name):
    _, kind = _embedder()
    # One collection per embedder, so vectors of different sizes never mix.
    return client().get_or_create_collection(f"{name}-{kind}", embedding_function=None,
                                             metadata={"hnsw:space": "cosine"})


def upsert(name, docs):
    """docs: [{"id", "text", "meta"}]"""
    if not docs:
        return 0
    embed, _ = _embedder()
    col = collection(name)
    col.upsert(ids=[d["id"] for d in docs], documents=[d["text"] for d in docs],
               embeddings=embed([d["text"] for d in docs]),
               metadatas=[{k: str(v) for k, v in (d.get("meta") or {"src": name}).items()} for d in docs])
    return len(docs)


KEYWORD_WEIGHT = 0.5 if config.env("HRAI_EMBEDDINGS", "auto") == "hash" else 0.15
STOP = set("the a an of for to is in and how many what do i my can get on we me much are does take our there who "
           "which with by be it this that you your".split())


def _keywords(text):
    return {w.rstrip("s") for w in re.findall(r"[a-z]+", text.lower()) if w not in STOP and len(w) > 2}


def search(name, query, k=3, where=None):
    """Hybrid search: vector similarity from Chroma, re-ranked with keyword overlap (titles count double)."""
    embed, _ = _embedder()
    col = collection(name)
    n = col.count()
    if n == 0:
        return []
    res = col.query(query_embeddings=embed([query]), n_results=min(max(k * 4, 10), n), where=where)
    q = _keywords(query)
    hits = []
    for i, t, m, d in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]):
        title = _keywords(t.split("\n", 1)[0])
        overlap = (len(q & _keywords(t)) + len(q & title)) / (len(q) or 1)
        hits.append({"id": i, "text": t, "meta": m, "score": round((1 - d) + KEYWORD_WEIGHT * overlap, 3)})
    return sorted(hits, key=lambda h: -h["score"])[:k]


def sections(markdown_text, source):
    """Split a markdown document into '## ' sections, the unit we embed and cite."""
    parts = re.split(r"^## ", markdown_text, flags=re.M)[1:]
    out = []
    for p in parts:
        title, _, body = p.partition("\n")
        out.append({"id": f"{source}:{title.strip()[:60]}", "text": f"{title.strip()}\n{body.strip()}",
                    "meta": {"source": source, "section": title.strip()}})
    return out


def handbook_text():
    """The policy handbook plus approved additions written by the ticket agent."""
    text = (config.DATA / "policy_handbook.md").read_text(encoding="utf-8")
    extra = config.DATA / "kb_additions.md"
    if extra.exists():
        text += "\n" + extra.read_text(encoding="utf-8")
    return text


def index_all():
    """(Re)build every collection from the handbook, FAQ and candidate resumes."""
    counts = {}
    counts["policy"] = upsert("policy", sections(handbook_text(), "policy_handbook"))
    counts["faq"] = upsert("faq", sections((config.DATA / "candidate_faq.md").read_text(encoding="utf-8"), "candidate_faq"))
    counts["resumes"] = upsert("resumes", [
        {"id": c["id"], "text": c["resume_text"], "meta": {"name": c["name"], "job_id": c["job_id"] or ""}}
        for c in db.q("SELECT * FROM candidates")])
    counts["embedder"] = _embedder()[1]
    return counts
