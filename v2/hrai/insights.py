"""HR insights: hiring funnel, interview rounds, time to hire, workforce, leave, onboarding, AI spend and tickets.

Everything is computed from the local database on request, so the numbers are always current. `attention()` is the
"what needs you today" list the dashboard and the insights agent lead with; each item says where to act.
"""

import csv
import io
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from . import config, db

FUNNEL = [("applied", "Applied"), ("screened_in", "Passed screening"), ("interviewed", "Interviewed"),
          ("offered", "Offered"), ("accepted", "Accepted offer"), ("joined", "Joined")]
ACTIVE = ("applied", "selected", "on_hold", "interviewing", "offer", "offer_accepted")


def _d(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _days(a, b):
    a, b = _d(a), _d(b)
    return (b - a).days if a and b else None


def _avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def _months_back(n):
    """The last n months as YYYY-MM, oldest first, ending with the current month."""
    y, m = config.today().year, config.today().month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out[::-1]


# ---------------------------------------------------------------- hiring

def hiring(job_id=None):
    where, args = (" WHERE job_id=?", (job_id,)) if job_id else ("", ())
    cands = db.q("SELECT id, job_id, stage, score, created_at, updated_at FROM candidates" + where, args)
    interviews = db.q("SELECT * FROM interviews" + where, args)
    offers = db.q("SELECT * FROM offers" + where, args)
    events = defaultdict(list)
    for e in db.q("SELECT candidate_id, ts, event, detail FROM candidate_events ORDER BY id"):
        events[e["candidate_id"]].append(e)

    interviewed = {i["candidate_id"] for i in interviews if i["status"] == "completed"}
    offered = {o["candidate_id"] for o in offers if o["status"] in ("sent", "accepted", "declined")}
    accepted = {o["candidate_id"] for o in offers if o["status"] == "accepted"}
    reached = {"applied": len(cands), "interviewed": len(interviewed), "offered": len(offered),
               "accepted": len(accepted) or sum(c["stage"] in ("offer_accepted", "joined") for c in cands),
               "joined": sum(c["stage"] == "joined" for c in cands)}
    reached["screened_in"] = sum(c["stage"] not in ("applied", "rejected") or c["id"] in interviewed for c in cands)
    funnel = []
    for key, label in FUNNEL:
        n = reached[key]
        funnel.append({"key": key, "label": label, "count": n,
                       "pct_of_applied": round(100 * n / reached["applied"]) if reached["applied"] else 0})

    stages = Counter(c["stage"] for c in cands)
    by_round = defaultdict(lambda: {"scheduled": 0, "pass": 0, "fail": 0, "hold": 0, "ratings": []})
    for i in interviews:
        r = by_round[i["round"]]
        if i["status"] == "scheduled":
            r["scheduled"] += 1
        if i["result"] in ("pass", "fail", "hold"):
            r[i["result"]] += 1
        if i["rating"]:
            r["ratings"].append(i["rating"])
    order = []
    for j in db.q("SELECT rounds FROM jobs" + (" WHERE id=?" if job_id else ""), args):
        for name in json.loads(j["rounds"] or "[]"):
            if name not in order:
                order.append(name)
    rounds = []
    for name in order + sorted(set(by_round) - set(order)):
        r = by_round.get(name)
        if not r:
            continue
        decided = r["pass"] + r["fail"] + r["hold"]
        rounds.append({"round": name, "scheduled": r["scheduled"], "pass": r["pass"], "fail": r["fail"], "hold": r["hold"],
                       "pass_rate": round(100 * r["pass"] / decided) if decided else None,
                       "avg_rating": _avg(r["ratings"])})

    to_offer, to_accept, to_join = [], [], []
    for c in cands:
        ev = events.get(c["id"], [])
        start = c["created_at"] or (ev[0]["ts"] if ev else None)
        for e in ev:
            if e["event"] == "offer_requested":
                to_offer.append(_days(start, e["ts"]))
                break
        for e in ev:
            if e["event"] == "moved_offer_accepted":
                to_accept.append(_days(start, e["ts"]))
                break
    for o in offers:
        if o["status"] == "accepted":
            first = (events.get(o["candidate_id"]) or [{"ts": None}])[0]["ts"]
            to_join.append(_days(first, o["joining_date"]))
    sent = [o for o in offers if o["status"] in ("sent", "accepted", "declined")]
    answered = [o for o in sent if o["status"] != "sent"]

    jobs = []
    for j in db.q("SELECT id, title, status FROM jobs" + (" WHERE id=?" if job_id else "") + " ORDER BY id", args):
        mine = [c for c in cands if c["job_id"] == j["id"]]
        jobs.append({"job_id": j["id"], "title": j["title"], "status": j["status"] or "open", "total": len(mine),
                     "active": sum(c["stage"] in ACTIVE for c in mine),
                     "rejected": sum(c["stage"] in ("rejected", "offer_declined", "withdrawn") for c in mine),
                     "joined": sum(c["stage"] == "joined" for c in mine),
                     "avg_score": _avg([c["score"] for c in mine if c["score"] is not None])})
    return {"funnel": funnel, "stages": dict(stages), "rounds": rounds, "jobs": jobs,
            "missing_skills": missing_skills(job_id),
            "time_to_offer_days": _avg(to_offer), "time_to_accept_days": _avg(to_accept),
            "time_to_join_days": _avg(to_join),
            "offer_acceptance_pct": round(100 * sum(o["status"] == "accepted" for o in answered) / len(answered))
            if answered else None,
            "active": sum(stages[s] for s in ACTIVE)}


def missing_skills(job_id=None, top=6):
    """Must-have skills most often missing among candidates who were screened out."""
    miss = Counter()
    jobs = {j["id"]: j for j in db.q("SELECT id, must_have FROM jobs")}
    sql = "SELECT job_id, skills, resume_text FROM candidates WHERE stage='rejected'" + (" AND job_id=?" if job_id else "")
    for c in db.q(sql, (job_id,) if job_id else ()):
        job = jobs.get(c["job_id"])
        if not job:
            continue
        have = {s.lower() for s in json.loads(c["skills"] or "[]")}
        text = (c["resume_text"] or "").lower()
        for skill in json.loads(job["must_have"] or "[]"):
            if skill.lower() not in have and skill.lower() not in text:
                miss[skill] += 1
    return [{"skill": s, "candidates": n} for s, n in miss.most_common(top)]


# ---------------------------------------------------------------- people

def workforce():
    emps = db.q("SELECT department, level FROM employees")
    hires = db.q("SELECT start_date, department FROM new_hires")
    months = _months_back(6) + [m for m in sorted({(h["start_date"] or "")[:7] for h in hires})
                                if m and m > _months_back(1)[0]][:3]
    per_month = Counter((h["start_date"] or "")[:7] for h in hires)
    today = config.today().isoformat()
    return {"headcount": len(emps),
            "by_department": [{"department": k or "Unassigned", "count": v}
                              for k, v in Counter(e["department"] for e in emps).most_common()],
            "by_level": [{"level": k or "-", "count": v} for k, v in sorted(Counter(e["level"] for e in emps).items())],
            "joiners_by_month": [{"month": m, "count": per_month.get(m, 0)} for m in months],
            "upcoming_joiners": sum((h["start_date"] or "") >= today for h in hires)}


def leave():
    reqs = db.q("SELECT leave_type, status, working_days, start_date, end_date FROM leave_requests")
    today = config.today()
    soon = (today + timedelta(days=30)).isoformat()
    bal = db.q1("SELECT AVG(annual) AS annual, AVG(sick) AS sick, AVG(casual) AS casual FROM employees") or {}
    by_type = defaultdict(float)
    for r in reqs:
        if r["status"] == "approved":
            by_type[r["leave_type"] or "other"] += r["working_days"] or 0
    return {"requests": len(reqs),
            "by_status": dict(Counter(r["status"] for r in reqs)),
            "days_by_type": [{"type": k, "days": round(v, 1)} for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])],
            "pending": sum(r["status"] == "pending_manager" for r in reqs),
            "upcoming_days_30": round(sum(r["working_days"] or 0 for r in reqs if r["status"] == "approved"
                                          and today.isoformat() <= (r["start_date"] or "") <= soon), 1),
            "avg_balance": {k: round(v, 1) for k, v in bal.items() if v is not None}}


def onboarding():
    tasks = db.q("SELECT hire_id, due, status FROM onboarding_tasks")
    today = config.today().isoformat()
    done = sum(t["status"] == "done" for t in tasks)
    return {"tasks": len(tasks), "done": done, "pct_done": round(100 * done / len(tasks)) if tasks else None,
            "blocked": sum((t["status"] or "").startswith("blocked") for t in tasks),
            "overdue": sum(t["status"] != "done" and (t["due"] or "9") < today for t in tasks)}


# ---------------------------------------------------------------- AI and operations

def ai_usage(days=30):
    from .gateway import llm
    report = llm.budget_report()
    since = (config.today() - timedelta(days=days - 1)).isoformat()
    rows = db.q("SELECT substr(ts,1,10) AS day, SUM(cost_usd) AS usd, COUNT(*) AS calls, SUM(cached_tokens) AS cached, "
                "SUM(prompt_tokens) AS prompt FROM llm_usage WHERE ts >= ? GROUP BY day", (since,))
    per = {r["day"]: r for r in rows}
    daily = []
    for i in range(days):
        d = (config.today() - timedelta(days=days - 1 - i)).isoformat()
        r = per.get(d) or {}
        daily.append({"day": d, "usd": round(r.get("usd") or 0, 4), "calls": r.get("calls") or 0})
    tiers = db.q("SELECT tier, COUNT(*) AS calls, SUM(cost_usd) AS usd FROM llm_usage WHERE ts >= ? GROUP BY tier",
                 (llm.month_start(),))
    prompt = sum(r["prompt"] or 0 for r in rows)
    return {"month_from": report["month_from"], "total": report["total"], "agents": report["agents"], "daily": daily,
            "tiers": [{"tier": t["tier"] or "-", "calls": t["calls"], "usd": round(t["usd"] or 0, 4)} for t in tiers],
            "cache_hit_pct": round(100 * sum(r["cached"] or 0 for r in rows) / prompt) if prompt else None,
            "answer_cache_hits": (db.q1("SELECT COALESCE(SUM(hits),0) AS n FROM answer_cache") or {}).get("n", 0)}


def payroll_numbers():
    from . import payroll
    month = config.today().strftime("%Y-%m")
    s = payroll.summary(month)
    return {"month": month, "status": s["status"], "employees": s.get("employees", 0), "net": s.get("net", 0),
            "gross": s.get("gross", 0), "employer_cost": s.get("employer_cost", 0),
            "statutory": s.get("statutory", {}), "warnings": s.get("warnings", []), "history": payroll.history()}


def projects_numbers():
    from . import projects
    b = projects.board()
    return {"projects": len(b), "active": sum(p["status"] == "active" for p in b),
            "utilisation": projects.utilisation(),
            "open_tasks": sum(p["open_tasks"] for p in b), "overdue_tasks": sum(p["overdue_tasks"] for p in b),
            "hours_this_month": round(sum(p["hours_this_month"] for p in b), 1),
            "by_project": [{"project": p["name"], "status": p["status"], "team_size": p["team_size"], "fte": p["fte"],
                            "open_tasks": p["open_tasks"], "overdue_tasks": p["overdue_tasks"],
                            "hours_this_month": p["hours_this_month"]} for p in b],
            "risks": projects.risks()}


def culture_numbers():
    from . import engage
    return engage.engagement()


def operations():
    tickets = db.q("SELECT status, kind FROM tickets")
    fb = db.q("SELECT rating FROM feedback")
    return {"tickets_by_status": dict(Counter(t["status"] for t in tickets)),
            "open_tickets": sum(t["status"] not in ("closed", "resolved", "rejected", "duplicate") for t in tickets),
            "feedback": len(fb), "helpful_pct": round(100 * sum((f["rating"] or 0) > 0 for f in fb) / len(fb)) if fb else None,
            "pending_approvals": (db.q1("SELECT COUNT(*) AS n FROM approvals WHERE status='pending'") or {}).get("n", 0),
            "email_drafts": (db.q1("SELECT COUNT(*) AS n FROM outbox WHERE status='draft'") or {}).get("n", 0)}


# ---------------------------------------------------------------- what needs attention

def attention(limit=12):
    """Concrete next actions, most urgent first. Each item names where to act (tab, and a candidate or ticket id)."""
    today = config.today()
    t = today.isoformat()
    items = []

    def add(priority, text, tab, ref=None, kind="info"):
        items.append({"priority": priority, "text": text, "tab": tab, "ref": ref, "kind": kind})

    for f in db.q("SELECT f.id, f.due, f.note, f.candidate_id, c.name FROM followups f LEFT JOIN candidates c "
                  "ON c.id=f.candidate_id WHERE f.status='open' AND f.due <= ? ORDER BY f.due", (t,)):
        late = (today - _d(f["due"])).days if _d(f["due"]) else 0
        add(1 if late else 2, f"{f['note']} ({'overdue by ' + str(late) + ' day' + ('s' if late != 1 else '') if late else 'due today'})",
            "hiring", f["candidate_id"], "critical" if late > 2 else "warning")
    for a in db.q("SELECT id, kind, summary FROM approvals WHERE status='pending' ORDER BY id"):
        add(2, f"Approval waiting: {a['summary']}", "approvals", a["id"], "warning")
    for c in db.q("SELECT id, name FROM candidates WHERE stage='selected' AND id NOT IN "
                  "(SELECT candidate_id FROM interviews WHERE status='scheduled')"):
        add(3, f"{c['name']} passed screening but has no interview scheduled", "hiring", c["id"])
    for c in db.q("SELECT id, name FROM candidates WHERE stage='offer' AND id NOT IN (SELECT candidate_id FROM offers)"):
        add(2, f"{c['name']} cleared every round; prepare the offer", "hiring", c["id"], "warning")
    for i in db.q("SELECT i.candidate_id, i.round, i.scheduled_at, c.name FROM interviews i JOIN candidates c "
                  "ON c.id=i.candidate_id WHERE i.status='scheduled' AND substr(i.scheduled_at,1,10) < ?", (t,)):
        add(2, f"Record the {i['round']} result for {i['name']} (was on {i['scheduled_at'][:10]})", "hiring",
            i["candidate_id"], "warning")
    soon = (today + timedelta(days=7)).isoformat()
    for h in db.q("SELECT id, name, start_date FROM new_hires WHERE start_date BETWEEN ? AND ?", (t, soon)):
        blocked = db.q1("SELECT COUNT(*) AS n FROM onboarding_tasks WHERE hire_id=? AND status LIKE 'blocked%'", (h["id"],))
        if blocked and blocked["n"]:
            add(1, f"{h['name']} joins on {h['start_date']} with documents still missing", "ask", h["id"], "critical")
    try:
        from . import payroll
        m = config.today().strftime("%Y-%m")
        pr = payroll.summary(m)
        if config.today().day >= 25 and pr["status"] in ("not_started", "draft"):
            add(2, f"Payroll for {m} is {'not run yet' if pr['status'] == 'not_started' else 'still a draft'}", "payroll",
                m, "warning")
        elif pr["status"] == "pending_approval":
            add(2, f"Payroll for {m} is waiting for approval (net {pr['net']:,})", "approvals", m, "warning")
        for w in pr.get("warnings", [])[:3]:
            add(3, f"Payroll detail missing: {w}", "payroll", m)
    except Exception:  # payroll is optional for the attention list
        pass
    try:
        from . import projects
        for risk in projects.risks():
            if risk["severity"] in ("critical", "warning"):
                add(2 if risk["severity"] == "critical" else 3, risk["text"], "projects",
                    risk.get("project_id") or risk.get("employee_id"), risk["severity"])
    except Exception:  # projects are optional for the attention list
        pass
    try:
        from . import engage
        for e in db.q("SELECT id, title, day FROM events WHERE status='planned' AND day BETWEEN ? AND ? "
                      "AND COALESCE(budget_status,'') != 'pending_approval'",
                      (t, (today + timedelta(days=7)).isoformat())):
            add(3, f"{e['title']} is on {e['day']} and has not been announced", "culture", e["id"])
        for o in engage.occasions(3):
            add(3, f"{o['title']} on {o['day']}", "culture")      # a birthday has no page of its own to open
    except Exception:  # culture is optional for the attention list
        pass
    tk = db.q("SELECT id, title FROM tickets WHERE status='awaiting_approval'")
    for k in tk:
        add(3, f"Fix ready for review: ticket #{k['id']} {k['title']}", "tickets", k["id"])
    try:
        from .gateway import llm
        for a in llm.budget_report()["agents"]:
            if a["used_pct"] >= 80:
                add(2 if a["used_pct"] >= 100 else 3, f"The {a['agent']} agent has used {a['used_pct']}% of its AI budget",
                    "budget", a["agent"], "critical" if a["used_pct"] >= 100 else "warning")
    except Exception:  # budgets are optional for the attention list
        pass
    items.sort(key=lambda i: i["priority"])
    return items[:limit]


# ---------------------------------------------------------------- everything, and plain-language summary

def overview(job_id=None):
    h, w, lv, ob, ai, ops = hiring(job_id), workforce(), leave(), onboarding(), ai_usage(), operations()
    pay, prj, cul = payroll_numbers(), projects_numbers(), culture_numbers()
    soon = (config.today() + timedelta(days=30)).isoformat()
    week = (config.today() + timedelta(days=7)).isoformat()
    t = config.today().isoformat()
    kpis = {
        "open_jobs": (db.q1("SELECT COUNT(*) AS n FROM jobs WHERE COALESCE(status,'open')='open'") or {}).get("n", 0),
        "active_candidates": h["active"],
        "interviews_7d": (db.q1("SELECT COUNT(*) AS n FROM interviews WHERE status='scheduled' AND "
                                "substr(scheduled_at,1,10) BETWEEN ? AND ?", (t, week)) or {}).get("n", 0),
        "offers_out": (db.q1("SELECT COUNT(*) AS n FROM offers WHERE status IN ('pending_approval','sent')") or {}).get("n", 0),
        "joining_30d": (db.q1("SELECT COUNT(*) AS n FROM new_hires WHERE start_date BETWEEN ? AND ?", (t, soon)) or {}).get("n", 0),
        "followups_due": (db.q1("SELECT COUNT(*) AS n FROM followups WHERE status='open' AND due <= ?", (t,)) or {}).get("n", 0),
        "headcount": w["headcount"],
        "leave_pending": lv["pending"],
        "ai_spend_usd": ai["total"]["spent_usd"], "ai_budget_usd": ai["total"]["budget_usd"],
        "open_tickets": ops["open_tickets"],
        "payroll_net": pay["net"], "payroll_status": pay["status"], "payroll_month": pay["month"],
        "events_upcoming": len(cul["upcoming"]), "kudos_90d": cul["kudos_90d"],
        "active_projects": prj["active"], "utilisation_pct": prj["utilisation"]["average_pct"],
        "bench": prj["utilisation"]["bench"], "overdue_tasks": prj["overdue_tasks"],
    }
    return {"as_of": t, "kpis": kpis, "attention": attention(), "hiring": h, "workforce": w, "leave": lv,
            "onboarding": ob, "ai": ai, "operations": ops, "payroll": pay, "projects": prj, "culture": cul}


def narrate(data=None):
    """A short plain-language read-out of the overview (used by the insights agent without a model, and the weekly report)."""
    d = data or overview()
    k, h = d["kpis"], d["hiring"]
    f = {s["key"]: s for s in h["funnel"]}
    lines = [f"HR snapshot for {d['as_of']}:",
             f"- Hiring: {k['open_jobs']} open job(s), {k['active_candidates']} active candidate(s). "
             f"Of {f['applied']['count']} applicants, {f['screened_in']['count']} passed screening, "
             f"{f['interviewed']['count']} were interviewed, {f['offered']['count']} got offers and {f['joined']['count']} joined."]
    if h["rounds"]:
        lines.append("- Rounds: " + "; ".join(
            f"{r['round']} {r['pass']} passed, {r['fail']} failed" + (f", {r['hold']} on hold" if r["hold"] else "")
            + (f", avg rating {r['avg_rating']}" if r["avg_rating"] else "") + (f", {r['scheduled']} scheduled" if r["scheduled"] else "")
            for r in h["rounds"]))
    speed = [f"{label} {v} days" for label, v in (("to offer", h["time_to_offer_days"]), ("to acceptance", h["time_to_accept_days"]),
                                                  ("to joining", h["time_to_join_days"])) if v is not None]
    if speed:
        lines.append("- Average time from application " + ", ".join(speed) + ".")
    if h["offer_acceptance_pct"] is not None:
        lines.append(f"- Offer acceptance: {h['offer_acceptance_pct']}%.")
    if h["missing_skills"]:
        lines.append("- Most common missing must-haves among rejected resumes: " +
                     ", ".join(f"{m['skill']} ({m['candidates']})" for m in h["missing_skills"][:4]) + ".")
    lines.append(f"- People: headcount {k['headcount']}, {k['joining_30d']} joining in the next 30 days, "
                 f"{k['leave_pending']} leave request(s) pending.")
    ob = d["onboarding"]
    if ob["tasks"]:
        lines.append(f"- Onboarding: {ob['done']} of {ob['tasks']} tasks done, {ob['blocked']} blocked, {ob['overdue']} overdue.")
    if d["payroll"]["employees"] or d["payroll"]["status"] != "not_started":
        p = d["payroll"]
        lines.append(f"- Payroll {p['month']} ({p['status'].replace('_', ' ')}): net Rs {p['net']:,} for {p['employees']} "
                     f"employees, employer cost Rs {p['employer_cost']:,}.")
    if d["projects"]["projects"]:
        pj = d["projects"]
        lines.append(f"- Projects: {pj['active']} active, average utilisation {pj['utilisation']['average_pct']}%, "
                     f"{pj['utilisation']['bench']} on the bench, {pj['open_tasks']} open task(s) "
                     f"({pj['overdue_tasks']} overdue).")
    cul = d["culture"]
    if cul["events_this_year"] or cul["kudos_90d"]:
        lines.append(f"- Culture: {len(cul['upcoming'])} event(s) coming up, {cul['kudos_90d']} kudos in 90 days across "
                     f"{cul['people_recognised']} of {cul['headcount']} people.")
    lines.append(f"- AI spend this month: ${k['ai_spend_usd']:.2f} of ${k['ai_budget_usd']:.2f}. Open tickets: {k['open_tickets']}.")
    if d["attention"]:
        lines.append("Needs attention:")
        lines += [f"  {i + 1}. {a['text']}" for i, a in enumerate(d["attention"][:6])]
    else:
        lines.append("Nothing needs attention right now.")
    return "\n".join(lines)


def _cell(v):
    """Stop spreadsheet formula injection from resume text (a name like '=HYPERLINK(...)')."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def candidates_csv(job_id=None):
    """The pipeline as CSV for spreadsheets: one row per candidate with stage, score, latest round and offer."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["candidate_id", "name", "email", "phone", "job_id", "stage", "score", "years", "latest_round",
                "latest_result", "offer_ctc_lpa", "joining_date", "offer_status", "updated_at"])
    sql = "SELECT * FROM candidates" + (" WHERE job_id=?" if job_id else "") + " ORDER BY job_id, id"
    for c in db.q(sql, (job_id,) if job_id else ()):
        i = db.q1("SELECT round, result FROM interviews WHERE candidate_id=? ORDER BY id DESC LIMIT 1", (c["id"],)) or {}
        o = db.q1("SELECT ctc_lpa, joining_date, status FROM offers WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
                  (c["id"],)) or {}
        w.writerow([_cell(v) for v in [c["id"], c["name"], c["email"], c.get("phone"), c["job_id"], c.get("stage"), c["score"], c.get("years"),
                    i.get("round"), i.get("result"), o.get("ctc_lpa"), o.get("joining_date"), o.get("status"),
                    c["updated_at"]]])
    return out.getvalue()


def save_report(data=None):
    """Write the snapshot to var/reports/insights-<date>.md and return the path."""
    d = data or overview()
    path = config.home() / "reports" / f"insights-{d['as_of']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# HR insights {d['as_of']}\n\n```\n{narrate(d)}\n```\n\nGenerated {datetime.now():%Y-%m-%d %H:%M}.\n",
                    encoding="utf-8")
    return path
