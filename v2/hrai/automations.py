"""Built-in triggers (see hrai/triggers.py for how triggers work)."""

import json
import threading
from datetime import date, datetime, timedelta

from . import auth, config, db, triggers
from . import tools as T
from .gateway import llm


def _as(user):
    return auth.set_current_user(user)


@triggers.schedule("onboarding_document_chase", daily="09:00")
def onboarding_document_chase():
    """Every morning: draft reminders to new hires starting within 21 days who still owe documents."""
    reset = _as(auth.User(0, "trigger", "service"))
    try:
        drafted = []
        for h in db.q("SELECT * FROM new_hires"):
            days = (date.fromisoformat(h["start_date"]) - config.today()).days
            missing = T.run("check_documents", hire_id=h["id"])["missing"]
            if 0 <= days <= 21 and missing:
                T.run("draft_email", to=h["email"], subject=f"Reminder: {len(missing)} documents still needed",
                      body=f"Hi {h['name'].split()[0]},\n\nYou join in {days} days. Please upload: {', '.join(missing)}.\n\nHR")
                drafted.append(h["id"])
        return {"reminders_drafted": drafted}
    finally:
        auth._current.reset(reset)


@triggers.schedule("ticket_sweep", every=600)
def ticket_sweep():
    """Every 10 minutes: the ticket agent works new tickets; GitHub PR states are synced."""
    from .ops import ticket_agent
    out = {"worked": ticket_agent.work_new_tickets()}
    if ticket_agent.github_repo():
        try:
            out["synced"] = ticket_agent.sync_github()
        except Exception as exc:
            out["sync_error"] = str(exc)[:200]
    return out


@triggers.schedule("budget_watch", daily="18:00")
def budget_watch():
    """Every evening: warn the admin about agents past 80% of their monthly AI budget."""
    hot = [a for a in llm.budget_report()["agents"] if a["used_pct"] >= 80]
    if hot:
        reset = _as(auth.User(0, "trigger", "service"))
        try:
            T.run("draft_email", to=config.env("HRAI_ADMIN_EMAIL", "admin@example.com"), subject="AI budget warning",
                  body="These agents are past 80% of their monthly budget:\n" +
                       "\n".join(f"- {a['agent']}: ${a['spent_usd']} of ${a['budget_usd']} ({a['used_pct']}%)" for a in hot))
        finally:
            auth._current.reset(reset)
    return {"over_80_pct": [a["agent"] for a in hot]}


@triggers.schedule("reindex_knowledge", daily="02:00")
def reindex_knowledge():
    """Every night: rebuild the vector store and knowledge graph from the latest data."""
    from .knowledge import kag, vectors
    return {"vectors": vectors.index_all(), "kg_triples": kag.build()}


@triggers.on("new_hire.created")
def plan_for_new_hire(payload):
    """When a new hire is added: build their onboarding plan straight away."""
    reset = _as(auth.User(0, "trigger", "service"))
    try:
        return T.run("create_onboarding_plan", hire_id=payload["hire_id"])
    finally:
        auth._current.reset(reset)


@triggers.on("candidate.added")
def score_new_candidate(payload):
    """When a candidate is added: index and score them for their job."""
    from .knowledge import vectors
    c = db.q1("SELECT * FROM candidates WHERE id=?", (payload["candidate_id"],))
    vectors.upsert("resumes", [{"id": c["id"], "text": c["resume_text"], "meta": {"name": c["name"], "job_id": c["job_id"] or ""}}])
    reset = _as(auth.User(0, "trigger", "service"))
    try:
        return T.run("score_candidate", candidate_id=c["id"], job_id=c["job_id"])
    finally:
        auth._current.reset(reset)


@triggers.on("ticket.created")
def auto_work_ticket(payload):
    """With HRAI_TICKET_AUTOWORK=1, the ticket agent starts on a new ticket immediately (in the background)."""
    if config.env("HRAI_TICKET_AUTOWORK") != "1":
        return "queued for ticket_sweep"
    from .ops import ticket_agent
    threading.Thread(target=ticket_agent.work, args=(payload["ticket_id"],), daemon=True).start()
    return "started"


def add_new_hire(hire):
    """Example producer: inserting a new hire fires the new_hire.created event."""
    db.x("INSERT OR REPLACE INTO new_hires (id, name, email, role, department, manager, start_date, documents) "
         "VALUES (?,?,?,?,?,?,?,?)",
         (hire["id"], hire["name"], hire["email"], hire["role"], hire["department"], hire["manager"], hire["start_date"],
          json.dumps(hire.get("documents_submitted", []))))
    triggers.emit("new_hire.created", {"hire_id": hire["id"]})


# ---------------------------------------------------------------- hiring pipeline

@triggers.schedule("inbox_watch", every=120)
def inbox_watch():
    """Every 2 minutes: read new resume files dropped into inbox/<JOB-ID>/ and screen them."""
    from . import hiring
    reset = _as(auth.User(0, "trigger", "service"))
    try:
        r = hiring.ingest()
        return {k: v for k, v in r.items() if v}
    finally:
        auth._current.reset(reset)


@triggers.schedule("hiring_followups", daily="08:30")
def hiring_followups():
    """Every morning: a digest email to HR of follow-ups due today or overdue, and reminders for tomorrow's interviews."""
    from . import hiring
    reset = _as(auth.User(0, "trigger", "service"))
    try:
        due = hiring.followups(0)
        tomorrow = (config.today() + timedelta(days=1)).isoformat()
        interviews = db.q("SELECT i.*, c.name, c.email FROM interviews i JOIN candidates c ON c.id=i.candidate_id "
                          "WHERE i.status='scheduled' AND substr(i.scheduled_at,1,10)=?", (tomorrow,))
        for iv in interviews:
            T.run("draft_email", to=iv["email"], subject=f"Reminder: your {iv['round']} interview tomorrow",
                  body=f"Dear {iv['name']},\n\nA reminder that your {iv['round']} interview is at "
                       f"{iv['scheduled_at'][11:16]} tomorrow ({iv['mode']}).\n\nRegards,\nTalent Acquisition")
        if due:
            T.run("draft_email", to=config.env("HRAI_HR_EMAIL", "hr@example.com"), subject=f"{len(due)} hiring follow-ups due",
                  body="\n".join(f"- {f['due']} {f['name']} ({f['stage']}): {f['note']}" for f in due))
        return {"followups_due": len(due), "interview_reminders": len(interviews)}
    finally:
        auth._current.reset(reset)


@triggers.schedule("stale_candidates", daily="10:00")
def stale_candidates():
    """Every morning: candidates stuck in selected, on_hold or interviewing for 5+ days get a follow-up."""
    from . import hiring
    cutoff = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    flagged = []
    for c in db.q("SELECT id, name, stage FROM candidates WHERE stage IN ('selected','on_hold','interviewing') AND updated_at<?", (cutoff,)):
        if not db.q1("SELECT 1 AS y FROM followups WHERE candidate_id=? AND kind='stale' AND status='open'", (c["id"],)):
            hiring.add_followup(c["id"], config.today().isoformat(), "stale", f"{c['name']} has been {c['stage']} for 5+ days; decide the next step")
            flagged.append(c["id"])
    return {"flagged": flagged}


@triggers.schedule("insights_report", daily="07:45")
def insights_report():
    """Every morning: save an HR insights snapshot to var/reports/; on Mondays also draft it as an email to HR."""
    from . import insights
    data = insights.overview()
    path = insights.save_report(data)
    drafted = False
    if config.today().weekday() == 0 or config.env("HRAI_INSIGHTS_EMAIL_DAILY") == "1":
        reset = _as(auth.User(0, "trigger", "service"))
        try:
            T.run("draft_email", to=config.env("HRAI_HR_EMAIL", "hr@example.com"),
                  subject=f"HR insights for the week of {data['as_of']}", body=insights.narrate(data))
            drafted = True
        finally:
            auth._current.reset(reset)
    return {"report": str(path), "attention_items": len(data["attention"]), "email_drafted": drafted}


@triggers.schedule("payroll_reminder", daily="09:30")
def payroll_reminder():
    """From the 25th: remind HR to run and approve this month's payroll while it is still a draft."""
    from . import payroll
    if config.today().day < int(config.env("HRAI_PAYROLL_REMINDER_DAY", "25")):
        return {"skipped": "too early in the month"}
    month = config.today().strftime("%Y-%m")
    s = payroll.summary(month)
    if s["status"] in ("approved", "paid"):
        return {"month": month, "status": s["status"]}
    reset = _as(auth.User(0, "trigger", "service"))
    try:
        if s["status"] == "not_started":
            body = f"Payroll for {month} has not been run yet. Run it in the Payroll tab, check it, then submit it for approval."
        elif s["status"] == "draft":
            body = (f"The {month} payroll is still a draft: {s['employees']} employees, net ₹{s['net']:,}. Submit it for "
                    f"approval." + ("\nWarnings: " + "; ".join(s["warnings"]) if s["warnings"] else ""))
        else:
            body = f"The {month} payroll is waiting for approval: net ₹{s['net']:,}. Someone other than the submitter must approve it."
        T.run("draft_email", to=config.env("HRAI_HR_EMAIL", "hr@example.com"), subject=f"Payroll {month}: {s['status']}",
              body=body)
    finally:
        auth._current.reset(reset)
    return {"month": month, "status": s["status"]}


@triggers.schedule("project_health", daily="08:45")
def project_health():
    """Every morning: flag project risks, and on Mondays draft a staffing digest for managers."""
    from . import projects
    risks = projects.risks()
    drafted = False
    if risks and (config.today().weekday() == 0 or any(r["severity"] == "critical" for r in risks)):
        reset = _as(auth.User(0, "trigger", "service"))
        try:
            u = projects.utilisation()
            T.run("draft_email", to=config.env("HRAI_HR_EMAIL", "hr@example.com"), subject="Projects and staffing",
                  body="\n".join(f"- {r['text']}" for r in risks) +
                       f"\n\nAverage utilisation {u['average_pct']}% across {u['people']} people; {u['bench']} on the "
                       f"bench, {u['over_allocated']} over-allocated.")
            drafted = True
        finally:
            auth._current.reset(reset)
    return {"risks": len(risks), "email_drafted": drafted}
