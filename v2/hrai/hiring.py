"""Hiring pipeline: resume inbox -> screening -> interview rounds (L1..Ln, HR, Final) -> offer -> joining -> follow-ups.

- HR drops resumes (.txt, .md, .pdf, .docx) into inbox/<JOB-ID>/ (or inbox/ for the only open job). The
  inbox_watch trigger, the web upload, or `app.py hiring ingest` reads them in.
- Every file becomes a candidate (duplicates are spotted by file hash and by email), is scored against the job
  and sorted into selected, on_hold or rejected. A copy of the file is placed in inbox/<JOB-ID>/sorted/<stage>/
  so the folder itself shows the result.
- Each job has its own list of rounds (default L1, L2, HR, Final; set any list, e.g. L1 L2 L3 HR Final).
  Passing a round moves the candidate to the next one; passing the last makes them ready for an offer.
- Offers need a human approval before the offer email is drafted. An accepted offer creates the new-hire record
  (which starts onboarding) and the pre-joining and post-joining follow-ups.
- Everything that happens to a candidate is logged in candidate_events, which is their timeline.
"""

import hashlib
import json
import re
import shutil
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from . import auth, config, db, triggers

STAGES = ["applied", "selected", "on_hold", "interviewing", "offer", "offer_accepted", "joined", "rejected",
          "offer_declined", "withdrawn"]
RESULTS = ("pass", "fail", "hold")
SUPPORTED = {".txt", ".md", ".pdf", ".docx"}
COMMON_SKILLS = ["python", "java", "javascript", "typescript", "react", "node", "django", "flask", "fastapi", "sql",
                 "postgresql", "mysql", "mongodb", "aws", "azure", "gcp", "docker", "kubernetes", "rest api", "graphql",
                 "spring", "go", "rust", "c++", "c#", ".net", "excel", "power bi", "tableau", "machine learning",
                 "communication", "recruitment", "payroll", "figma", "selenium", "linux", "git", "ci/cd", "kafka"]


class HiringError(ValueError):
    pass


def inbox_root() -> Path:
    path = Path(config.env("HRAI_INBOX", str(config.ROOT / "inbox")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _actor():
    return auth.current_user().username


def log(candidate_id, event, detail=""):
    db.x("INSERT INTO candidate_events (candidate_id, ts, actor, event, detail) VALUES (?,?,?,?,?)",
         (candidate_id, db.now(), _actor(), event, detail if isinstance(detail, str) else json.dumps(detail, default=str)))


# ---------------------------------------------------------------- reading files

def extract_text(path: Path):
    ext = path.suffix.lower()
    if ext in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    return None


def parse_resume(text, filename=""):
    """Pull out name, email, phone, years of experience and skills (rules; a model refines it when available)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    name = re.search(r"^name\s*[:\-]\s*(.+)$", text, re.M | re.I)
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    phone = re.search(r"(\+?\d[\d \t-]{8,}\d)", text)
    years = re.search(r"(\d+)\+?\s*(?:years|yrs)", text, re.I)
    low = text.lower()
    vocab = set(COMMON_SKILLS)
    for j in db.q("SELECT must_have, nice_to_have FROM jobs"):
        vocab |= set(json.loads(j["must_have"])) | set(json.loads(j["nice_to_have"]))
    skills = sorted(s for s in vocab if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", low))
    lines = [l.lstrip("#*- ").strip() for l in lines]
    guess = name.group(1).strip() if name else (lines[0] if lines and len(lines[0]) < 60 and "@" not in lines[0]
                                                 else Path(filename).stem.replace("_", " ").title())
    return {"name": guess, "email": email.group(0) if email else None,
            "phone": re.sub(r"\s+", " ", phone.group(1)).strip() if phone else None,
            "years": int(years.group(1)) if years else 0, "skills": skills}


# ---------------------------------------------------------------- ingest and screen

def _next_id(table, prefix):
    rows = db.q(f"SELECT id FROM {table} WHERE id LIKE ?", (f"{prefix}-%",))
    nums = [int(r["id"].split("-")[1]) for r in rows if r["id"].split("-")[1].isdigit()]
    return f"{prefix}-{(max(nums) if nums else 0) + 1:03d}"


def _job_for(folder: Path, job_id):
    if job_id:
        job = db.q1("SELECT * FROM jobs WHERE upper(id)=upper(?)", (job_id,))
    else:
        job = db.q1("SELECT * FROM jobs WHERE upper(id)=upper(?)", (folder.name,))
        if not job:
            open_jobs = db.q("SELECT * FROM jobs WHERE COALESCE(status,'open')='open'")
            job = open_jobs[0] if len(open_jobs) == 1 else None
    return job


def ingest(folder=None, job_id=None):
    """Read every new resume under the inbox (or `folder`) into the pipeline and screen it."""
    root = Path(folder) if folder else inbox_root()
    report = {"added": [], "duplicates": [], "unreadable": [], "no_job": []}
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED):
        if "sorted" in path.relative_to(root).parts or path.name.startswith("."):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if db.q1("SELECT 1 AS y FROM candidates WHERE file_hash=?", (digest,)):
            continue  # already ingested this exact file: stay quiet on every sweep
        job = _job_for(path.parent, job_id)
        if not job:
            report["no_job"].append(path.name)
            continue
        try:
            text = extract_text(path) or ""
        except Exception as exc:
            report["unreadable"].append(f"{path.name}: {exc}")
            continue
        if len(text.strip()) < 20:
            report["unreadable"].append(f"{path.name}: no text found (scanned image?)")
            continue
        info = parse_resume(text, path.name)
        dup = info["email"] and db.q1("SELECT id FROM candidates WHERE lower(email)=lower(?) AND job_id=?", (info["email"], job["id"]))
        if dup:
            report["duplicates"].append(f"{path.name} (same email as {dup['id']})")
            db.x("UPDATE candidates SET file_hash=COALESCE(file_hash, ?) WHERE id=?", (digest, dup["id"]))
            log(dup["id"], "duplicate_resume", path.name)
            continue
        cid = _next_id("candidates", "C")
        db.x("INSERT INTO candidates (id, file_name, name, email, phone, skills, years, resume_text, job_id, file_hash, "
             "source_path, stage, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (cid, path.name, info["name"], info["email"], info["phone"], json.dumps(info["skills"]), info["years"], text,
              job["id"], digest, str(path), "applied", db.now(), db.now()))
        log(cid, "applied", f"from {path.name}")
        triggers.emit("candidate.added", {"candidate_id": cid})
        result = screen(cid)
        report["added"].append({"candidate_id": cid, "name": info["name"], "stage": result["stage"], "score": result["score"]})
    return report


def score(c, job):
    must, nice = json.loads(job["must_have"]), json.loads(job["nice_to_have"])
    text = (c["resume_text"] or "").lower()
    years = c.get("years")
    if not years:
        m = re.search(r"(\d+)\+?\s*(?:years|yrs)", text)
        years = int(m.group(1)) if m else 0
    must_hit = [s for s in must if s in text]
    nice_hit = [s for s in nice if s in text]
    pts = round(60 * len(must_hit) / max(len(must), 1) + 24 * len(nice_hit) / max(len(nice), 1)
                + 16 * min(years / max(job["min_years"], 1), 1))
    return {"score": pts, "years": years, "must_missing": [s for s in must if s not in must_hit],
            "nice_hit": nice_hit, "meets_minimum": len(must_hit) == len(must) and years >= job["min_years"]}


def screen(candidate_id):
    """Score a candidate and sort them: rejected (misses a must-have or the minimum years), selected (score at or
    above the job's threshold), or on_hold (meets the minimum but scores lower: HR decides)."""
    c = get(candidate_id)
    job = db.q1("SELECT * FROM jobs WHERE id=?", (c["job_id"],))
    s = score(c, job)
    threshold = job.get("select_threshold") or 70
    if not s["meets_minimum"]:
        stage = "rejected"
        note = (f"Missing must-have: {', '.join(s['must_missing'])}" if s["must_missing"]
                else f"{s['years']} years; job needs {job['min_years']}")
    elif s["score"] >= threshold:
        stage, note = "selected", f"Score {s['score']} (threshold {threshold})"
    else:
        stage, note = "on_hold", f"Meets minimum but score {s['score']} is under {threshold}; HR to review"
    db.x("UPDATE candidates SET score=?, years=?, decision=?, stage=?, status_note=?, updated_at=? WHERE id=?",
         (s["score"], s["years"], "shortlist" if stage == "selected" else "decline" if stage == "rejected" else "hold",
          stage, note, db.now(), candidate_id))
    log(candidate_id, f"screened_{stage}", note)
    _file_copy(candidate_id, stage)
    return {"candidate_id": candidate_id, "stage": stage, "score": s["score"], "note": note}


def _file_copy(candidate_id, stage):
    """Mirror the pipeline in the folder: inbox/<JOB>/sorted/<stage>/<file>."""
    c = get(candidate_id)
    src = Path(c["source_path"]) if c.get("source_path") else None
    if not src or not src.exists():
        return
    base = inbox_root() / c["job_id"] / "sorted"
    for old in base.glob(f"*/{src.name}"):
        old.unlink()
    target = base / stage
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target / src.name)


# ---------------------------------------------------------------- lookups

def get(candidate_ref):
    c = db.q1("SELECT * FROM candidates WHERE upper(id)=upper(?)", (candidate_ref,))
    if not c:
        matches = db.q("SELECT * FROM candidates WHERE lower(name) LIKE ?", (f"%{candidate_ref.lower().strip()}%",))
        if len(matches) == 1:
            c = matches[0]
        elif len(matches) > 1:
            raise HiringError(f"'{candidate_ref}' matches {len(matches)} candidates: "
                              + ", ".join(f"{m['id']} {m['name']}" for m in matches))
    if not c:
        raise HiringError(f"No candidate {candidate_ref}")
    return c


def rounds(job_id):
    job = db.q1("SELECT rounds FROM jobs WHERE id=?", (job_id,))
    return json.loads(job["rounds"]) if job and job["rounds"] else ["L1", "L2", "HR", "Final"]


def set_rounds(job_id, round_names):
    names = [r.strip() for r in round_names if r.strip()]
    if not names:
        raise HiringError("Give at least one round, e.g. L1 L2 L3 HR Final")
    db.x("UPDATE jobs SET rounds=? WHERE id=?", (json.dumps(names), job_id))
    return names


def current_round(candidate_id):
    row = db.q1("SELECT * FROM interviews WHERE candidate_id=? ORDER BY id DESC LIMIT 1", (candidate_id,))
    return row


def next_round(c):
    names = rounds(c["job_id"])
    passed = [r["round"] for r in db.q("SELECT round FROM interviews WHERE candidate_id=? AND result='pass'", (c["id"],))]
    return next((r for r in names if r not in passed), None)


# ---------------------------------------------------------------- moving through the pipeline

def move(candidate_ref, stage, note=""):
    if stage not in STAGES:
        raise HiringError(f"Unknown stage {stage}; use one of {', '.join(STAGES)}")
    c = get(candidate_ref)
    db.x("UPDATE candidates SET stage=?, status_note=?, updated_at=? WHERE id=?", (stage, note or c["status_note"], db.now(), c["id"]))
    log(c["id"], f"moved_{stage}", note)
    if stage in ("rejected", "withdrawn", "offer_declined"):
        db.x("UPDATE followups SET status='done', done_at=? WHERE candidate_id=? AND status='open'", (db.now(), c["id"]))
    _file_copy(c["id"], stage)
    return {"candidate_id": c["id"], "stage": stage}


def schedule(candidate_ref, round_name=None, when=None, interviewer="", mode="Video call"):
    c = get(candidate_ref)
    if c["stage"] not in ("selected", "interviewing", "on_hold"):
        raise HiringError(f"{c['name']} is {c['stage']}; only selected or interviewing candidates can be scheduled")
    round_name = round_name or next_round(c)
    if round_name not in rounds(c["job_id"]):
        raise HiringError(f"{round_name} is not a round for {c['job_id']}: {', '.join(rounds(c['job_id']))}")
    when = when or (datetime.combine(config.today() + timedelta(days=2), datetime.min.time()).replace(hour=11)).isoformat(timespec="minutes")
    datetime.fromisoformat(when)  # validates
    iid = db.x("INSERT INTO interviews (candidate_id, job_id, round, scheduled_at, interviewer, mode, status, created_at, "
               "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (c["id"], c["job_id"], round_name, when, interviewer, mode, "scheduled", db.now(), db.now()))
    db.x("UPDATE candidates SET stage='interviewing', status_note=?, updated_at=? WHERE id=?",
         (f"{round_name} scheduled {when}", db.now(), c["id"]))
    log(c["id"], "interview_scheduled", f"{round_name} at {when} with {interviewer or 'TBD'} ({mode})")
    _file_copy(c["id"], "interviewing")
    job = db.q1("SELECT title FROM jobs WHERE id=?", (c["job_id"],))
    _draft(c["email"], f"{round_name} interview: {job['title']}",
           f"Dear {c['name']},\n\nYour {round_name} interview for {job['title']} is scheduled on {when.replace('T', ' at ')} "
           f"({mode}){' with ' + interviewer if interviewer else ''}. Please confirm by replying to this email.\n\nRegards,\nTalent Acquisition")
    _settle(c["id"], "schedule_next_round")
    add_followup(c["id"], when[:10], "interview_reminder", f"Remind {c['name']} and {interviewer or 'the panel'} about {round_name}",
                 offset_days=-1)
    return {"interview_id": iid, "candidate_id": c["id"], "round": round_name, "scheduled_at": when}


def record_result(candidate_ref, round_name, result, rating=None, feedback=""):
    """Record an interview outcome. pass -> next round (or ready for offer after the last); fail -> rejected; hold -> on hold."""
    if result not in RESULTS:
        raise HiringError("result must be pass, fail or hold")
    c = get(candidate_ref)
    iv = db.q1("SELECT * FROM interviews WHERE candidate_id=? AND round=? ORDER BY id DESC LIMIT 1", (c["id"], round_name))
    if not iv:  # result recorded without a scheduled slot (walk-in or scheduled elsewhere)
        iv_id = db.x("INSERT INTO interviews (candidate_id, job_id, round, scheduled_at, status, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?)", (c["id"], c["job_id"], round_name, db.now(), "completed", db.now(), db.now()))
    else:
        iv_id = iv["id"]
    if rating is not None and not 1 <= int(rating) <= 5:
        raise HiringError("rating must be 1 to 5")
    db.x("UPDATE interviews SET status='completed', result=?, rating=?, feedback=?, updated_at=? WHERE id=?",
         (result, rating, feedback, db.now(), iv_id))
    log(c["id"], f"{round_name}_{result}", f"rating {rating or '-'}: {feedback}")
    _settle(c["id"], "interview_reminder", f"about {round_name}")
    if result == "fail":
        move(c["id"], "rejected", f"Did not clear {round_name}")
        job = db.q1("SELECT title FROM jobs WHERE id=?", (c["job_id"],))
        _draft(c["email"], f"Your application for {job['title']}",
               f"Dear {c['name']},\n\nThank you for your time in the {round_name} interview. We will not be moving forward "
               "at this time, and we wish you the very best.\n\nRegards,\nTalent Acquisition")
        return {"candidate_id": c["id"], "stage": "rejected"}
    if result == "hold":
        move(c["id"], "on_hold", f"On hold after {round_name}")
        return {"candidate_id": c["id"], "stage": "on_hold"}
    nxt = next_round(get(c["id"]))
    if nxt:
        db.x("UPDATE candidates SET stage='interviewing', status_note=?, updated_at=? WHERE id=?",
             (f"Cleared {round_name}; next {nxt}", db.now(), c["id"]))
        add_followup(c["id"], (config.today() + timedelta(days=2)).isoformat(), "schedule_next_round",
                     f"Schedule {nxt} for {c['name']}")
        return {"candidate_id": c["id"], "stage": "interviewing", "next_round": nxt}
    move(c["id"], "offer", f"Cleared all rounds ({', '.join(rounds(c['job_id']))})")
    add_followup(c["id"], (config.today() + timedelta(days=1)).isoformat(), "make_offer", f"Prepare the offer for {c['name']}")
    return {"candidate_id": c["id"], "stage": "offer", "next_round": None}


def make_offer(candidate_ref, ctc_lpa, joining_date):
    c = get(candidate_ref)
    if c["stage"] != "offer":
        raise HiringError(f"{c['name']} is {c['stage']}; offers are made after the final round")
    date.fromisoformat(joining_date)
    oid = db.x("INSERT INTO offers (candidate_id, job_id, ctc_lpa, joining_date, status, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?,?)", (c["id"], c["job_id"], float(ctc_lpa), joining_date, "pending_approval", db.now(), db.now()))
    aid = db.x("INSERT INTO approvals (kind, ref, summary, payload, requested_by, created_at) VALUES (?,?,?,?,?,?)",
               ("offer", str(oid), f"Offer for {c['name']} ({c['job_id']}): {ctc_lpa} LPA, joining {joining_date}",
                json.dumps({"offer_id": oid}), _actor(), db.now()))
    db.x("UPDATE offers SET approval_id=? WHERE id=?", (aid, oid))
    _settle(c["id"], "make_offer")
    log(c["id"], "offer_requested", f"{ctc_lpa} LPA, joining {joining_date}; approval #{aid}")
    return {"offer_id": oid, "approval_id": aid, "status": "pending_approval"}


def on_offer_decision(offer_id, approved):
    """Called by the approvals queue."""
    o = db.q1("SELECT * FROM offers WHERE id=?", (offer_id,))
    c = get(o["candidate_id"])
    if not approved:
        db.x("UPDATE offers SET status='rejected_internally', updated_at=? WHERE id=?", (db.now(), offer_id))
        log(c["id"], "offer_not_approved")
        return
    db.x("UPDATE offers SET status='sent', updated_at=? WHERE id=?", (db.now(), offer_id))
    job = db.q1("SELECT title FROM jobs WHERE id=?", (c["job_id"],))
    _draft(c["email"], f"Offer of employment: {job['title']}",
           f"Dear {c['name']},\n\nWe are delighted to offer you the role of {job['title']} at a CTC of {o['ctc_lpa']} lakh per "
           f"annum, with a joining date of {o['joining_date']}. The detailed offer letter is attached. Please confirm your "
           "acceptance within 5 working days.\n\nRegards,\nHR")
    log(c["id"], "offer_sent", f"{o['ctc_lpa']} LPA")
    add_followup(c["id"], (config.today() + timedelta(days=3)).isoformat(), "offer_response", f"Check {c['name']}'s response to the offer")


def offer_response(candidate_ref, accepted, joining_date=None):
    c = get(candidate_ref)
    o = db.q1("SELECT * FROM offers WHERE candidate_id=? AND status='sent' ORDER BY id DESC LIMIT 1", (c["id"],))
    if not o:
        raise HiringError(f"No sent offer for {c['name']}")
    _settle(c["id"], "offer_response")
    if not accepted:
        db.x("UPDATE offers SET status='declined', updated_at=? WHERE id=?", (db.now(), o["id"]))
        return move(c["id"], "offer_declined", "Declined the offer")
    joining = joining_date or o["joining_date"]
    date.fromisoformat(joining)
    db.x("UPDATE offers SET status='accepted', joining_date=?, updated_at=? WHERE id=?", (joining, db.now(), o["id"]))
    move(c["id"], "offer_accepted", f"Joining {joining}")
    job = db.q1("SELECT * FROM jobs WHERE id=?", (c["job_id"],))
    hire_id = _next_id("new_hires", "NH")
    db.x("INSERT INTO new_hires (id, name, email, role, department, manager, start_date, documents, candidate_id) "
         "VALUES (?,?,?,?,?,?,?,?,?)", (hire_id, c["name"], c["email"], job["title"], config.env("HRAI_DEFAULT_DEPT", "Engineering"),
                                       config.env("HRAI_DEFAULT_MANAGER", "Hiring manager"), joining, "[]", c["id"]))
    for offset, kind, note in [(-7, "pre_joining_call", "Pre-joining call: confirm joining date, answer questions"),
                               (-3, "documents_check", "Check onboarding documents are in"),
                               (0, "day_one", "Day-1 welcome and induction"), (30, "check_in_30", "30-day check-in"),
                               (90, "probation_review", "90-day probation review")]:
        add_followup(c["id"], joining, kind, note, offset_days=offset)
    triggers.emit("new_hire.created", {"hire_id": hire_id})
    log(c["id"], "new_hire_created", hire_id)
    return {"candidate_id": c["id"], "stage": "offer_accepted", "new_hire_id": hire_id, "joining_date": joining}


def mark_joined(candidate_ref):
    c = get(candidate_ref)
    if c["stage"] != "offer_accepted":
        raise HiringError(f"{c['name']} is {c['stage']}, not offer_accepted")
    return move(c["id"], "joined", f"Joined on {config.today().isoformat()}")


# ---------------------------------------------------------------- follow-ups

def _settle(candidate_id, kind, note_suffix=None):
    """Close open follow-ups that an action just made moot (a reminder for an interview that happened, and so on)."""
    sql, args = "UPDATE followups SET status='done', done_at=? WHERE candidate_id=? AND kind=? AND status='open'", \
        [db.now(), candidate_id, kind]
    if note_suffix:
        sql += " AND note LIKE ?"
        args.append(f"%{note_suffix}")
    db.x(sql, args)


def add_followup(candidate_id, base_date, kind, note, offset_days=0):
    due = (date.fromisoformat(base_date[:10]) + timedelta(days=offset_days)).isoformat()
    return db.x("INSERT INTO followups (candidate_id, due, kind, note, status, created_at) VALUES (?,?,?,?,?,?)",
                (candidate_id, due, kind, note, "open", db.now()))


def followups(days_ahead=7, include_done=False):
    until = (config.today() + timedelta(days=days_ahead)).isoformat()
    sql = ("SELECT f.*, c.name, c.stage FROM followups f JOIN candidates c ON c.id=f.candidate_id WHERE f.due<=?"
           + ("" if include_done else " AND f.status='open'") + " ORDER BY f.due")
    return db.q(sql, (until,))


def complete_followup(followup_id, note=""):
    f = db.q1("SELECT * FROM followups WHERE id=?", (followup_id,))
    if not f:
        raise HiringError(f"No follow-up {followup_id}")
    db.x("UPDATE followups SET status='done', done_at=? WHERE id=?", (db.now(), followup_id))
    log(f["candidate_id"], "followup_done", f"{f['kind']}: {note}")
    return {"followup_id": followup_id, "status": "done"}


# ---------------------------------------------------------------- views

def timeline(candidate_ref):
    c = get(candidate_ref)
    c.pop("resume_text", None)
    c["skills"] = json.loads(c["skills"]) if c.get("skills") else []
    return {"candidate": c, "rounds": rounds(c["job_id"]), "next_round": next_round(c),
            "interviews": db.q("SELECT * FROM interviews WHERE candidate_id=? ORDER BY id", (c["id"],)),
            "offers": db.q("SELECT * FROM offers WHERE candidate_id=? ORDER BY id", (c["id"],)),
            "followups": db.q("SELECT * FROM followups WHERE candidate_id=? ORDER BY due", (c["id"],)),
            "events": db.q("SELECT ts, actor, event, detail FROM candidate_events WHERE candidate_id=? ORDER BY id", (c["id"],))}


def board(job_id=None):
    """Kanban columns: applied, selected, on_hold, one per round, offer, offer_accepted, joined, rejected/declined."""
    jobs = db.q("SELECT * FROM jobs" + (" WHERE id=?" if job_id else ""), (job_id,) if job_id else ())
    out = []
    for job in jobs:
        names = rounds(job["id"])
        cols = {k: [] for k in ["applied", "selected", "on_hold", *names, "offer", "offer_accepted", "joined", "rejected"]}
        for c in db.q("SELECT id, name, email, score, stage, status_note, updated_at FROM candidates WHERE job_id=? "
                      "ORDER BY COALESCE(score,0) DESC", (job["id"],)):
            col = c["stage"]
            if col == "interviewing":
                last = current_round(c["id"])
                col = last["round"] if last and last["result"] != "pass" else (next_round(get(c["id"])) or names[-1])
            elif col in ("offer_declined", "withdrawn"):
                col = "rejected"
            cols.setdefault(col, []).append(c)
        out.append({"job_id": job["id"], "title": job["title"], "rounds": names,
                    "columns": [{"key": k, "count": len(v), "candidates": v} for k, v in cols.items()]})
    return out


def summary(job_id=None):
    lines = []
    for b in board(job_id):
        counts = ", ".join(f"{c['key']} {c['count']}" for c in b["columns"] if c["count"])
        lines.append(f"{b['title']} ({b['job_id']}), rounds {' > '.join(b['rounds'])}: {counts or 'no candidates yet'}")
    due = followups(0)
    if due:
        lines.append(f"{len(due)} follow-up(s) due today or overdue: " + "; ".join(f"{f['name']}: {f['note']}" for f in due[:5]))
    return "\n".join(lines)


def _draft(to, subject, body):
    from . import tools as T
    if to:
        T.run("draft_email", to=to, subject=subject, body=body)
