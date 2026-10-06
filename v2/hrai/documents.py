"""Employee and new-hire documents: uploaded at the step of the process that needs them, kept in one folder per
person and document type, and reviewed by HR.

  documents/new-hires/<NH-ID>/<type>/<uploaded-at>__<file>     onboarding: offer letter, ID, PAN, bank details...
  documents/employees/<E-ID>/<type>/<uploaded-at>__<file>      during employment: medical certificates for sick
                                                               leave, investment proofs for payroll, updates

Nothing is overwritten: a second upload of the same type is kept next to the first, and the newest one counts.
Uploading an onboarding document marks it submitted for the new hire (so the onboarding agent and the morning
reminder stop asking for it); if HR rejects it, it is asked for again. The folder is git-ignored: it holds
personal data. Set HRAI_DOCS_DIR to keep it elsewhere.
"""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from . import auth, config, db

NEW_HIRE_TYPES = ["offer_letter_signed", "id_proof", "pan_card", "address_proof", "bank_details",
                  "education_certificates", "nda_signed", "relieving_letter"]
EMPLOYEE_TYPES = ["medical_certificate", "investment_proof", "id_proof", "pan_card", "address_proof", "bank_details",
                  "education_certificates", "resignation_letter", "other"]
LABELS = {"offer_letter_signed": "Signed offer letter", "id_proof": "Government ID proof", "pan_card": "PAN card",
          "address_proof": "Address proof", "bank_details": "Bank account details",
          "education_certificates": "Education certificates", "nda_signed": "Signed NDA",
          "relieving_letter": "Relieving letter (previous employer)", "medical_certificate": "Medical certificate",
          "investment_proof": "Investment proof (tax)", "resignation_letter": "Resignation letter", "other": "Other"}
ALLOWED = {".pdf", ".docx", ".jpg", ".jpeg", ".png", ".txt"}
MAX_BYTES = 5_000_000
KINDS = {"new_hire": "new-hires", "employee": "employees"}


class DocumentError(ValueError):
    pass


def root() -> Path:
    path = Path(config.env("HRAI_DOCS_DIR", str(config.ROOT / "documents")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _owner(owner_id):
    """('new_hire', row) or ('employee', row) for an id like NH-201 or E101."""
    oid = (owner_id or "").strip().upper()
    if oid.startswith("NH-"):
        row = db.q1("SELECT * FROM new_hires WHERE id=?", (oid,))
        if row:
            return "new_hire", row
    row = db.q1("SELECT * FROM employees WHERE id=?", (oid,))
    if row:
        return "employee", row
    raise DocumentError(f"No employee or new hire {owner_id!r}")


def _may_see(kind, oid):
    user = auth.current_user()
    if user.can("documents:manage"):
        return True
    if kind == "employee" and user.employee_id == oid:
        return True
    # an employee also sees the documents they gave as a new hire
    return kind == "new_hire" and bool(user.employee_id) and bool(
        db.q1("SELECT 1 AS y FROM new_hires WHERE id=? AND employee_id=?", (oid, user.employee_id)))


def _check(kind, oid):
    if not _may_see(kind, oid):
        raise PermissionError("You can only see and upload your own documents")


def types_for(kind):
    return NEW_HIRE_TYPES if kind == "new_hire" else EMPLOYEE_TYPES


def save(owner_id, doc_type, file_name, data, note=""):
    """Store one uploaded file in its person's and type's folder and record it."""
    kind, row = _owner(owner_id)
    _check(kind, row["id"])
    if doc_type not in types_for(kind):
        raise DocumentError(f"Pick a document type: {', '.join(types_for(kind))}")
    name = re.sub(r"[^\w.\- ]", "_", Path(file_name or "").name)[:120].strip()
    if not name or Path(name).suffix.lower() not in ALLOWED:
        raise DocumentError(f"Unsupported file {file_name!r}: use {', '.join(sorted(ALLOWED))}")
    if not data:
        raise DocumentError(f"{name} is empty")
    if len(data) > MAX_BYTES:
        raise DocumentError(f"{name} is larger than 5 MB")
    sha = hashlib.sha256(data).hexdigest()
    dup = db.q1("SELECT id FROM documents WHERE owner_id=? AND doc_type=? AND sha256=? AND status!='rejected'",
                (row["id"], doc_type, sha))
    if dup:
        return {**get(dup["id"]), "duplicate": True}
    folder = root() / KINDS[kind] / row["id"] / doc_type
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}__{name}"
    path.write_bytes(data)
    user = auth.current_user()
    did = db.x("INSERT INTO documents (owner_kind, owner_id, doc_type, file_name, path, sha256, size, status, note, "
               "uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (kind, row["id"], doc_type, name, str(path.relative_to(root())), sha, len(data), "received", note,
                user.username, db.now()))
    if kind == "new_hire":
        _mark_submitted(row["id"])
    db.audit(user.username, "document.uploaded", {"owner": row["id"], "type": doc_type, "file": name})
    return get(did)


def _mark_submitted(hire_id):
    """A new hire's submitted list = what was declared before uploads existed, plus every type with a file that
    HR has not rejected (a rejected type with no other file is asked for again)."""
    h = db.q1("SELECT documents FROM new_hires WHERE id=?", (hire_id,))
    declared = set(json.loads(h["documents"] or "[]"))
    files = db.q("SELECT doc_type, status FROM documents WHERE owner_id=?", (hire_id,))
    good = {f["doc_type"] for f in files if f["status"] != "rejected"}
    bad = {f["doc_type"] for f in files if f["status"] == "rejected"} - good
    have = [t for t in NEW_HIRE_TYPES if t in (declared | good) - bad]
    db.x("UPDATE new_hires SET documents=? WHERE id=?", (json.dumps(have), hire_id))
    _update_onboarding_tasks(hire_id, set(NEW_HIRE_TYPES) - set(have))


# onboarding tasks that wait for a document (see create_onboarding_plan in tools.py)
NEEDS = {"Create email": {"id_proof"}, "Set up payroll": {"bank_details", "pan_card"}}


def _update_onboarding_tasks(hire_id, missing):
    """Unblock a task once its documents are in (or block it again if one was rejected)."""
    for t in db.q("SELECT id, task, status FROM onboarding_tasks WHERE hire_id=?", (hire_id,)):
        needs = next((v for k, v in NEEDS.items() if t["task"].startswith(k)), None)
        if needs is not None:
            status = "blocked: missing documents" if needs & missing else "to do"
            if t["status"] in ("to do", "blocked: missing documents") and t["status"] != status:
                db.x("UPDATE onboarding_tasks SET status=? WHERE id=?", (status, t["id"]))
        elif t["task"] == "Collect missing documents" and not missing and t["status"] != "done":
            db.x("UPDATE onboarding_tasks SET status='done' WHERE id=?", (t["id"],))


def get(doc_id):
    d = db.q1("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not d:
        raise DocumentError("No such document")
    _check(d["owner_kind"], d["owner_id"])
    return {**d, "label": LABELS.get(d["doc_type"], d["doc_type"])}


def file_of(doc_id):
    d = get(doc_id)
    path = (root() / d["path"]).resolve()
    if root().resolve() not in path.parents or not path.is_file():
        raise DocumentError("The file is missing from the documents folder")
    return d, path.read_bytes()


def checklist(owner_id):
    """Every document type for this person: the newest file (if any), its status, and what is still missing."""
    kind, row = _owner(owner_id)
    _check(kind, row["id"])
    files = db.q("SELECT * FROM documents WHERE owner_id=? ORDER BY id DESC", (row["id"],))
    declared = set(json.loads(row["documents"] or "[]")) if kind == "new_hire" else set()
    items = []
    for t in types_for(kind):
        mine = [f for f in files if f["doc_type"] == t]
        latest = mine[0] if mine else None
        state = latest["status"] if latest else ("declared" if t in declared else "missing")
        items.append({"type": t, "label": LABELS[t], "status": state, "files": [
            {k: f[k] for k in ("id", "file_name", "status", "note", "uploaded_by", "uploaded_at", "size")} for f in mine]})
    required = NEW_HIRE_TYPES if kind == "new_hire" else []
    return {"owner_id": row["id"], "kind": kind, "name": row["name"], "items": items,
            "missing": [i["type"] for i in items if i["type"] in required and i["status"] in ("missing", "rejected")],
            "start_date": row.get("start_date"), "employee_id": row.get("employee_id") if kind == "new_hire" else row["id"]}


def review(doc_id, status, note=""):
    """HR marks a document verified, or rejects it (it is then asked for again)."""
    user = auth.require("documents:manage")
    if status not in ("verified", "rejected"):
        raise DocumentError("status must be verified or rejected")
    d = get(doc_id)
    db.x("UPDATE documents SET status=?, note=?, reviewed_by=?, reviewed_at=? WHERE id=?",
         (status, note or d["note"], user.username, db.now(), doc_id))
    if d["owner_kind"] == "new_hire":
        _mark_submitted(d["owner_id"])
    db.audit(user.username, f"document.{status}", {"id": doc_id, "owner": d["owner_id"], "type": d["doc_type"]})
    return get(doc_id)


def people():
    """Who HR can upload for: new hires (with how many required documents are missing) and employees."""
    auth.require("documents:manage")
    hires = []
    for h in db.q("SELECT id, name, role, start_date, employee_id FROM new_hires ORDER BY start_date"):
        hires.append({**h, "missing": len(checklist(h["id"])["missing"])})
    emps = db.q("SELECT e.id, e.name, e.department, COUNT(d.id) AS files FROM employees e "
                "LEFT JOIN documents d ON d.owner_id=e.id GROUP BY e.id ORDER BY e.name")
    pending = db.q1("SELECT COUNT(*) AS n FROM documents WHERE status='received'")["n"]
    return {"new_hires": hires, "employees": emps, "to_review": pending}
