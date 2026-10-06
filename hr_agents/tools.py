"""Deterministic HR tools the agents call.

Every tool takes plain JSON-able arguments and returns a JSON-able dict, so the
same functions serve both the Claude tool-use loop and the offline mock planner.
Nothing here sends email or changes HR records: outbound messages are written to
outputs/outbox as drafts for a human to review.
"""

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "outputs"

REQUIRED_DOCUMENTS = [
    "offer_letter_signed", "id_proof", "pan_card", "address_proof",
    "bank_details", "education_certificates", "nda_signed", "relieving_letter",
]


def today() -> date:
    """Demo clock; set HR_DEMO_TODAY=YYYY-MM-DD to pin it."""
    pinned = os.environ.get("HR_DEMO_TODAY")
    return date.fromisoformat(pinned) if pinned else date.today()


def _load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _write(rel_path, text):
    path = OUT / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path.relative_to(ROOT))


# ---------------------------------------------------------------- screening

def list_job_openings():
    return {"jobs": _load("job_openings.json")}


def list_resumes():
    return {"resumes": sorted(p.name for p in (DATA / "resumes").glob("*.txt"))}


def read_resume(file_name):
    path = DATA / "resumes" / Path(file_name).name
    if not path.exists():
        return {"error": f"No resume named {file_name}"}
    return {"file_name": path.name, "text": path.read_text(encoding="utf-8")}


def score_resume(file_name, job_id):
    """Score one resume against a job: 60 must-have, 24 nice-to-have, 16 experience."""
    job = next((j for j in _load("job_openings.json") if j["id"] == job_id), None)
    if not job:
        return {"error": f"No job {job_id}"}
    resume = read_resume(file_name)
    if "error" in resume:
        return resume
    text = resume["text"].lower()
    name = re.search(r"^name:\s*(.+)$", resume["text"], re.M | re.I)
    email = re.search(r"^email:\s*(.+)$", resume["text"], re.M | re.I)
    years = re.search(r"years of experience:\s*(\d+)", text)
    years = int(years.group(1)) if years else 0

    must_hit = [s for s in job["must_have"] if s in text]
    nice_hit = [s for s in job["nice_to_have"] if s in text]
    must_pts = 60 * len(must_hit) / len(job["must_have"])
    nice_pts = 24 * len(nice_hit) / len(job["nice_to_have"])
    exp_pts = 16 * min(years / job["min_years"], 1.0)
    missing = [s for s in job["must_have"] if s not in must_hit]
    return {
        "file_name": resume["file_name"],
        "candidate": name.group(1).strip() if name else file_name,
        "email": email.group(1).strip() if email else None,
        "years_experience": years,
        "score": round(must_pts + nice_pts + exp_pts),
        "must_have_matched": must_hit,
        "must_have_missing": missing,
        "nice_to_have_matched": nice_hit,
        "meets_minimum": not missing and years >= job["min_years"],
    }


def save_shortlist(job_id, ranked_candidates, notes=""):
    """Write the ranked shortlist report for HR review."""
    job = next(j for j in _load("job_openings.json") if j["id"] == job_id)
    lines = [f"# Shortlist: {job['title']} ({job_id})", "",
             f"Generated {datetime.now():%Y-%m-%d %H:%M} by the screening agent. Draft for HR review.", "",
             "| Rank | Candidate | Score | Years | Missing must-haves | Decision |",
             "| --- | --- | --- | --- | --- | --- |"]
    for i, c in enumerate(ranked_candidates, 1):
        lines.append(f"| {i} | {c['candidate']} | {c['score']} | {c.get('years_experience', '')} | "
                     f"{', '.join(c.get('must_have_missing', [])) or 'none'} | {c.get('decision', '')} |")
    if notes:
        lines += ["", "## Agent notes", "", notes]
    return {"saved_to": _write(f"shortlist_{job_id}.md", "\n".join(lines) + "\n")}


# ---------------------------------------------------------------- onboarding

def get_new_hire(hire_id_or_name):
    key = hire_id_or_name.lower()
    for h in _load("new_hires.json"):
        if key in (h["id"].lower(), h["name"].lower()) or key in h["name"].lower():
            return h
    return {"error": f"No new hire matching {hire_id_or_name}",
            "known": [f"{h['id']} {h['name']}" for h in _load("new_hires.json")]}


def check_documents(hire_id):
    hire = get_new_hire(hire_id)
    if "error" in hire:
        return hire
    missing = [d for d in REQUIRED_DOCUMENTS if d not in hire["documents_submitted"]]
    return {"hire_id": hire["id"], "submitted": hire["documents_submitted"],
            "missing": missing, "payroll_blocked": any(d in missing for d in ("bank_details", "pan_card"))}


def create_onboarding_plan(hire_id):
    """Build the dated checklist from the start date; tasks that need missing documents are marked blocked."""
    hire = get_new_hire(hire_id)
    if "error" in hire:
        return hire
    docs = check_documents(hire["id"])
    start = date.fromisoformat(hire["start_date"])
    steps = [
        (-7, "HR", "Send welcome email with joining details and document checklist", None),
        (-5, "IT", f"Create email, Slack and HRMS accounts; ship laptop for {hire['role']}", "id_proof"),
        (-3, "Payroll", "Set up payroll and statutory registrations", "bank_details"),
        (-3, "HR", "Collect missing documents", None),
        (-1, hire["manager"], "Assign onboarding buddy and first-week goals", None),
        (0, "HR", "Day-1 orientation: policies, benefits, security training", None),
        (1, hire["manager"], "Team introduction and role walkthrough", None),
        (30, hire["manager"], "30-day check-in", None),
        (90, "HR", "90-day probation review", None),
    ]
    tasks = []
    for offset, owner, task, needs in steps:
        if task.startswith("Collect missing") and not docs["missing"]:
            continue
        blocked = needs in docs["missing"] if needs else False
        if needs == "bank_details" and docs["payroll_blocked"]:
            blocked = True
        tasks.append({"due": (start + timedelta(days=offset)).isoformat(), "owner": owner,
                      "task": task, "status": "blocked: missing documents" if blocked else "to do"})
    lines = [f"# Onboarding plan: {hire['name']} ({hire['id']})", "",
             f"Role: {hire['role']}, {hire['department']}. Manager: {hire['manager']}. Start date: {hire['start_date']}.", "",
             f"Missing documents: {', '.join(docs['missing']) or 'none'}", "",
             "| Due | Owner | Task | Status |", "| --- | --- | --- | --- |"]
    lines += [f"| {t['due']} | {t['owner']} | {t['task']} | {t['status']} |" for t in tasks]
    saved = _write(f"onboarding_{hire['id']}.md", "\n".join(lines) + "\n")
    return {"hire_id": hire["id"], "tasks": tasks, "saved_to": saved}


# ---------------------------------------------------------------- shared

def draft_email(to, subject, body):
    """Save an email draft to outputs/outbox. Nothing is sent."""
    slug = re.sub(r"[^a-z0-9]+", "_", f"{to}_{subject}".lower()).strip("_")[:60]
    text = f"To: {to}\nSubject: {subject}\nStatus: DRAFT - needs human review before sending\n\n{body}\n"
    return {"draft_saved_to": _write(f"outbox/{slug}.txt", text)}


# ---------------------------------------------------------------- leave and policy

def _policy_sections():
    text = (DATA / "policy_handbook.md").read_text(encoding="utf-8")
    parts = re.split(r"^## ", text, flags=re.M)[1:]
    return [{"section": p.split("\n", 1)[0].strip(), "text": p.split("\n", 1)[1].strip()} for p in parts]


def search_policy(query, top_k=2):
    """Keyword search over the handbook; returns the best sections with their titles for citation."""
    stop = {"the", "a", "an", "of", "for", "to", "is", "in", "and", "how", "many", "what", "do", "i", "my",
            "can", "get", "on", "we", "me", "much", "are", "does", "take"}
    words = [w for w in re.findall(r"[a-z]+", query.lower()) if w not in stop]
    scored = []
    for s in _policy_sections():
        hay = (s["section"] + " " + s["text"]).lower()
        title = s["section"].lower()
        score = sum(hay.count(w) + 3 * (w in title) for w in words)
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return {"results": [s for _, s in scored[:top_k]]}


def get_employee(employee_id):
    emp = next((e for e in _load("employees.json") if e["id"].lower() == employee_id.lower()), None)
    return emp or {"error": f"No employee {employee_id}"}


def count_leave_days(start_date, end_date):
    """Working days between two dates inclusive, skipping weekends and public holidays."""
    holidays = set(_load("holidays.json"))
    d, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    days, skipped = 0, []
    while d <= end:
        if d.weekday() >= 5 or d.isoformat() in holidays:
            skipped.append(d.isoformat())
        else:
            days += 1
        d += timedelta(days=1)
    return {"working_days": days, "skipped_dates": skipped}


def evaluate_leave_request(employee_id, leave_type, start_date, end_date):
    """Apply the handbook rules and return approve or route_to_manager with reasons."""
    emp = get_employee(employee_id)
    if "error" in emp:
        return emp
    leave_type = leave_type.lower()
    if leave_type not in emp["leave_balance"]:
        return {"error": f"Unknown leave type {leave_type}; use annual, sick or casual"}
    days = count_leave_days(start_date, end_date)["working_days"]
    notice = (date.fromisoformat(start_date) - today()).days
    balance = emp["leave_balance"][leave_type]
    reasons = []
    if days == 0:
        reasons.append("The period has no working days")
    if days > balance:
        reasons.append(f"Needs {days} days but only {balance} {leave_type} days left")
    if leave_type == "annual":
        if notice < 7:
            reasons.append(f"Only {notice} days' notice; policy section 1 asks for 7")
        if days > 5:
            reasons.append("More than 5 working days needs manager approval (section 1)")
    if leave_type == "casual":
        if days > 2:
            reasons.append("Casual leave is limited to 2 consecutive days (section 3)")
        if notice < 1:
            reasons.append("Casual leave needs 1 day's notice (section 3)")
    note = "Medical certificate needed after return (section 2)" if leave_type == "sick" and days > 2 else ""
    decision = "approve" if not reasons else "route_to_manager"
    return {"employee": emp["name"], "employee_id": emp["id"], "employee_email": emp["email"],
            "manager": emp["manager"], "manager_email": emp["manager_email"],
            "leave_type": leave_type, "start_date": start_date, "end_date": end_date,
            "working_days": days, "balance_before": balance,
            "balance_after": balance - days if decision == "approve" else balance,
            "notice_days": notice, "decision": decision, "reasons": reasons, "note": note}


def record_leave_decision(decision):
    """Append the decision to outputs/leave_decisions.json (the HRMS stand-in)."""
    path = OUT / "leave_decisions.json"
    log = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    log.append({**decision, "recorded_at": datetime.now().isoformat(timespec="seconds")})
    OUT.mkdir(exist_ok=True)
    path.write_text(json.dumps(log, indent=2), encoding="utf-8")
    return {"recorded_in": str(path.relative_to(ROOT)), "entries": len(log)}


# ---------------------------------------------------------------- registry

def _schema(props, required):
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


S = {"type": "string"}
TOOLS = {
    "list_job_openings": (list_job_openings, "List open jobs with their must-have and nice-to-have skills.", _schema({}, [])),
    "list_resumes": (list_resumes, "List resume files waiting to be screened.", _schema({}, [])),
    "read_resume": (read_resume, "Read one resume's full text.", _schema({"file_name": S}, ["file_name"])),
    "score_resume": (score_resume, "Score a resume against a job's requirements (0-100) and list missing must-haves.",
                     _schema({"file_name": S, "job_id": S}, ["file_name", "job_id"])),
    "save_shortlist": (save_shortlist, "Save the ranked candidate list as a report for HR. Each candidate needs candidate, score, years_experience, must_have_missing and decision (shortlist or decline).",
                       _schema({"job_id": S, "ranked_candidates": {"type": "array", "items": {"type": "object"}}, "notes": S},
                               ["job_id", "ranked_candidates", "notes"])),
    "get_new_hire": (get_new_hire, "Look up a new hire by id or name.", _schema({"hire_id_or_name": S}, ["hire_id_or_name"])),
    "check_documents": (check_documents, "Check which required onboarding documents a new hire has not submitted.", _schema({"hire_id": S}, ["hire_id"])),
    "create_onboarding_plan": (create_onboarding_plan, "Create and save the dated onboarding checklist for a new hire.", _schema({"hire_id": S}, ["hire_id"])),
    "draft_email": (draft_email, "Save an email draft for human review. Never sends.", _schema({"to": S, "subject": S, "body": S}, ["to", "subject", "body"])),
    "search_policy": (search_policy, "Search the HR policy handbook. Returns matching sections to quote and cite.", _schema({"query": S}, ["query"])),
    "get_employee": (get_employee, "Get an employee's manager and leave balances.", _schema({"employee_id": S}, ["employee_id"])),
    "count_leave_days": (count_leave_days, "Count working days in a date range, skipping weekends and holidays.", _schema({"start_date": S, "end_date": S}, ["start_date", "end_date"])),
    "evaluate_leave_request": (evaluate_leave_request, "Apply leave policy rules to a request. Returns approve or route_to_manager with reasons.",
                               _schema({"employee_id": S, "leave_type": {"type": "string", "enum": ["annual", "sick", "casual"]}, "start_date": S, "end_date": S},
                                       ["employee_id", "leave_type", "start_date", "end_date"])),
    "record_leave_decision": (record_leave_decision, "Record the result of evaluate_leave_request in the leave ledger.",
                              _schema({"decision": {"type": "object"}}, ["decision"])),
}


def tool_specs(names):
    return [{"name": n, "description": TOOLS[n][1], "input_schema": TOOLS[n][2]} for n in names]


def run_tool(name, args):
    try:
        return TOOLS[name][0](**args)
    except Exception as exc:  # report tool errors back to the agent instead of crashing
        return {"error": f"{type(exc).__name__}: {exc}"}
