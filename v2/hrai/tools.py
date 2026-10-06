"""HR tools, defined once and shared by every surface: LangChain/LangGraph agents, the CrewAI crew, the MCP server
and the scripted offline planner.

Each tool:
- checks the caller's role (and, for employees, that they only touch their own records);
- runs the pre_tool / post_tool hooks (audit log, guards);
- returns a JSON-able dict, and reports failures as {"error": ...} plus a ticket instead of crashing the agent.
Nothing here sends email or changes a real HR system: emails are drafts, and leave that needs a manager goes to
the approvals queue.
"""

import functools
import inspect
import json
import re
from datetime import date, timedelta

from langchain_core.tools import StructuredTool

from . import auth, config, db, hooks
from .knowledge import cag, kag, mag, vectors

REGISTRY = {}  # name -> {"fn", "roles", "tool" (LangChain), "description"}
HR_ROLES = {"admin", "hr", "service"}
ALL_ROLES = set(auth.ROLES)

REQUIRED_DOCUMENTS = ["offer_letter_signed", "id_proof", "pan_card", "address_proof", "bank_details",
                      "education_certificates", "nda_signed", "relieving_letter"]


def hr_tool(roles=ALL_ROLES):
    def wrap(fn):
        @functools.wraps(fn)
        def guarded(**kwargs):
            user = auth.current_user()
            if user.role not in roles:
                return {"error": f"Your role ({user.role}) cannot use {fn.__name__}"}
            ctx = {"tool": fn.__name__, "args": kwargs, "username": user.username}
            try:
                hooks.run("pre_tool", ctx)
                out = fn(**ctx["args"])
                ctx["result"] = out
                hooks.run("post_tool", ctx)
                return ctx["result"]
            except hooks.HookBlocked as exc:
                return {"error": str(exc)}
            except PermissionError as exc:
                return {"error": str(exc)}
            except Exception as exc:
                from .ops import tracker
                tid = tracker.capture_exception(exc, {"tool": fn.__name__, "args": kwargs})
                return {"error": f"{type(exc).__name__}: {exc}", "ticket_id": tid}

        guarded.__signature__ = inspect.signature(fn)
        description = inspect.getdoc(fn).split("\n\n")[0]
        REGISTRY[fn.__name__] = {"fn": guarded, "roles": roles, "description": description,
                                 "tool": StructuredTool.from_function(func=guarded, name=fn.__name__,
                                                                      description=description)}
        return guarded
    return wrap


def run(name, **args):
    return REGISTRY[name]["fn"](**args)


def langchain_tools(names):
    return [REGISTRY[n]["tool"] for n in names]


def openai_specs(names):
    from langchain_core.utils.function_calling import convert_to_openai_tool
    return [convert_to_openai_tool(REGISTRY[n]["tool"]) for n in names]


def _report_path(name):
    path = config.home() / "reports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- screening

@hr_tool(HR_ROLES)
def list_job_openings() -> dict:
    """List open jobs with their must-have and nice-to-have skills."""
    jobs = db.q("SELECT * FROM jobs")
    for j in jobs:
        j["must_have"], j["nice_to_have"] = json.loads(j["must_have"]), json.loads(j["nice_to_have"])
    return {"jobs": jobs}


@hr_tool(HR_ROLES)
def list_candidates(job_id: str = "") -> dict:
    """List candidates (id, name, current score and decision), optionally for one job."""
    sql, args = "SELECT id, name, email, job_id, score, decision FROM candidates", ()
    if job_id:
        sql, args = sql + " WHERE job_id=?", (job_id,)
    return {"candidates": db.q(sql, args)}


@hr_tool(HR_ROLES)
def search_resumes(query: str, k: int = 5) -> dict:
    """Semantic search over all resumes (RAG), e.g. 'payments APIs on AWS'. Returns best-matching candidates."""
    hits = vectors.search("resumes", query, k=k)
    return {"matches": [{"candidate_id": h["id"], "name": h["meta"].get("name"), "similarity": h["score"],
                         "excerpt": h["text"][:300]} for h in hits]}


@hr_tool(HR_ROLES)
def read_resume(candidate_id: str) -> dict:
    """Read one candidate's full resume text."""
    c = db.q1("SELECT id, name, resume_text FROM candidates WHERE id=?", (candidate_id,))
    return c or {"error": f"No candidate {candidate_id}"}


@hr_tool(HR_ROLES)
def score_candidate(candidate_id: str, job_id: str) -> dict:
    """Score a candidate against a job (0-100): 60 for must-haves, 24 for nice-to-haves, 16 for experience.

    Uses only skills and experience. Never uses name, gender, age, religion or other protected traits."""
    job = db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
    c = db.q1("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    if not job or not c:
        return {"error": f"Unknown job {job_id} or candidate {candidate_id}"}
    must, nice = json.loads(job["must_have"]), json.loads(job["nice_to_have"])
    text = c["resume_text"].lower()
    years = re.search(r"years of experience:\s*(\d+)", text)
    years = int(years.group(1)) if years else 0
    must_hit = [s for s in must if s in text]
    nice_hit = [s for s in nice if s in text]
    score = round(60 * len(must_hit) / len(must) + 24 * len(nice_hit) / len(nice) + 16 * min(years / job["min_years"], 1))
    db.x("UPDATE candidates SET score=?, updated_at=? WHERE id=?", (score, db.now(), candidate_id))
    return {"candidate_id": candidate_id, "candidate": c["name"], "email": c["email"], "years_experience": years,
            "score": score, "must_have_matched": must_hit, "must_have_missing": [s for s in must if s not in must_hit],
            "nice_to_have_matched": nice_hit, "meets_minimum": len(must_hit) == len(must) and years >= job["min_years"]}


@hr_tool(HR_ROLES)
def save_shortlist(job_id: str, decisions: list[dict], notes: str = "") -> dict:
    """Save screening decisions. decisions: [{"candidate_id", "decision": "shortlist"|"decline", "reason"}]."""
    job = db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        return {"error": f"No job {job_id}"}
    lines = [f"# Shortlist: {job['title']} ({job_id})", "", f"Generated {db.now()} by the screening crew. Draft for HR review.",
             "", "| Candidate | Score | Decision | Reason |", "| --- | --- | --- | --- |"]
    for d in decisions:
        if d.get("decision") not in ("shortlist", "decline"):
            return {"error": f"decision must be shortlist or decline, got {d.get('decision')!r}"}
        c = db.q1("SELECT * FROM candidates WHERE id=?", (d["candidate_id"],))
        if not c:
            return {"error": f"No candidate {d['candidate_id']}"}
        stage = "selected" if d["decision"] == "shortlist" else "rejected"
        db.x("UPDATE candidates SET decision=?, stage=?, status_note=?, updated_at=? WHERE id=?",
             (d["decision"], stage, d.get("reason", ""), db.now(), c["id"]))
        db.x("INSERT INTO candidate_events (candidate_id, ts, actor, event, detail) VALUES (?,?,?,?,?)",
             (c["id"], db.now(), auth.current_user().username, f"screened_{stage}", d.get("reason", "")))
        lines.append(f"| {c['name']} | {c['score']} | {d['decision']} | {d.get('reason', '')} |")
    if notes:
        lines += ["", "## Notes", "", notes]
    path = _report_path(f"shortlist_{job_id}.md")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"saved": len(decisions), "report": str(path)}


# ---------------------------------------------------------------- onboarding

@hr_tool(HR_ROLES)
def get_new_hire(hire_id_or_name: str) -> dict:
    """Look up a new hire by id (NH-201) or name."""
    key = hire_id_or_name.lower().strip()
    for h in db.q("SELECT * FROM new_hires"):
        if key in (h["id"].lower(), h["name"].lower()) or key in h["name"].lower():
            h["documents"] = json.loads(h["documents"])
            return h
    return {"error": f"No new hire matching {hire_id_or_name}",
            "known": [f"{h['id']} {h['name']}" for h in db.q("SELECT id, name FROM new_hires")]}


@hr_tool(HR_ROLES)
def check_documents(hire_id: str) -> dict:
    """Check which required onboarding documents a new hire has not submitted yet."""
    h = db.q1("SELECT * FROM new_hires WHERE id=?", (hire_id,))
    if not h:
        return {"error": f"No new hire {hire_id}"}
    have = json.loads(h["documents"])
    missing = [d for d in REQUIRED_DOCUMENTS if d not in have]
    return {"hire_id": hire_id, "submitted": have, "missing": missing,
            "payroll_blocked": any(d in missing for d in ("bank_details", "pan_card"))}


@hr_tool(HR_ROLES)
def create_onboarding_plan(hire_id: str) -> dict:
    """Create the dated onboarding checklist for a new hire; tasks needing missing documents are marked blocked."""
    h = db.q1("SELECT * FROM new_hires WHERE id=?", (hire_id,))
    if not h:
        return {"error": f"No new hire {hire_id}"}
    docs = run("check_documents", hire_id=hire_id)
    start = date.fromisoformat(h["start_date"])
    steps = [(-7, "HR", "Send welcome email with joining details and document checklist", None),
             (-5, "IT", f"Create email, chat and HRMS accounts; ship laptop for {h['role']}", "id_proof"),
             (-3, "Payroll", "Set up payroll and statutory registrations", "bank_details"),
             (-3, "HR", "Collect missing documents", "collect"),
             (-1, h["manager"], "Assign onboarding buddy and first-week goals", None),
             (0, "HR", "Day-1 orientation: policies, benefits, security training", None),
             (1, h["manager"], "Team introduction and role walkthrough", None),
             (30, h["manager"], "30-day check-in", None), (90, "HR", "90-day probation review", None)]
    db.x("DELETE FROM onboarding_tasks WHERE hire_id=?", (hire_id,))
    tasks = []
    for offset, owner, task, needs in steps:
        if needs == "collect" and not docs["missing"]:
            continue
        blocked = (needs in docs["missing"]) or (needs == "bank_details" and docs["payroll_blocked"])
        t = {"due": (start + timedelta(days=offset)).isoformat(), "owner": owner, "task": task,
             "status": "blocked: missing documents" if blocked else "to do"}
        db.x("INSERT INTO onboarding_tasks (hire_id, due, owner, task, status) VALUES (?,?,?,?,?)",
             (hire_id, t["due"], owner, task, t["status"]))
        tasks.append(t)
    return {"hire_id": hire_id, "name": h["name"], "start_date": h["start_date"], "missing_documents": docs["missing"],
            "tasks": tasks}


# ---------------------------------------------------------------- shared

@hr_tool(ALL_ROLES)
def draft_email(to: str, subject: str, body: str) -> dict:
    """Save an email as a draft for a human to review. Never sends anything."""
    eid = db.x("INSERT INTO outbox (to_addr, subject, body, status, agent, created_at) VALUES (?,?,?,?,?,?)",
               (to, subject, body, "draft", auth.current_user().username, db.now()))
    return {"draft_id": eid, "status": "draft, needs human review before sending"}


@hr_tool(ALL_ROLES)
def recall_memory(query: str) -> dict:
    """Recall what this user said or asked before (long-term memory, MAG)."""
    return {"memories": mag.recall(auth.current_user().username, query)}


@hr_tool(ALL_ROLES)
def load_skill(name: str) -> dict:
    """Load the full instructions of a skill by name (see the skills list in your instructions)."""
    from . import skills
    s = skills.get(name)
    return {"name": name, "instructions": s["body"]} if s else {"error": f"No skill {name}", "available": skills.names()}


@hr_tool(ALL_ROLES)
def report_issue(title: str, detail: str) -> dict:
    """Report a problem you hit (missing data, a tool bug, a policy gap) as a ticket for the engineering agent."""
    from .ops import tracker
    return {"ticket_id": tracker.open_ticket("agent", title, detail, kind="bug", severity="medium",
                                             fingerprint_parts=("agent", title.lower()))}


@hr_tool(ALL_ROLES)
def ask_agent(agent: str, question: str) -> dict:
    """Ask another HR agent (policy, leave, onboarding, screening) a question over the A2A protocol."""
    from . import a2a
    return a2a.delegate(agent, question)


# ---------------------------------------------------------------- policy

@hr_tool(ALL_ROLES)
def policy_context(question: str) -> dict:
    """Get the policy text to answer from: the whole handbook (CAG) when small, else the top sections (RAG)."""
    return cag.policy_context(question)


@hr_tool(ALL_ROLES)
def search_policy(query: str, k: int = 3) -> dict:
    """Semantic search over the HR policy handbook (RAG). Returns sections to quote and cite."""
    return {"results": [{"section": h["meta"].get("section"), "text": h["text"], "similarity": h["score"]}
                        for h in vectors.search("policy", query, k=k)]}


@hr_tool(ALL_ROLES)
def search_faq(query: str) -> dict:
    """Search the candidate FAQ (interview format, results, rescheduling, travel)."""
    return {"results": [{"section": h["meta"].get("section"), "text": h["text"]} for h in vectors.search("faq", query, k=2)]}


@hr_tool(ALL_ROLES)
def kg_facts(question: str) -> dict:
    """Exact facts from the HR knowledge graph (KAG): reporting lines, approvers, leave rules, job skills."""
    user = auth.current_user()
    facts = kag.facts_for(question, employee_id=user.employee_id)
    if user.role == "employee":  # employees only see facts about themselves and general policy
        me = db.q1("SELECT name FROM employees WHERE id=?", (user.employee_id,)) or {"name": ""}
        people = {r["name"] for r in db.q("SELECT name FROM employees")}
        facts = [f for f in facts if f["subject"] not in people or f["subject"] == me["name"]]
    return {"facts": facts}


# ---------------------------------------------------------------- leave

def _check_employee_access(employee_id):
    user = auth.current_user()
    if not user.can("leave:any_employee") and (user.employee_id or "").lower() != employee_id.lower():
        raise PermissionError("Employees can only see and request their own leave")


@hr_tool(ALL_ROLES)
def get_employee(employee_id: str) -> dict:
    """Get an employee's manager and leave balances."""
    _check_employee_access(employee_id)
    e = db.q1("SELECT * FROM employees WHERE upper(id)=upper(?)", (employee_id,))
    if not e:
        return {"error": f"No employee {employee_id}"}
    e["leave_balance"] = {k: e.pop(k) for k in ("annual", "sick", "casual")}
    return e


def _working_days(start, end):
    holidays = {r["day"] for r in db.q("SELECT day FROM holidays")}
    d, n = start, 0
    while d <= end:
        n += d.weekday() < 5 and d.isoformat() not in holidays
        d += timedelta(days=1)
    return n


@hr_tool(ALL_ROLES)
def evaluate_leave_request(employee_id: str, leave_type: str, start_date: str, end_date: str) -> dict:
    """Apply the leave rules (read from the knowledge graph) and return approve or route_to_manager with reasons."""
    _check_employee_access(employee_id)
    e = db.q1("SELECT * FROM employees WHERE upper(id)=upper(?)", (employee_id,))
    if not e:
        return {"error": f"No employee {employee_id}"}
    lt = leave_type.lower().replace(" leave", "")
    if lt not in ("annual", "sick", "casual"):
        return {"error": "leave_type must be annual, sick or casual"}
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if end < start:
        return {"error": "end_date is before start_date"}
    days, notice, balance = _working_days(start, end), (start - config.today()).days, e[lt]
    reasons = []
    if days == 0:
        reasons.append("The period has no working days")
    if days > balance:
        reasons.append(f"Needs {days} days but only {balance:g} {lt} days left")
    if lt == "annual":
        need, cap = kag.rule("annual leave", "notice_days"), kag.rule("annual leave", "auto_approve_max_days")
        if notice < need:
            reasons.append(f"Only {notice} days' notice; policy section 1 asks for {need}")
        if days > cap:
            reasons.append(f"More than {cap} working days needs manager approval (section 1)")
    if lt == "casual":
        cap, need = kag.rule("casual leave", "max_consecutive_days"), kag.rule("casual leave", "notice_days")
        if days > cap:
            reasons.append(f"Casual leave is limited to {cap} consecutive days (section 3)")
        if notice < need:
            reasons.append(f"Casual leave needs {need} day's notice (section 3)")
    cert = kag.rule("sick leave", "certificate_after_days")
    note = f"Medical certificate needed after return (section 2)" if lt == "sick" and days > cert else ""
    decision = "approve" if not reasons else "route_to_manager"
    return {"employee": e["name"], "employee_id": e["id"], "manager": e["manager"], "leave_type": lt,
            "start_date": start_date, "end_date": end_date, "working_days": days, "notice_days": notice,
            "balance_before": balance, "balance_after": balance - days if decision == "approve" else balance,
            "decision": decision, "reasons": reasons, "note": note}


@hr_tool(ALL_ROLES)
def record_leave_decision(employee_id: str, leave_type: str, start_date: str, end_date: str) -> dict:
    """Record a leave request. The rules are re-checked here; auto-approved leave updates the balance, anything
    else goes to the manager's approval queue."""
    ev = run("evaluate_leave_request", employee_id=employee_id, leave_type=leave_type, start_date=start_date, end_date=end_date)
    if "error" in ev:
        return ev
    status = "approved" if ev["decision"] == "approve" else "pending_manager"
    rid = db.x("INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, working_days, decision, reasons, "
               "status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (ev["employee_id"], ev["leave_type"], start_date, end_date, ev["working_days"], ev["decision"],
                json.dumps(ev["reasons"]), status, db.now()))
    if status == "approved":
        db.x(f"UPDATE employees SET {ev['leave_type']} = {ev['leave_type']} - ? WHERE id=?", (ev["working_days"], ev["employee_id"]))
        approval = None
    else:
        approval = db.x("INSERT INTO approvals (kind, ref, summary, payload, requested_by, created_at) VALUES (?,?,?,?,?,?)",
                        ("leave", str(rid), f"{ev['employee']}: {ev['working_days']} days {ev['leave_type']} leave "
                         f"{start_date} to {end_date}. " + "; ".join(ev["reasons"]), json.dumps(ev),
                         auth.current_user().username, db.now()))
    return {"leave_request_id": rid, "status": status, "approval_id": approval, **ev}


def decide_approval(approval_id, approve, note=""):
    """Human decision on a queued approval (leave today; more kinds later)."""
    user = auth.require("approvals:decide")
    a = db.q1("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not a or a["status"] != "pending":
        raise ValueError("No pending approval with that id")
    status = "approved" if approve else "rejected"
    db.x("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?", (status, user.username, db.now(), approval_id))
    if a["kind"] == "offer":
        from . import hiring
        hiring.on_offer_decision(int(a["ref"]), approve)
    if a["kind"] == "leave":
        lr = db.q1("SELECT * FROM leave_requests WHERE id=?", (int(a["ref"]),))
        db.x("UPDATE leave_requests SET status=? WHERE id=?", (status, lr["id"]))
        if approve:
            db.x(f"UPDATE employees SET {lr['leave_type']} = {lr['leave_type']} - ? WHERE id=?", (lr["working_days"], lr["employee_id"]))
    db.audit(user.username, f"approval.{status}", {"id": approval_id, "note": note})
    return {"approval_id": approval_id, "status": status}


# ---------------------------------------------------------------- hiring pipeline

def _hiring(fn, *args, **kwargs):
    from . import hiring
    try:
        return fn(hiring)(*args, **kwargs)
    except hiring.HiringError as exc:
        return {"error": str(exc)}


@hr_tool(HR_ROLES)
def ingest_resumes(job_id: str = "") -> dict:
    """Read new resume files from the inbox folder (inbox/<JOB-ID>/), screen them and sort them into selected,
    on_hold or rejected. Returns what was added, duplicates and unreadable files."""
    return _hiring(lambda h: h.ingest, job_id=job_id or None)


@hr_tool(HR_ROLES)
def pipeline_summary(job_id: str = "") -> dict:
    """Counts of candidates per stage and per interview round, plus follow-ups due today."""
    from . import hiring
    return {"summary": hiring.summary(job_id or None), "board": hiring.board(job_id or None)}


@hr_tool(HR_ROLES)
def candidate_timeline(candidate: str) -> dict:
    """Everything about one candidate (id like C-007 or a name): stage, rounds, interviews, offer, follow-ups, history."""
    return _hiring(lambda h: h.timeline, candidate)


@hr_tool(HR_ROLES)
def move_candidate(candidate: str, stage: str, note: str = "") -> dict:
    """Move a candidate to a stage (selected, on_hold, rejected, withdrawn, ...) with a note. HR override."""
    return _hiring(lambda h: h.move, candidate, stage, note)


@hr_tool(HR_ROLES)
def schedule_interview(candidate: str, round_name: str = "", when: str = "", interviewer: str = "",
                       mode: str = "Video call") -> dict:
    """Schedule an interview round (L1, L2, HR, Final...; default the next round) at `when` (YYYY-MM-DDTHH:MM).
    Drafts the invite email and a reminder follow-up."""
    return _hiring(lambda h: h.schedule, candidate, round_name or None, when or None, interviewer, mode)


@hr_tool(HR_ROLES)
def record_interview_result(candidate: str, round_name: str, result: str, rating: int = 0, feedback: str = "") -> dict:
    """Record a round's result: pass (moves to the next round, or to offer after the last), fail (rejected, regret
    email drafted) or hold. rating 1-5, 0 for none."""
    return _hiring(lambda h: h.record_result, candidate, round_name, result, rating or None, feedback)


@hr_tool(HR_ROLES)
def make_offer(candidate: str, ctc_lpa: float, joining_date: str) -> dict:
    """Request an offer (CTC in lakh per annum, joining date YYYY-MM-DD). Goes to the approvals queue first."""
    return _hiring(lambda h: h.make_offer, candidate, ctc_lpa, joining_date)


@hr_tool(HR_ROLES)
def record_offer_response(candidate: str, accepted: bool, joining_date: str = "") -> dict:
    """Record whether the candidate accepted the offer. Accepting creates the new hire, starts onboarding and
    schedules pre-joining and post-joining follow-ups."""
    return _hiring(lambda h: h.offer_response, candidate, accepted, joining_date or None)


@hr_tool(HR_ROLES)
def mark_joined(candidate: str) -> dict:
    """Mark that an accepted candidate has joined."""
    return _hiring(lambda h: h.mark_joined, candidate)


@hr_tool(HR_ROLES)
def list_followups(days_ahead: int = 7) -> dict:
    """Open hiring follow-ups due within `days_ahead` days (overdue ones included)."""
    from . import hiring
    return {"followups": hiring.followups(days_ahead)}


@hr_tool(HR_ROLES)
def complete_followup(followup_id: int, note: str = "") -> dict:
    """Mark a follow-up done."""
    return _hiring(lambda h: h.complete_followup, followup_id, note)


@hr_tool(HR_ROLES)
def set_interview_rounds(job_id: str, rounds: list[str]) -> dict:
    """Set a job's interview rounds in order, e.g. ["L1", "L2", "L3", "HR", "Final"]."""
    return {"job_id": job_id, "rounds": _hiring(lambda h: h.set_rounds, job_id, rounds)}


# ---------------------------------------------------------------- insights

@hr_tool(HR_ROLES)
def hr_insights(section: str = "", job_id: str = "") -> dict:
    """HR analytics. section: '' for a plain-language snapshot plus key numbers, or one of hiring, workforce, leave,
    onboarding, ai, operations for that section's detail. job_id narrows hiring numbers to one job."""
    from . import insights
    fns = {"hiring": lambda: insights.hiring(job_id or None), "workforce": insights.workforce, "leave": insights.leave,
           "onboarding": insights.onboarding, "ai": insights.ai_usage, "operations": insights.operations}
    if section:
        if section not in fns:
            return {"error": f"Unknown section {section!r}; use one of {', '.join(fns)}"}
        return {section: fns[section]()}
    data = insights.overview(job_id or None)
    return {"summary": insights.narrate(data), "kpis": data["kpis"]}


@hr_tool(HR_ROLES)
def needs_attention(limit: int = 10) -> dict:
    """What needs HR today, most urgent first: overdue follow-ups, approvals, unscheduled interviews, missing results,
    joiners with missing documents, budgets running out, fixes awaiting review."""
    from . import insights
    return {"items": insights.attention(limit)}
