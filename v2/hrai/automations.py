"""Built-in triggers (see hrai/triggers.py for how triggers work)."""

import json
import threading
from datetime import date, timedelta

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
    db.x("INSERT OR REPLACE INTO new_hires VALUES (?,?,?,?,?,?,?,?)",
         (hire["id"], hire["name"], hire["email"], hire["role"], hire["department"], hire["manager"], hire["start_date"],
          json.dumps(hire.get("documents_submitted", []))))
    triggers.emit("new_hire.created", {"hire_id": hire["id"]})
