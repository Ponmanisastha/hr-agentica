"""Versioned policy documents: an upload becomes a new version that HR reviews before it goes live.

Why versions rather than overwrite, merge or replace-all: a policy is a record the company may have to show later
("what did the leave policy say in March?"), and one wrong upload should never silently change leave decisions.
So each upload of a document is staged as the next version, compared with the live one (sections added, removed
or changed, and the leave rules that would change), checked against the other live documents for rules that
disagree, and only published when HR approves it. Publishing archives the previous version; nothing is ever
deleted, and any earlier version can be rolled back. Answers and the leave engine only ever read live versions.

Folders inside the policy folder (both ignored by the agents):
  .pending/<id>/<file>           uploads waiting for approval
  .archive/<file>/v<N>/<file>    a copy of every version that was ever live

Files copied straight into the folder still work: the next re-index registers them as a new live version (that
is the administrator's route; the web upload is HR's route with review).
"""

import hashlib
import json
import re
import shutil
from pathlib import Path

from .. import auth, db
from . import policies


class PolicyError(ValueError):
    pass


def _dirs():
    root = policies.policy_dir()
    return root, root / ".pending", root / ".archive"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _row(r):
    if not r:
        return None
    r = dict(r)
    r["changes"] = json.loads(r["changes"] or "{}")
    r["conflicts"] = json.loads(r["conflicts"] or "[]")
    return r


def active(name):
    return _row(db.q1("SELECT * FROM policy_versions WHERE name=? AND status='active'", (name,)))


def get(version_id):
    return _row(db.q1("SELECT * FROM policy_versions WHERE id=?", (version_id,)))


def history(name):
    return [_row(r) for r in db.q("SELECT * FROM policy_versions WHERE name=? ORDER BY version DESC, id DESC", (name,))]


def pending():
    return [_row(r) for r in db.q("SELECT * FROM policy_versions WHERE status='pending' ORDER BY id")]


def activation_times():
    """{document: when its live version went live}; the knowledge graph prefers the newest on a clash."""
    return {r["name"]: r["activated_at"] for r in db.q("SELECT name, activated_at FROM policy_versions WHERE status='active'")}


def cite(name):
    """How a document is cited: its name, plus the version once it has been updated."""
    r = db.q1("SELECT version FROM policy_versions WHERE name=? AND status='active'", (name,))
    return f"{name} (version {r['version']})" if r and r["version"] > 1 else name


def _next_version(name):
    return (db.q1("SELECT MAX(version) AS v FROM policy_versions WHERE name=?", (name,))["v"] or 0) + 1


def _archive_copy(name, version, src):
    _, _, archive = _dirs()
    dest = archive / name / f"v{version}" / Path(name).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def _archived_file(name, version):
    _, _, archive = _dirs()
    return archive / name / f"v{version}" / Path(name).name


def sync(actor="system"):
    """Register files that reached the folder without an upload (copied in, edited in place, or removed)."""
    root, _, _ = _dirs()
    live = {str(p.relative_to(root)): p for p in policies.own_documents()}
    changed = []
    for name, path in live.items():
        data = path.read_bytes()
        cur = active(name)
        if cur and cur["sha256"] == _sha(data):
            continue
        if cur:
            db.x("UPDATE policy_versions SET status='archived' WHERE id=?", (cur["id"],))
        same = db.q1("SELECT id FROM policy_versions WHERE name=? AND sha256=? AND status IN ('archived','retired') "
                     "ORDER BY version DESC LIMIT 1", (name, _sha(data)))
        if same:  # an earlier version put back in the folder: that version is live again
            db.x("UPDATE policy_versions SET status='active', activated_at=?, note='put back in the folder' WHERE id=?",
                 (db.now(), same["id"]))
            changed.append(name)
            continue
        version = _next_version(name)
        db.x("INSERT INTO policy_versions (name, version, status, sha256, size, changes, conflicts, uploaded_by, "
             "uploaded_at, decided_by, decided_at, activated_at, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (name, version, "active", _sha(data), len(data), "{}", "[]", actor, db.now(), actor, db.now(), db.now(),
              "changed in the folder" if cur else "added in the folder"))
        _archive_copy(name, version, path)
        changed.append(name)
    for r in db.q("SELECT id, name FROM policy_versions WHERE status='active'"):
        if r["name"] not in live:
            db.x("UPDATE policy_versions SET status='retired', note='removed from the folder' WHERE id=?", (r["id"],))
            changed.append(r["name"])
    return changed


# ---------------------------------------------------------------- comparing

def _norm(text):
    return re.sub(r"\s+", " ", text).strip()


def _topic(title):
    """'3. Casual leave' and 'CASUAL LEAVE (2027)' are the same topic."""
    words = re.findall(r"[a-z]+", title.lower())
    return " ".join(w for w in words if w not in {"and", "the", "of", "policy", "section"})


def _sections_of(path, name):
    text = policies.extract(path)
    return [{"text": f"{t}\n{b}", "meta": {"source": name, "section": t}} for t, b in policies.split(text, Path(name).stem)]


def compare(name, path):
    """What publishing this file would change: sections and leave rules, against the live version (if any)."""
    from . import kag
    new = _sections_of(path, name)
    old = [s for s in policies.sections() if s["meta"]["source"] == name]
    body = lambda s: _norm(s["text"].split("\n", 1)[1] if "\n" in s["text"] else "")  # noqa: E731
    before, after = {s["meta"]["section"]: body(s) for s in old}, {s["meta"]["section"]: body(s) for s in new}
    changed = [{"section": t, "before": before[t][:400], "after": after[t][:400]}
               for t in after if t in before and before[t] != after[t]]
    # rules as they are applied now, against what they would be once this version is live (it wins on a clash)
    rules_old = {(s, p) for s, p, _, _, _ in kag.extract(old)}
    rules_new = {(s, p): v for s, p, v, _, _ in kag.extract(new)}
    rule_changes = []
    for key in dict.fromkeys(list(rules_new) + sorted(rules_old - set(rules_new))):
        now = kag.rule(*key)
        now = None if now is None else f"{now:g}"
        will = rules_new.get(key)
        if will is None:
            rule_changes.append({"rule": _rule_label(key), "before": now,
                                 "after": "no longer stated here (another document or the default applies)"})
        elif now is None or float(will) != float(now):
            rule_changes.append({"rule": _rule_label(key), "before": now, "after": will})
    return {"is_new": not old, "sections": len(new), "added": [t for t in after if t not in before],
            "removed": [t for t in before if t not in after], "changed": changed, "rule_changes": rule_changes}


def conflicts(name, path):
    """Rules this file states differently from another live document, and topics another document also covers."""
    from . import kag
    new = _sections_of(path, name)
    others = [s for s in policies.sections() if s["meta"]["source"] != name]
    theirs = {}
    for subj, pred, value, doc, title in kag.extract(others):
        theirs.setdefault((subj, pred), (value, doc, title))
    out, seen = [], set()
    for subj, pred, value, _, title in kag.extract(new):
        other = theirs.get((subj, pred))
        if other and other[0] != value and (subj, pred) not in seen:
            seen.add((subj, pred))
            out.append({"kind": "rule", "rule": _rule_label((subj, pred)), "here": value, "section": title,
                        "other_value": other[0], "other": f"{other[1]}, section {other[2]}"})
    topics = {}
    for s in others:
        topics.setdefault(_topic(s["meta"]["section"]), f"{s['meta']['source']}, section {s['meta']['section']}")
    for s in new:
        t = _topic(s["meta"]["section"])
        if t and t in topics and len(t) > 3:
            out.append({"kind": "topic", "section": s["meta"]["section"], "other": topics[t]})
    return out


def _rule_label(key):
    from . import kag
    subj, pred = key
    return f"{subj.capitalize()}: {kag.LABELS.get(pred, pred)}"


# ---------------------------------------------------------------- the workflow

def stage(file_name, data):
    """Stage an upload as the next version of that document and ask HR to approve it."""
    user = auth.require("policies:manage")
    name = re.sub(r"[^\w.\- ]", "_", Path(file_name).name)[:120].strip()
    if not name or Path(name).suffix.lower() not in policies.SUPPORTED or name.lower().startswith("readme"):
        raise PolicyError(f"Unsupported file {file_name!r}: use .pdf, .docx, .txt or .md")
    cur = active(name)
    if cur and cur["sha256"] == _sha(data):
        return {"name": name, "status": "unchanged", "version": cur["version"],
                "message": f"{name} is the same as the live version {cur['version']}; nothing to do."}
    _, pend, _ = _dirs()
    for old in db.q("SELECT id, approval_id FROM policy_versions WHERE name=? AND status='pending'", (name,)):
        _withdraw(old, "replaced by a newer upload")
    vid = db.x("INSERT INTO policy_versions (name, version, status, sha256, size, uploaded_by, uploaded_at) "
               "VALUES (?,?,?,?,?,?,?)", (name, _next_version(name), "pending", _sha(data), len(data), user.username, db.now()))
    path = pend / str(vid) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    try:
        changes, clashes = compare(name, path), conflicts(name, path)
    except Exception as exc:  # unreadable file: keep nothing
        shutil.rmtree(path.parent, ignore_errors=True)
        db.x("DELETE FROM policy_versions WHERE id=?", (vid,))
        raise PolicyError(f"Could not read {name}: {exc}")
    version = get(vid)["version"]
    summary = _summary(name, version, changes, clashes)
    aid = db.x("INSERT INTO approvals (kind, ref, summary, payload, requested_by, created_at) VALUES (?,?,?,?,?,?)",
               ("policy_version", str(vid), summary, json.dumps({"name": name, "version": version}), user.username, db.now()))
    db.x("UPDATE policy_versions SET changes=?, conflicts=?, approval_id=? WHERE id=?",
         (json.dumps(changes), json.dumps(clashes), aid, vid))
    db.audit(user.username, "policy.staged", {"name": name, "version": version, "approval_id": aid})
    return {**get(vid), "message": summary}


def _summary(name, version, changes, clashes):
    if changes["is_new"]:
        what = f"new document, {changes['sections']} sections"
    else:
        parts = [f"{len(changes[k])} section(s) {k}" for k in ("added", "removed", "changed") if changes[k]]
        what = ", ".join(parts) or "wording only"
    said = lambda r: (f"{r['rule'].lower()} no longer stated (was {r['before']})"  # noqa: E731
                      if str(r["after"]).startswith("no longer") else
                      f"{r['rule'].lower()} {r['before'] or 'unset'} to {r['after']}")
    rules = "; changes " + ", ".join(said(r) for r in changes["rule_changes"][:3]) if changes["rule_changes"] else ""
    clash = sum(1 for c in clashes if c["kind"] == "rule")
    return (f"Policy {name} version {version}: {what}{rules}"
            + (f"; {clash} rule(s) disagree with another document" if clash else "") + ". Approve to publish it.")


def _withdraw(row, note):
    _, pend, _ = _dirs()
    shutil.rmtree(pend / str(row["id"]), ignore_errors=True)
    db.x("UPDATE policy_versions SET status='withdrawn', note=? WHERE id=?", (note, row["id"]))
    if row["approval_id"]:
        db.x("UPDATE approvals SET status='withdrawn', decided_at=? WHERE id=? AND status='pending'", (db.now(), row["approval_id"]))


def on_decision(version_id, approve, username, note=""):
    """Called when HR approves or rejects the staged version (from Approvals or the Policies page)."""
    v = get(version_id)
    if not v or v["status"] != "pending":
        raise PolicyError("That policy version is not waiting for approval")
    root, pend, _ = _dirs()
    staged = pend / str(v["id"]) / v["name"]
    if not approve:
        shutil.rmtree(staged.parent, ignore_errors=True)
        db.x("UPDATE policy_versions SET status='rejected', decided_by=?, decided_at=?, note=? WHERE id=?",
             (username, db.now(), note, v["id"]))
        return get(v["id"])
    cur = active(v["name"])
    if cur:
        db.x("UPDATE policy_versions SET status='archived' WHERE id=?", (cur["id"],))
    live = root / v["name"]
    live.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(staged, live)
    _archive_copy(v["name"], v["version"], staged)
    shutil.rmtree(staged.parent, ignore_errors=True)
    db.x("UPDATE policy_versions SET status='active', decided_by=?, decided_at=?, activated_at=?, note=? WHERE id=?",
         (username, db.now(), db.now(), note, v["id"]))
    _reindex()
    return get(v["id"])


def decide(version_id, approve, note=""):
    """Approve or reject from the Policies page: closes the matching approval too."""
    user = auth.require("policies:manage")
    v = get(version_id)
    if not v:
        raise PolicyError("No such policy version")
    if v["approval_id"]:
        db.x("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'",
             ("approved" if approve else "rejected", user.username, db.now(), v["approval_id"]))
    out = on_decision(version_id, approve, user.username, note)
    db.audit(user.username, "policy." + ("approved" if approve else "rejected"), {"name": v["name"], "version": v["version"]})
    return out


def rollback(name, version):
    """Make an earlier version live again (the current one is archived, not lost)."""
    user = auth.require("policies:manage")
    target = db.q1("SELECT * FROM policy_versions WHERE name=? AND version=? AND status IN ('archived','retired')",
                   (name, int(version)))
    src = _archived_file(name, int(version))
    if not target or not src.exists():
        raise PolicyError(f"No archived version {version} of {name}")
    cur = active(name)
    if cur:
        db.x("UPDATE policy_versions SET status='archived' WHERE id=?", (cur["id"],))
    root, _, _ = _dirs()
    shutil.copyfile(src, root / name)
    db.x("UPDATE policy_versions SET status='active', activated_at=?, note=? WHERE id=?",
         (db.now(), f"rolled back by {user.username}", target["id"]))
    db.audit(user.username, "policy.rollback", {"name": name, "version": int(version)})
    _reindex()
    return get(target["id"])


def retire(name):
    """Take a document out of use. Its versions stay in the archive and can be rolled back."""
    user = auth.require("policies:manage")
    root, _, _ = _dirs()
    live = (root / name).resolve()
    if root.resolve() not in live.parents or not live.is_file():
        raise PolicyError(f"No policy document {name!r}")
    cur = active(name)
    if not cur:
        sync(user.username)
        cur = active(name)
    live.unlink()
    db.x("UPDATE policy_versions SET status='retired', note=? WHERE id=?", (f"removed by {user.username}", cur["id"]))
    db.audit(user.username, "policy.retired", {"name": name, "version": cur["version"]})
    _reindex()
    return {"name": name, "retired_version": cur["version"]}


def _reindex():
    from .. import automations
    return automations.reindex_policies()
