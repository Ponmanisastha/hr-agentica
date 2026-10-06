"""KAG: knowledge-augmented generation with a small knowledge graph in SQLite (table kg_triples).

Why it helps here: HR answers often hinge on exact relationships and numbers ("who approves Sanjay's leave",
"how many days' notice does annual leave need", "which skills does JOB-101 require"). Vector search returns
roughly-similar paragraphs; the graph returns the exact fact. The leave rules engine reads its thresholds
from the same graph, so the agent's explanation and the decision come from one source of truth.

Facts come from the HR tables (reporting lines, departments, job skills) and from the policy handbook
(leave rules, extracted with patterns below; `extract_with_llm` can add more when a model is available).
"""

import json
import re

from .. import db
from . import vectors

# (subject, predicate, regex on the handbook section, section title prefix)
RULE_PATTERNS = [
    ("annual leave", "days_per_year", r"earns (\d+) days of annual leave", "1."),
    ("annual leave", "notice_days", r"at least (\d+) calendar days in advance", "1."),
    ("annual leave", "auto_approve_max_days", r"up to (\d+) working days", "1."),
    ("annual leave", "carry_forward_days", r"Up to (\d+) unused days", "1."),
    ("sick leave", "days_per_year", r"get (\d+) days of paid sick leave", "2."),
    ("sick leave", "certificate_after_days", r"more than (\d+) consecutive working days", "2."),
    ("casual leave", "days_per_year", r"get (\d+) days of casual leave", "3."),
    ("casual leave", "max_consecutive_days", r"at most (\d+) consecutive days", "3."),
    ("casual leave", "notice_days", r"at least (\d+) day in advance", "3."),
    ("maternity leave", "weeks", r"Maternity leave is (\d+) weeks", "5."),
    ("paternity leave", "working_days", r"Paternity leave is (\d+) working days", "5."),
    ("notice period", "days_L3_and_above", r"notice period is (\d+) days for employees at level L3", "6."),
    ("notice period", "days_other_levels", r"and (\d+) days for others", "6."),
    ("work from home", "max_days_per_week", r"up to (\d+) days a week", "7."),
    ("internet reimbursement", "cap_rupees_per_month", r"capped at ([\d,]+) rupees", "8."),
]
DEFAULT_RULES = {  # used only if the handbook text no longer matches a pattern
    ("annual leave", "notice_days"): 7, ("annual leave", "auto_approve_max_days"): 5,
    ("sick leave", "certificate_after_days"): 2, ("casual leave", "max_consecutive_days"): 2,
    ("casual leave", "notice_days"): 1,
}


def add(subject, predicate, obj, source):
    db.x("INSERT OR REPLACE INTO kg_triples VALUES (?,?,?,?)", (str(subject), predicate, str(obj), source))


def build():
    db.x("DELETE FROM kg_triples")
    for e in db.q("SELECT * FROM employees"):
        add(e["id"], "name", e["name"], "employees")
        add(e["name"], "employee_id", e["id"], "employees")
        add(e["name"], "reports_to", e["manager"], "employees")
        add(e["manager"], "approves_leave_for", e["name"], "employees")
        add(e["name"], "department", e["department"], "employees")
        add(e["name"], "level", e["level"], "employees")
    for h in db.q("SELECT * FROM new_hires"):
        add(h["name"], "new_hire_id", h["id"], "new_hires")
        add(h["name"], "joins_as", h["role"], "new_hires")
        add(h["name"], "reports_to", h["manager"], "new_hires")
        add(h["name"], "start_date", h["start_date"], "new_hires")
    for j in db.q("SELECT * FROM jobs"):
        add(j["id"], "title", j["title"], "jobs")
        for s in json.loads(j["must_have"]):
            add(j["id"], "requires_skill", s, "jobs")
        for s in json.loads(j["nice_to_have"]):
            add(j["id"], "prefers_skill", s, "jobs")
        add(j["id"], "min_years", j["min_years"], "jobs")
    for sec in vectors.sections(vectors.handbook_text(), "policy_handbook"):
        title = sec["meta"]["section"]
        for subj, pred, pattern, prefix in RULE_PATTERNS:
            if title.startswith(prefix):
                m = re.search(pattern, sec["text"])
                if m:
                    add(subj, pred, m.group(1).replace(",", ""), f"handbook section {title}")
    return db.q1("SELECT COUNT(*) AS n FROM kg_triples")["n"]


def rule(subject, predicate):
    row = db.q1("SELECT object FROM kg_triples WHERE subject=? AND predicate=?", (subject, predicate))
    if row:
        return float(row["object"]) if "." in row["object"] else int(row["object"])
    return DEFAULT_RULES.get((subject, predicate))


def neighbours(entity):
    return db.q("SELECT * FROM kg_triples WHERE lower(subject)=lower(?) OR lower(object)=lower(?)", (entity, entity))


def entities_in(text):
    """Find graph entities mentioned in free text (ids, names, leave types, policy topics)."""
    t = text.lower()
    found = []
    for row in db.q("SELECT DISTINCT subject FROM kg_triples"):
        s = row["subject"]
        first = s.lower().split()[0]
        if s.lower() in t or (len(first) > 3 and re.search(rf"\b{re.escape(first)}\b", t) and " " in s):
            found.append(s)
    for word in ("annual", "sick", "casual", "maternity", "paternity"):
        if word in t and f"{word} leave" not in found:
            found.append(f"{word} leave")
    return found


def facts_for(text, employee_id=None, limit=40):
    """Triples relevant to a question, including 2-hop facts for people (their manager, the approver)."""
    ents = entities_in(text)
    if employee_id:
        row = db.q1("SELECT name FROM employees WHERE id=?", (employee_id,))
        if row and re.search(r"\b(i|me|my|mine)\b", text.lower()):
            ents.append(row["name"])
    seen, out = set(), []
    for e in ents:
        for tr in neighbours(e):
            key = (tr["subject"], tr["predicate"], tr["object"])
            if key not in seen:
                seen.add(key)
                out.append(tr)
    return out[:limit]


def as_text(triples):
    return "\n".join(f"- {t['subject']} --{t['predicate']}--> {t['object']}  [{t['source']}]" for t in triples)


def extract_with_llm():
    """Optional: ask the fast model for extra (subject, predicate, object) facts from the handbook."""
    from ..gateway import llm
    prompt = ("Extract HR policy facts from this handbook as JSON lines, each {\"subject\",\"predicate\",\"object\","
              "\"section\"}. Numbers only for numeric objects. No prose.\n\n" + vectors.handbook_text())
    res = llm.complete("memory", [{"role": "user", "content": prompt}], tier="fast")
    added = 0
    for line in res.text.splitlines():
        try:
            f = json.loads(line)
            add(f["subject"].lower(), f["predicate"], f["object"], f"handbook section {f.get('section', '?')} (llm)")
            added += 1
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return added
