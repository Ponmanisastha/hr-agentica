"""Your own HR policy documents: the folder the policy agent answers from.

Put policy files in `policies/` (or the folder named by HRAI_POLICY_DIR): .md, .txt, .pdf or .docx, in
subfolders if you like. Each file is split into sections, and each section is embedded (RAG), mined for
leave rules (KAG), and cited by file name and section title in answers. When the folder has no documents,
the sample handbook in data/policy_handbook.md is used instead, so a fresh install still answers.

Changes are picked up by `python app.py index`, by uploading on the Policies page, and by the `policy_watch`
trigger, which re-indexes within a few minutes of a file being added, changed or removed.
"""

import hashlib
import re
from pathlib import Path

from .. import config

SUPPORTED = {".md", ".txt", ".pdf", ".docx"}
SAMPLE = "policy_handbook.md"
CHUNK = 1200  # characters per section when a document has no headings


def policy_dir() -> Path:
    path = Path(config.env("HRAI_POLICY_DIR", str(config.ROOT / "policies")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def own_documents():
    """The organisation's own policy files (README files in the folder are instructions, not policy)."""
    root = policy_dir()
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
                  and p.stem.lower() != "readme" and not p.name.startswith((".", "~$")))


def documents():
    """What the agent answers from: your documents, or the sample handbook when there are none, plus any
    approved additions the ticket agent wrote (data/kb_additions.md)."""
    docs = own_documents() or [config.DATA / SAMPLE]
    extra = config.DATA / "kb_additions.md"
    return docs + ([extra] if extra.exists() else [])


def label(path: Path):
    """How a document is named in citations."""
    if path.parent == config.DATA:
        return {"policy_handbook.md": "Policy handbook", "kb_additions.md": "Approved additions"}.get(path.name, path.name)
    return str(path.relative_to(policy_dir()))


def extract(path: Path):
    from ..hiring import extract_text  # same reader the resume inbox uses (.txt, .md, .pdf, .docx)
    return (extract_text(path) or "").replace("\r\n", "\n")


_NUMBERED = re.compile(r"^(\d+(\.\d+)*[.)]?|[IVX]+\.|section \d+[.:]?)\s+\S", re.I)


def _is_heading(line):
    """A short line that looks like a title: '# Leave', '3. Casual leave', '2.1 Sick leave', 'SICK LEAVE'."""
    s = line.strip()
    if not s or len(s) > 80:
        return False
    if s.startswith("#"):
        return True
    words = s.split()
    if s.endswith((".", ",", ";")) or len(words) > 10:
        return False
    return bool(_NUMBERED.match(s)) or (s.isupper() and any(c.isalpha() for c in s) and len(words) <= 8)


def _chunks(text, title):
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, buf = [], ""
    for p in paras:
        if buf and len(buf) + len(p) > CHUNK:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n\n{p}".strip()
    if buf:
        out.append(buf)
    return [(f"{title} (part {i})" if len(out) > 1 else title, body) for i, body in enumerate(out, 1)]


def split(text, title):
    """[(section title, body)]: markdown '## ' sections, else heading-like lines (numbered or all caps), else
    fixed-size chunks so even an unstructured PDF can be searched and cited."""
    if re.search(r"^## ", text, re.M):
        parts = re.split(r"^## ", text, flags=re.M)[1:]
        return [(p.partition("\n")[0].strip(), p.partition("\n")[2].strip()) for p in parts]
    lines = text.split("\n")
    heads = [i for i, l in enumerate(lines) if _is_heading(l)]
    if len(heads) >= 2:
        out = []
        for a, b in zip(heads, heads[1:] + [len(lines)]):
            body = "\n".join(lines[a + 1:b]).strip()
            if body:
                out.append((lines[a].strip().lstrip("#").strip(), body))
        if out:
            return out
    return _chunks(text, title)


_memo = {"fingerprint": None, "sections": None}


def sections():
    """Every section of every policy document, ready to embed: {"id", "text", "meta"}. Re-read only when a
    file changes, so a PDF is not parsed again for every question."""
    fp = fingerprint()
    if _memo["fingerprint"] != fp:
        _memo.update(fingerprint=fp, sections=_read_sections())
    return _memo["sections"]


def _read_sections():
    out, seen = [], set()
    for path in documents():
        name = label(path)
        try:
            text = extract(path)
        except Exception as exc:  # an unreadable file should not stop the rest from being indexed
            out.append({"id": f"{name}:unreadable", "text": f"{name}\nThis document could not be read ({exc}).",
                        "meta": {"source": name, "section": "unreadable"}})
            continue
        for title, body in split(text, Path(name).stem):
            sid = f"{name}:{title[:60]}"
            n = 2
            while sid in seen:
                sid, n = f"{name}:{title[:56]} #{n}", n + 1
            seen.add(sid)
            out.append({"id": sid, "text": f"{title}\n{body}", "meta": {"source": name, "section": title}})
    return out


def combined_text():
    """All policy text as one markdown document (what the CAG prompt carries)."""
    parts, current = [], None
    for s in sections():
        if s["meta"]["source"] != current:
            current = s["meta"]["source"]
            parts.append(f"# {current}")
        parts.append(f"## {s['text']}")
    return "\n\n".join(parts)


def fingerprint():
    """Changes when a document is added, edited or removed (the policy_watch trigger compares it)."""
    h = hashlib.sha256(str(policy_dir()).encode())
    for p in documents():
        st = p.stat()
        h.update(f"{p}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


def summary():
    by_doc = {}
    for s in sections():
        by_doc.setdefault(s["meta"]["source"], []).append(s["meta"]["section"])
    from . import kag
    return {"folder": str(policy_dir()), "using_sample": not own_documents(),
            "documents": [{"name": k, "sections": v} for k, v in by_doc.items()], "rules": kag.rules()}
