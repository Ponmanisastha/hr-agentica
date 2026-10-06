"""Cultural events, recognition and the other HR activities that keep a workplace human.

Events (festivals, town halls, offsites, training, volunteering) with RSVPs, a budget that needs approval past a
threshold, and organisers. Recognition: kudos between colleagues and nominated awards. Pulse surveys with anonymous
answers. Dates worth remembering: birthdays, work anniversaries and the holiday calendar.

Nothing is announced automatically: event invitations and announcements are drafted into the outbox for a person to
send, and spending past the threshold waits for approval.
"""

import json
import statistics
from collections import Counter
from datetime import date, timedelta

from . import auth, config, db

KINDS = ("festival", "town_hall", "offsite", "training", "volunteering", "celebration", "sports", "other")
EVENT_STATUSES = ("planned", "announced", "done", "cancelled")
RSVP = ("yes", "no", "maybe")
AWARD_STATUSES = ("nominated", "shortlisted", "awarded", "declined")
DEFAULT_BUDGET_LIMIT = 25000  # rupees; past this an event's budget needs approval


class EngageError(Exception):
    pass


def _actor():
    try:
        return auth.current_user().username
    except Exception:
        return "system"


def _me():
    try:
        return auth.current_user().employee_id or ""
    except Exception:
        return ""


def _d(value, field="date"):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        raise EngageError(f"{field} must look like 2026-10-24, not {value!r}")


def _emp(ref):
    if not ref:
        raise EngageError("Which employee?")
    e = db.q1("SELECT * FROM employees WHERE upper(id)=upper(?) OR lower(name)=lower(?)", (ref, ref))
    if not e:
        e = db.q1("SELECT * FROM employees WHERE lower(name) LIKE lower(?)", (f"%{ref}%",))
    if not e:
        raise EngageError(f"No employee {ref!r}")
    return e


def budget_limit():
    return float(config.env("HRAI_EVENT_BUDGET_LIMIT", DEFAULT_BUDGET_LIMIT))


# ---------------------------------------------------------------- events

def _next_id():
    rows = db.q("SELECT id FROM events WHERE id LIKE 'EV-%'")
    nums = [int(r["id"].split("-")[1]) for r in rows if r["id"].split("-")[1].isdigit()]
    return f"EV-{(max(nums) if nums else 0) + 1:03d}"


def get(ref):
    e = db.q1("SELECT * FROM events WHERE upper(id)=upper(?) OR lower(title)=lower(?)", (ref, ref))
    if not e:
        e = db.q1("SELECT * FROM events WHERE lower(title) LIKE lower(?) ORDER BY day DESC", (f"%{ref}%",))
    if not e:
        raise EngageError(f"No event {ref!r}")
    return e


def create_event(title, day, kind="celebration", location="", organiser="", budget=0, description="",
                 audience="everyone", start_time=""):
    if not title.strip():
        raise EngageError("Give the event a title")
    if kind not in KINDS:
        raise EngageError(f"kind must be one of {', '.join(KINDS)}")
    when = _d(day, "day")
    budget = float(budget or 0)
    if budget < 0:
        raise EngageError("budget cannot be negative")
    eid = _next_id()
    db.x("INSERT INTO events (id, title, kind, day, start_time, location, organiser, budget, spent, description, "
         "audience, status, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (eid, title.strip(), kind, when.isoformat(), start_time, location, organiser or _actor(), budget, 0,
          description, audience, "planned", _actor(), db.now(), db.now()))
    out = {"event_id": eid, "title": title.strip(), "day": when.isoformat(), "budget": budget, "status": "planned"}
    if budget > budget_limit():
        aid = db.x("INSERT INTO approvals (kind, ref, summary, payload, requested_by, created_at) VALUES (?,?,?,?,?,?)",
                   ("event_budget", eid, f"Budget for {title.strip()} on {when.isoformat()}: ₹{budget:,.0f}",
                    json.dumps({"event_id": eid}), _actor(), db.now()))
        db.x("UPDATE events SET approval_id=?, budget_status='pending_approval' WHERE id=?", (aid, eid))
        out.update({"approval_id": aid, "budget_status": "pending_approval"})
    else:
        db.x("UPDATE events SET budget_status='within_limit' WHERE id=?", (eid,))
        out["budget_status"] = "within_limit"
    return out


def on_budget_decision(event_id, approved):
    db.x("UPDATE events SET budget_status=? WHERE id=?", ("approved" if approved else "rejected", event_id))


def update_event(ref, **fields):
    e = get(ref)
    allowed = {"title", "day", "start_time", "location", "organiser", "description", "audience", "status", "spent"}
    sets = {k: v for k, v in fields.items() if k in allowed and v not in (None, "")}
    if "status" in sets and sets["status"] not in EVENT_STATUSES:
        raise EngageError(f"status must be one of {', '.join(EVENT_STATUSES)}")
    if "day" in sets:
        sets["day"] = _d(sets["day"], "day").isoformat()
    if "spent" in sets:
        sets["spent"] = float(sets["spent"])
        if e["budget"] and sets["spent"] > e["budget"] * 1.1:
            raise EngageError(f"₹{sets['spent']:,.0f} is more than 10% over the ₹{e['budget']:,.0f} budget; "
                              f"raise the budget first so it goes through approval")
    if sets:
        db.x(f"UPDATE events SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?",
             (*sets.values(), db.now(), e["id"]))
    return get(e["id"])


def announce(ref):
    """Draft the invitation for the organiser to send, and mark the event announced. Nothing is emailed by itself."""
    e = get(ref)
    if e["budget_status"] == "pending_approval":
        raise EngageError(f"{e['title']} is waiting for its budget to be approved")
    if e["budget_status"] == "rejected":
        raise EngageError(f"The budget for {e['title']} was rejected")
    from . import tools as T
    body = (f"Hello everyone,\n\n{e['title']} is on {e['day']}"
            + (f" at {e['start_time']}" if e["start_time"] else "")
            + (f", {e['location']}" if e["location"] else "") + ".\n\n"
            + (e["description"] + "\n\n" if e["description"] else "")
            + "Please RSVP in the HR console under Events.\n\nRegards,\n"
            + (e["organiser"] or "HR"))
    T.run("draft_email", to=config.env("HRAI_ALL_EMAIL", "all@example.com"), subject=f"{e['title']} on {e['day']}",
          body=body)
    db.x("UPDATE events SET status='announced', updated_at=? WHERE id=?", (db.now(), e["id"]))
    return {"event_id": e["id"], "status": "announced", "draft": "in the outbox, send it when you are happy with it"}


def rsvp(ref, answer, employee="", guests=0, note=""):
    e = get(ref)
    if answer not in RSVP:
        raise EngageError("answer must be yes, no or maybe")
    emp = _emp(employee or _me())
    if _d(e["day"]) < config.today():
        raise EngageError(f"{e['title']} has already happened")
    db.x("INSERT INTO event_rsvps (event_id, employee_id, answer, guests, note, created_at) VALUES (?,?,?,?,?,?) "
         "ON CONFLICT(event_id, employee_id) DO UPDATE SET answer=excluded.answer, guests=excluded.guests, "
         "note=excluded.note, created_at=excluded.created_at",
         (e["id"], emp["id"], answer, int(guests or 0), note, db.now()))
    return {"event_id": e["id"], "title": e["title"], "employee_id": emp["id"], "answer": answer,
            "attending": attendance(e["id"])}


def attendance(ref):
    e = get(ref)
    rows = db.q("SELECT answer, guests FROM event_rsvps WHERE event_id=?", (e["id"],))
    counts = Counter(r["answer"] for r in rows)
    return {"yes": counts["yes"], "no": counts["no"], "maybe": counts["maybe"],
            "headcount": counts["yes"] + sum(r["guests"] for r in rows if r["answer"] == "yes"),
            "responded": len(rows), "employees": db.q1("SELECT COUNT(*) AS n FROM employees")["n"]}


def event_details(ref):
    e = get(ref)
    return {"event": e, "attendance": attendance(e["id"]),
            "rsvps": db.q("SELECT r.*, p.name FROM event_rsvps r JOIN employees p ON p.id=r.employee_id "
                          "WHERE r.event_id=? ORDER BY r.answer, p.name", (e["id"],))}


def calendar(days_ahead=60, include_past=False):
    """Everything coming up: events, public holidays, birthdays and work anniversaries."""
    today = config.today()
    until = today + timedelta(days=days_ahead)
    out = []
    sql = "SELECT * FROM events WHERE day<=?" + ("" if include_past else " AND day>=?") + " ORDER BY day"
    args = (until.isoformat(),) if include_past else (until.isoformat(), today.isoformat())
    for e in db.q(sql, args):
        a = attendance(e["id"])
        out.append({"day": e["day"], "type": "event", "id": e["id"], "title": e["title"], "kind": e["kind"],
                    "status": e["status"], "location": e["location"], "budget": e["budget"],
                    "budget_status": e["budget_status"], "attending": a["headcount"], "responded": a["responded"]})
    for h in db.q("SELECT day FROM holidays WHERE day BETWEEN ? AND ? ORDER BY day", (today.isoformat(), until.isoformat())):
        out.append({"day": h["day"], "type": "holiday", "title": "Public holiday"})
    out += occasions(days_ahead)
    return sorted(out, key=lambda x: x["day"])


def occasions(days_ahead=30):
    """Birthdays and work anniversaries in the next few days (the date in the year this one falls in)."""
    today = config.today()
    out = []
    for e in db.q("SELECT id, name, date_of_birth, joined_on FROM employees"):
        for field, label in (("date_of_birth", "birthday"), ("joined_on", "work anniversary")):
            if not e[field]:
                continue
            src = _d(e[field])
            for year in (today.year, today.year + 1):
                try:
                    when = src.replace(year=year)
                except ValueError:                      # 29 February in a year that has none
                    when = date(year, 3, 1)
                if 0 <= (when - today).days <= days_ahead:
                    years = year - src.year
                    out.append({"day": when.isoformat(), "type": "occasion", "kind": label, "employee_id": e["id"],
                                "title": f"{e['name']}'s {label}"
                                         + (f" ({years} year{'s' if years != 1 else ''})" if label != "birthday" and years else "")})
                    break
    return sorted(out, key=lambda x: x["day"])


# ---------------------------------------------------------------- recognition

def give_kudos(to, message, from_employee="", value=""):
    """A thank-you from one colleague to another. Visible to everyone; it is not an award and costs nothing."""
    receiver = _emp(to)
    giver_id = (_emp(from_employee)["id"] if from_employee else _me())
    if not message.strip():
        raise EngageError("Say what they did; a kudos without words helps nobody")
    if giver_id and giver_id == receiver["id"]:
        raise EngageError("Kudos go to someone else")
    kid = db.x("INSERT INTO kudos (from_id, to_id, message, value, created_at) VALUES (?,?,?,?,?)",
               (giver_id, receiver["id"], message.strip()[:500], value, db.now()))
    return {"kudos_id": kid, "to": receiver["name"], "to_id": receiver["id"], "message": message.strip()[:500]}


def kudos_wall(days=90, employee=""):
    since = (config.today() - timedelta(days=days)).isoformat()
    sql = ("SELECT k.*, t.name AS to_name, f.name AS from_name FROM kudos k JOIN employees t ON t.id=k.to_id "
           "LEFT JOIN employees f ON f.id=k.from_id WHERE k.created_at>=?")
    args = [since]
    if employee:
        sql += " AND k.to_id=?"
        args.append(_emp(employee)["id"])
    rows = db.q(sql + " ORDER BY k.id DESC LIMIT 100", args)
    top = Counter(r["to_name"] for r in rows)
    return {"since": since, "kudos": rows, "top": [{"name": n, "count": c} for n, c in top.most_common(5)]}


def nominate(award, employee, reason, cycle=""):
    """Nominate someone for an award. HR decides the result; nobody is awarded automatically."""
    e = _emp(employee)
    if not reason.strip():
        raise EngageError("Give a reason for the nomination")
    cycle = cycle or config.today().strftime("%Y-%m")
    nid = db.x("INSERT INTO awards (award, employee_id, reason, cycle, status, nominated_by, created_at) "
               "VALUES (?,?,?,?,?,?,?)", (award.strip(), e["id"], reason.strip()[:500], cycle, "nominated",
                                          _actor(), db.now()))
    return {"nomination_id": nid, "award": award.strip(), "employee_id": e["id"], "name": e["name"], "cycle": cycle,
            "status": "nominated"}


def decide_award(nomination_id, status, note=""):
    if status not in AWARD_STATUSES:
        raise EngageError(f"status must be one of {', '.join(AWARD_STATUSES)}")
    n = db.q1("SELECT * FROM awards WHERE id=?", (nomination_id,))
    if not n:
        raise EngageError(f"No nomination {nomination_id}")
    db.x("UPDATE awards SET status=?, note=?, decided_by=?, decided_at=? WHERE id=?",
         (status, note, _actor(), db.now(), nomination_id))
    if status == "awarded":
        e = db.q1("SELECT name, email FROM employees WHERE id=?", (n["employee_id"],))
        from . import tools as T
        T.run("draft_email", to=e["email"], subject=f"Congratulations: {n['award']}",
              body=f"Dear {e['name']},\n\nYou have been chosen for the {n['award']} award ({n['cycle']}).\n\n"
                   f"{n['reason']}\n\nCongratulations and thank you.\n\nRegards,\nHR")
    return {"nomination_id": nomination_id, "status": status}


def awards(cycle="", status=""):
    sql = ("SELECT a.*, e.name FROM awards a JOIN employees e ON e.id=a.employee_id WHERE 1=1")
    args = []
    for field, value in (("cycle", cycle), ("status", status)):
        if value:
            sql += f" AND a.{field}=?"
            args.append(value)
    return db.q(sql + " ORDER BY a.id DESC", args)


# ---------------------------------------------------------------- pulse surveys

def start_survey(title, question, scale_max=5, closes=None, audience="everyone"):
    """A one-question pulse survey. Answers are stored without the person's id, so results cannot be traced back."""
    if not question.strip():
        raise EngageError("Give the survey a question")
    if not 2 <= int(scale_max) <= 10:
        raise EngageError("scale_max must be between 2 and 10")
    close = _d(closes, "closes") if closes else config.today() + timedelta(days=7)
    sid = db.x("INSERT INTO surveys (title, question, scale_max, audience, closes, status, created_by, created_at) "
               "VALUES (?,?,?,?,?,?,?,?)", (title.strip(), question.strip(), int(scale_max), audience,
                                            close.isoformat(), "open", _actor(), db.now()))
    return {"survey_id": sid, "title": title.strip(), "closes": close.isoformat(), "status": "open"}


def answer_survey(survey_id, score, comment=""):
    """Record one answer. Who answered is kept only as a hash, so people cannot be identified from the results."""
    import hashlib
    s = db.q1("SELECT * FROM surveys WHERE id=?", (survey_id,))
    if not s:
        raise EngageError(f"No survey {survey_id}")
    if s["status"] != "open" or _d(s["closes"]) < config.today():
        raise EngageError(f"'{s['title']}' is closed")
    score = int(score)
    if not 1 <= score <= s["scale_max"]:
        raise EngageError(f"score must be between 1 and {s['scale_max']}")
    who = hashlib.sha256(f"{survey_id}:{_me() or _actor()}".encode()).hexdigest()
    if db.q1("SELECT 1 AS y FROM survey_answers WHERE survey_id=? AND respondent_hash=?", (survey_id, who)):
        raise EngageError("You have already answered this survey")
    db.x("INSERT INTO survey_answers (survey_id, respondent_hash, score, comment, created_at) VALUES (?,?,?,?,?)",
         (survey_id, who, score, comment.strip()[:500], db.now()))
    return {"survey_id": survey_id, "recorded": True, "anonymous": True}


def survey_results(survey_id, min_answers=3):
    """Scores and comments, but only once enough people have answered that nobody can be picked out."""
    s = db.q1("SELECT * FROM surveys WHERE id=?", (survey_id,))
    if not s:
        raise EngageError(f"No survey {survey_id}")
    rows = db.q("SELECT score, comment FROM survey_answers WHERE survey_id=?", (survey_id,))
    out = {"survey_id": survey_id, "title": s["title"], "question": s["question"], "scale_max": s["scale_max"],
           "closes": s["closes"], "status": s["status"], "answers": len(rows),
           "employees": db.q1("SELECT COUNT(*) AS n FROM employees")["n"]}
    if len(rows) < min_answers:
        out["note"] = f"Results appear once {min_answers} people have answered ({len(rows)} so far)"
        return out
    scores = [r["score"] for r in rows]
    out.update({"average": round(statistics.mean(scores), 2), "median": statistics.median(scores),
                "distribution": [{"score": i, "count": sum(x == i for x in scores)} for i in range(1, s["scale_max"] + 1)],
                "comments": [r["comment"] for r in rows if r["comment"]]})
    return out


def surveys(open_only=True):
    rows = db.q("SELECT * FROM surveys" + (" WHERE status='open'" if open_only else "") + " ORDER BY id DESC")
    for r in rows:
        r["answers"] = db.q1("SELECT COUNT(*) AS n FROM survey_answers WHERE survey_id=?", (r["id"],))["n"]
    return rows


def close_survey(survey_id):
    db.x("UPDATE surveys SET status='closed' WHERE id=?", (survey_id,))
    return survey_results(survey_id)


# ---------------------------------------------------------------- overview

def engagement():
    """The numbers behind culture: events, attendance, kudos, awards and pulse scores."""
    today = config.today()
    year = today.strftime("%Y")
    evs = db.q("SELECT * FROM events WHERE day LIKE ?", (f"{year}%",))
    done = [e for e in evs if e["status"] == "done"]
    att = [attendance(e["id"]) for e in evs]
    headcount = db.q1("SELECT COUNT(*) AS n FROM employees")["n"] or 1
    k = db.q("SELECT created_at, to_id FROM kudos WHERE created_at >= ?", ((today - timedelta(days=90)).isoformat(),))
    open_surveys = surveys(True)
    scores = []
    for s in surveys(False):
        res = survey_results(s["id"])
        if "average" in res:
            scores.append({"survey_id": s["id"], "title": s["title"], "average": res["average"],
                           "answers": res["answers"], "closes": s["closes"]})
    return {"events_this_year": len(evs), "events_done": len(done),
            "upcoming": [e for e in calendar(60) if e["type"] == "event"],
            "budget_this_year": round(sum(e["budget"] or 0 for e in evs)),
            "spent_this_year": round(sum(e["spent"] or 0 for e in evs)),
            "average_attendance": round(sum(a["headcount"] for a in att) / len(att), 1) if att else 0,
            "response_rate_pct": round(100 * sum(a["responded"] for a in att) / (len(att) * headcount)) if att else None,
            "kudos_90d": len(k), "people_recognised": len({r["to_id"] for r in k}), "headcount": headcount,
            "awards": Counter(a["status"] for a in awards()), "open_surveys": len(open_surveys),
            "pulse": scores[:6], "by_kind": [{"kind": k2, "count": c} for k2, c in
                                             Counter(e["kind"] for e in evs).most_common()]}


def summary():
    e = engagement()
    lines = [f"{e['events_this_year']} event(s) this year, {e['events_done']} already held; "
             f"₹{e['spent_this_year']:,} spent of ₹{e['budget_this_year']:,} budgeted."]
    up = e["upcoming"][:5]
    if up:
        lines.append("Coming up: " + "; ".join(f"{x['title']} on {x['day']} ({x['attending']} attending)" for x in up))
    occ = occasions(14)
    if occ:
        lines.append("In the next two weeks: " + "; ".join(f"{o['title']} on {o['day']}" for o in occ[:6]))
    lines.append(f"{e['kudos_90d']} kudos in the last 90 days across {e['people_recognised']} of {e['headcount']} people.")
    if e["pulse"]:
        lines.append("Pulse: " + "; ".join(f"{p['title']} {p['average']}/5 from {p['answers']} answers" for p in e["pulse"][:3]))
    return "\n".join(lines)


def seed_sample_events():
    if db.q1("SELECT 1 AS y FROM events LIMIT 1"):
        return
    today = config.today()
    create_event("Diwali celebration", (today + timedelta(days=14)).isoformat(), "festival", "Office atrium",
                 "Lakshmi Menon", 20000, "Lunch, rangoli and prizes. Families welcome.")
    create_event("Quarterly town hall", (today + timedelta(days=7)).isoformat(), "town_hall", "Main hall",
                 "Lakshmi Menon", 0, "Business update and open questions.", start_time="16:00")
