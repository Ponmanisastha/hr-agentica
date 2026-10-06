"""Project management from an HR point of view: projects, who is on them, tasks, timesheets and utilisation.

The question this answers is "who is working on what, who is free, and what is slipping" — staffing and capacity,
not a replacement for an engineering tracker. Allocations are a percentage of someone's time between two dates, so
over-allocation, the bench and utilisation all fall out of the same numbers. Approved leave is taken off capacity.
"""

import json
from collections import defaultdict
from datetime import date, timedelta

from . import auth, config, db

STATUSES = ("planned", "active", "on_hold", "done", "cancelled")
TASK_STATUSES = ("todo", "in_progress", "blocked", "done")
HEALTHS = ("on_track", "at_risk", "off_track")
FULL_DAY_HOURS = 8


class ProjectError(Exception):
    pass


def _actor():
    try:
        return auth.current_user().username
    except Exception:
        return "system"


def _d(value, field="date"):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        raise ProjectError(f"{field} must look like 2026-10-06, not {value!r}")


def _emp(ref):
    e = db.q1("SELECT * FROM employees WHERE upper(id)=upper(?) OR lower(name)=lower(?)", (ref, ref))
    if not e:
        e = db.q1("SELECT * FROM employees WHERE lower(name) LIKE lower(?)", (f"%{ref}%",))
    if not e:
        raise ProjectError(f"No employee {ref!r}")
    return e


def get(ref):
    p = db.q1("SELECT * FROM projects WHERE upper(id)=upper(?) OR lower(name)=lower(?)", (ref, ref))
    if not p:
        p = db.q1("SELECT * FROM projects WHERE lower(name) LIKE lower(?)", (f"%{ref}%",))
    if not p:
        raise ProjectError(f"No project {ref!r}")
    p["skills"] = json.loads(p["skills"] or "[]")
    return p


def _next_id():
    rows = db.q("SELECT id FROM projects WHERE id LIKE 'P-%'")
    nums = [int(r["id"].split("-")[1]) for r in rows if r["id"].split("-")[1].isdigit()]
    return f"P-{(max(nums) if nums else 0) + 1:03d}"


def log(project_id, event, detail=""):
    db.x("INSERT INTO project_events (project_id, ts, actor, event, detail) VALUES (?,?,?,?,?)",
         (project_id, db.now(), _actor(), event, str(detail)[:500]))


# ---------------------------------------------------------------- projects

def create(name, client="", manager="", start_date=None, end_date=None, skills=None, status="active", notes=""):
    if not name.strip():
        raise ProjectError("Give the project a name")
    if status not in STATUSES:
        raise ProjectError(f"status must be one of {', '.join(STATUSES)}")
    start = _d(start_date or config.today().isoformat(), "start_date")
    end = _d(end_date, "end_date") if end_date else None
    if end and end < start:
        raise ProjectError("The end date is before the start date")
    pid = _next_id()
    db.x("INSERT INTO projects (id, name, client, manager, start_date, end_date, status, health, skills, notes, "
         "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (pid, name.strip(), client, manager, start.isoformat(), end.isoformat() if end else None, status, "on_track",
          json.dumps(skills or []), notes, _actor(), db.now(), db.now()))
    log(pid, "created", name)
    return get(pid)


def update(ref, **fields):
    p = get(ref)
    allowed = {"name", "client", "manager", "start_date", "end_date", "status", "health", "notes", "skills"}
    sets = {k: v for k, v in fields.items() if k in allowed and v not in (None, "")}
    if "status" in sets and sets["status"] not in STATUSES:
        raise ProjectError(f"status must be one of {', '.join(STATUSES)}")
    if "health" in sets and sets["health"] not in HEALTHS:
        raise ProjectError(f"health must be one of {', '.join(HEALTHS)}")
    for k in ("start_date", "end_date"):
        if k in sets:
            sets[k] = _d(sets[k], k).isoformat()
    if "skills" in sets:
        sets["skills"] = json.dumps(sets["skills"])
    if sets:
        db.x(f"UPDATE projects SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?",
             (*sets.values(), db.now(), p["id"]))
        log(p["id"], "updated", ", ".join(sorted(sets)))
    return get(p["id"])


# ---------------------------------------------------------------- allocations

def allocate(employee, project, percent=100, role="", start_date=None, end_date=None):
    """Put someone on a project for a share of their time. Refuses to take anyone past 100%."""
    e, p = _emp(employee), get(project)
    percent = float(percent)
    if not 1 <= percent <= 100:
        raise ProjectError("percent must be between 1 and 100")
    start = _d(start_date or max(config.today().isoformat(), p["start_date"]), "start_date")
    end = _d(end_date, "end_date") if end_date else (_d(p["end_date"]) if p["end_date"] else None)
    if end and end < start:
        raise ProjectError("The end date is before the start date")
    busy = allocated_percent(e["id"], start, end)
    if busy + percent > 100:
        raise ProjectError(f"{e['name']} is already {busy:g}% allocated in that period; only {100 - busy:g}% is free")
    aid = db.x("INSERT INTO allocations (employee_id, project_id, percent, role, start_date, end_date, status, "
               "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (e["id"], p["id"], percent, role, start.isoformat(), end.isoformat() if end else None, "active",
                _actor(), db.now()))
    log(p["id"], "allocated", f"{e['name']} {percent:g}% {role}".strip())
    return {"allocation_id": aid, "employee_id": e["id"], "name": e["name"], "project_id": p["id"],
            "project": p["name"], "percent": percent, "role": role, "start_date": start.isoformat(),
            "end_date": end.isoformat() if end else None, "now_allocated_pct": busy + percent}


def release(allocation_id, end_date=None, note=""):
    """End an allocation (today by default), leaving the history behind."""
    a = db.q1("SELECT * FROM allocations WHERE id=?", (allocation_id,))
    if not a:
        raise ProjectError(f"No allocation {allocation_id}")
    end = _d(end_date or config.today().isoformat(), "end_date")
    db.x("UPDATE allocations SET end_date=?, status='ended' WHERE id=?", (end.isoformat(), allocation_id))
    e = db.q1("SELECT name FROM employees WHERE id=?", (a["employee_id"],))
    log(a["project_id"], "released", f"{e['name']} from {end.isoformat()}. {note}".strip())
    return {"allocation_id": allocation_id, "end_date": end.isoformat()}


def _overlaps(a_start, a_end, start, end):
    a_start, a_end = _d(a_start), _d(a_end) if a_end else None
    if end and a_start > end:
        return False
    return not (a_end and a_end < start)


def allocated_percent(employee_id, start=None, end=None):
    """How much of someone's time is already committed in a period (overlapping allocations added up)."""
    start = start or config.today()
    rows = db.q("SELECT percent, start_date, end_date FROM allocations WHERE employee_id=? AND status='active'",
                (employee_id,))
    return sum(r["percent"] for r in rows if _overlaps(r["start_date"], r["end_date"], start, end))


def team(project_ref):
    p = get(project_ref)
    rows = db.q("SELECT a.*, e.name, e.department, e.level FROM allocations a JOIN employees e ON e.id=a.employee_id "
                "WHERE a.project_id=? ORDER BY a.status, e.name", (p["id"],))
    today = config.today()
    for r in rows:
        r["current"] = r["status"] == "active" and _overlaps(r["start_date"], r["end_date"], today, today)
    return rows


def assignments(employee):
    e = _emp(employee)
    rows = db.q("SELECT a.*, p.name AS project, p.status AS project_status, p.health FROM allocations a "
                "JOIN projects p ON p.id=a.project_id WHERE a.employee_id=? ORDER BY a.start_date DESC", (e["id"],))
    today = config.today()
    current = [r for r in rows if r["status"] == "active" and _overlaps(r["start_date"], r["end_date"], today, today)]
    return {"employee_id": e["id"], "name": e["name"], "allocated_pct": sum(r["percent"] for r in current),
            "current": current, "history": [r for r in rows if r not in current]}


# ---------------------------------------------------------------- tasks

def add_task(project, title, owner="", due=None, status="todo", kind="task", estimate_hours=0):
    p = get(project)
    if status not in TASK_STATUSES:
        raise ProjectError(f"status must be one of {', '.join(TASK_STATUSES)}")
    if not title.strip():
        raise ProjectError("Give the task a title")
    owner_id = _emp(owner)["id"] if owner else ""
    tid = db.x("INSERT INTO project_tasks (project_id, title, owner_id, due, status, kind, estimate_hours, created_by, "
               "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (p["id"], title.strip(), owner_id, _d(due, "due").isoformat() if due else None, status, kind,
                float(estimate_hours or 0), _actor(), db.now(), db.now()))
    log(p["id"], f"{kind}_added", title)
    return {"task_id": tid, "project_id": p["id"], "title": title.strip(), "owner_id": owner_id, "status": status}


def set_task(task_id, status=None, owner=None, due=None, note=""):
    t = db.q1("SELECT * FROM project_tasks WHERE id=?", (task_id,))
    if not t:
        raise ProjectError(f"No task {task_id}")
    sets = {}
    if status:
        if status not in TASK_STATUSES:
            raise ProjectError(f"status must be one of {', '.join(TASK_STATUSES)}")
        sets["status"] = status
    if owner:
        sets["owner_id"] = _emp(owner)["id"]
    if due:
        sets["due"] = _d(due, "due").isoformat()
    if note:
        sets["note"] = note
    if sets:
        db.x(f"UPDATE project_tasks SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?",
             (*sets.values(), db.now(), task_id))
        log(t["project_id"], "task_updated", f"#{task_id} {t['title']}: " + ", ".join(f"{k} {v}" for k, v in sets.items()))
    return {"task_id": task_id, **sets}


def tasks(project=None, owner=None, open_only=True):
    sql = ("SELECT t.*, p.name AS project, e.name AS owner FROM project_tasks t JOIN projects p ON p.id=t.project_id "
           "LEFT JOIN employees e ON e.id=t.owner_id WHERE 1=1")
    args = []
    if project:
        sql += " AND t.project_id=?"
        args.append(get(project)["id"])
    if owner:
        sql += " AND t.owner_id=?"
        args.append(_emp(owner)["id"])
    if open_only:
        sql += " AND t.status!='done'"
    rows = db.q(sql + " ORDER BY COALESCE(t.due,'9999'), t.id", args)
    today = config.today().isoformat()
    for r in rows:
        r["overdue"] = bool(r["due"] and r["due"] < today and r["status"] != "done")
    return rows


# ---------------------------------------------------------------- timesheets

def log_hours(employee, project, day, hours, note=""):
    e, p = _emp(employee), get(project)
    hours = float(hours)
    if not 0 < hours <= 16:
        raise ProjectError("hours must be between 0 and 16")
    d = _d(day, "day")
    if d > config.today():
        raise ProjectError("You cannot log hours for a future day")
    same = db.q1("SELECT COALESCE(SUM(hours),0) AS h FROM timesheets WHERE employee_id=? AND day=?",
                 (e["id"], d.isoformat()))["h"]
    if same + hours > 16:
        raise ProjectError(f"{e['name']} already has {same:g} hours on {d.isoformat()}")
    tid = db.x("INSERT INTO timesheets (employee_id, project_id, day, hours, note, created_at) VALUES (?,?,?,?,?,?)",
               (e["id"], p["id"], d.isoformat(), hours, note, db.now()))
    return {"timesheet_id": tid, "employee_id": e["id"], "project_id": p["id"], "day": d.isoformat(), "hours": hours,
            "day_total": same + hours}


def timesheet(employee=None, project=None, days=7):
    since = (config.today() - timedelta(days=days - 1)).isoformat()
    sql = ("SELECT t.*, e.name, p.name AS project FROM timesheets t JOIN employees e ON e.id=t.employee_id "
           "JOIN projects p ON p.id=t.project_id WHERE t.day>=?")
    args = [since]
    if employee:
        sql += " AND t.employee_id=?"
        args.append(_emp(employee)["id"])
    if project:
        sql += " AND t.project_id=?"
        args.append(get(project)["id"])
    rows = db.q(sql + " ORDER BY t.day DESC, e.name", args)
    by_project = defaultdict(float)
    for r in rows:
        by_project[r["project"]] += r["hours"]
    return {"from": since, "entries": rows, "total_hours": round(sum(r["hours"] for r in rows), 1),
            "by_project": [{"project": k, "hours": round(v, 1)} for k, v in sorted(by_project.items(), key=lambda kv: -kv[1])]}


# ---------------------------------------------------------------- capacity and reporting

def capacity(weeks=4):
    """Everyone's committed time over the next few weeks, with approved leave taken off."""
    start, end = config.today(), config.today() + timedelta(weeks=weeks)
    out = []
    for e in db.q("SELECT id, name, department, level FROM employees ORDER BY name"):
        rows = db.q("SELECT a.*, p.name AS project FROM allocations a JOIN projects p ON p.id=a.project_id "
                    "WHERE a.employee_id=? AND a.status='active'", (e["id"],))
        on = [r for r in rows if _overlaps(r["start_date"], r["end_date"], start, end)]
        pct = sum(r["percent"] for r in on)
        leave = db.q1("SELECT COALESCE(SUM(working_days),0) AS d FROM leave_requests WHERE employee_id=? AND "
                      "status='approved' AND start_date<=? AND end_date>=?",
                      (e["id"], end.isoformat(), start.isoformat()))["d"]
        out.append({**e, "allocated_pct": pct, "free_pct": max(0, 100 - pct), "over_allocated": pct > 100,
                    "projects": [{"project_id": r["project_id"], "project": r["project"], "percent": r["percent"],
                                  "role": r["role"], "until": r["end_date"]} for r in on],
                    "leave_days": leave})
    return out


def bench(weeks=4, threshold=50):
    return [c for c in capacity(weeks) if c["allocated_pct"] <= threshold]


def utilisation(weeks=4):
    rows = capacity(weeks)
    if not rows:
        return {"people": 0, "average_pct": 0, "bench": 0, "over_allocated": 0}
    total = sum(r["allocated_pct"] for r in rows)
    return {"people": len(rows), "average_pct": round(total / len(rows), 1),
            "bench": sum(r["allocated_pct"] <= 50 for r in rows),
            "over_allocated": sum(r["over_allocated"] for r in rows),
            "fully_booked": sum(r["allocated_pct"] >= 100 for r in rows)}


def board(status=None):
    """Projects with their team size, open tasks, overdue tasks and hours logged this month."""
    sql = "SELECT * FROM projects" + (" WHERE status=?" if status else "") + " ORDER BY status, name"
    out = []
    month = config.today().strftime("%Y-%m")
    for p in db.q(sql, (status,) if status else ()):
        t = db.q1("SELECT COUNT(*) AS n, SUM(status!='done') AS open_n, "
                  "SUM(status!='done' AND due IS NOT NULL AND due<?) AS overdue_n FROM project_tasks WHERE project_id=?",
                  (config.today().isoformat(), p["id"]))
        people = db.q1("SELECT COUNT(*) AS n, COALESCE(SUM(percent),0) AS pct FROM allocations WHERE project_id=? AND "
                       "status='active'", (p["id"],))
        hours = db.q1("SELECT COALESCE(SUM(hours),0) AS h FROM timesheets WHERE project_id=? AND day LIKE ?",
                      (p["id"], f"{month}%"))["h"]
        out.append({**p, "skills": json.loads(p["skills"] or "[]"), "team_size": people["n"],
                    "fte": round(people["pct"] / 100, 2), "tasks": t["n"], "open_tasks": t["open_n"] or 0,
                    "overdue_tasks": t["overdue_n"] or 0, "hours_this_month": round(hours, 1),
                    "days_left": (_d(p["end_date"]) - config.today()).days if p["end_date"] else None})
    return out


def risks():
    """What a manager should look at: overdue tasks, projects without a team, people over 100%, ending allocations."""
    out = []
    today = config.today()
    for p in board():
        if p["status"] not in ("active", "planned"):
            continue
        if p["overdue_tasks"]:
            out.append({"kind": "overdue_tasks", "project_id": p["id"], "project": p["name"],
                        "text": f"{p['name']} has {p['overdue_tasks']} overdue task(s)", "severity": "warning"})
        if p["status"] == "active" and not p["team_size"]:
            out.append({"kind": "no_team", "project_id": p["id"], "project": p["name"],
                        "text": f"{p['name']} is active with nobody allocated", "severity": "critical"})
        if p["days_left"] is not None and 0 <= p["days_left"] <= 14 and p["open_tasks"]:
            out.append({"kind": "ending_soon", "project_id": p["id"], "project": p["name"],
                        "text": f"{p['name']} ends in {p['days_left']} day(s) with {p['open_tasks']} task(s) open",
                        "severity": "warning"})
        if p["health"] in ("at_risk", "off_track"):
            out.append({"kind": "health", "project_id": p["id"], "project": p["name"],
                        "text": f"{p['name']} is marked {p['health'].replace('_', ' ')}", "severity": "warning"})
    for c in capacity():
        if c["over_allocated"]:
            out.append({"kind": "over_allocated", "employee_id": c["id"], "text":
                        f"{c['name']} is allocated {c['allocated_pct']:g}% across "
                        f"{len(c['projects'])} projects", "severity": "warning"})
    for p in board("active"):
        gap = staffing_gap(p["id"])
        if gap["gaps"]:
            who = ", ".join(x["name"] for x in gap["suggestions"][:2])
            out.append({"kind": "skill_gap", "project_id": p["id"], "project": p["name"], "severity": "warning",
                        "text": f"Nobody on {p['name']} has {', '.join(gap['gaps'])}" +
                                (f"; {who} is free and has it" if who else "; consider hiring or training")})
    soon = (today + timedelta(days=14)).isoformat()
    for a in db.q("SELECT a.*, e.name, p.name AS project FROM allocations a JOIN employees e ON e.id=a.employee_id "
                  "JOIN projects p ON p.id=a.project_id WHERE a.status='active' AND a.end_date BETWEEN ? AND ?",
                  (today.isoformat(), soon)):
        out.append({"kind": "rolling_off", "employee_id": a["employee_id"], "project_id": a["project_id"],
                    "text": f"{a['name']} rolls off {a['project']} on {a['end_date']}", "severity": "info"})
    return out


def staffing_gap(project_ref):
    """Which of a project's required skills nobody on it has, and who in the company has them and is free."""
    p = get(project_ref)
    if not p["skills"]:
        return {"project_id": p["id"], "needed": [], "gaps": [], "suggestions": []}
    on = {t["employee_id"] for t in team(p["id"]) if t["current"]}
    free = {c["id"]: c for c in capacity() if c["free_pct"] >= 20}
    have, suggestions = set(), []
    for e in db.q("SELECT id, name FROM employees"):
        skills = {s.lower() for s in json.loads(db.q1("SELECT COALESCE(skills,'[]') AS s FROM employees WHERE id=?",
                                                     (e["id"],))["s"] or "[]")}
        match = [s for s in p["skills"] if s.lower() in skills]
        if not match:
            continue
        if e["id"] in on:
            have.update(match)
        elif e["id"] in free:
            suggestions.append({"employee_id": e["id"], "name": e["name"], "skills": match,
                                "free_pct": free[e["id"]]["free_pct"]})
    gaps = [s for s in p["skills"] if s not in have]
    return {"project_id": p["id"], "project": p["name"], "needed": p["skills"], "gaps": gaps,
            "suggestions": sorted(suggestions, key=lambda s: -s["free_pct"])[:5]}


def summary(status=None):
    rows = board(status)
    u = utilisation()
    lines = [f"{len(rows)} project(s): " + ", ".join(f"{p['name']} ({p['status']}, {p['team_size']} people, "
             f"{p['open_tasks']} open task(s))" for p in rows[:8])] if rows else ["No projects yet."]
    lines.append(f"Utilisation: average {u['average_pct']}% across {u['people']} people, {u['bench']} on the bench, "
                 f"{u['over_allocated']} over-allocated.")
    r = risks()
    if r:
        lines.append("Watch: " + "; ".join(x["text"] for x in r[:5]))
    return "\n".join(lines)


def seed_sample_projects():
    """Two sample projects with a team, tasks and a few hours logged (only when there are none)."""
    if db.q1("SELECT 1 AS y FROM projects LIMIT 1"):
        return
    today = config.today()
    a = create("Customer portal revamp", "Acme Retail", "Arun Kumar", today.isoformat(),
               (today + timedelta(days=90)).isoformat(), ["python", "rest api", "react"], notes="Phase 1 of 2")
    b = create("Payroll data migration", "Internal", "Lakshmi Menon", (today - timedelta(days=20)).isoformat(),
               (today + timedelta(days=10)).isoformat(), ["sql", "python"])
    try:
        allocate("E101", a["id"], 60, "Backend engineer")
        allocate("E102", a["id"], 50, "Tech lead")
        allocate("E101", b["id"], 30, "Data engineer")
    except ProjectError:
        pass
    add_task(a["id"], "Design the account pages", "E102", (today + timedelta(days=7)).isoformat())
    add_task(a["id"], "Build the login API", "E101", (today - timedelta(days=2)).isoformat(), "in_progress")
    add_task(a["id"], "Phase 1 demo to the client", "", (today + timedelta(days=30)).isoformat(), kind="milestone")
    add_task(b["id"], "Map the old salary tables", "E101", (today + timedelta(days=3)).isoformat(), "done")
    for i in range(1, 4):
        day = (today - timedelta(days=i)).isoformat()
        log_hours("E101", a["id"], day, 5, "portal work")
        log_hours("E101", b["id"], day, 2, "migration")
