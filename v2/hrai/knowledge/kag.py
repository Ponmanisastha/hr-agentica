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

# (subject, predicate, regex on a policy section, word the section title must contain). Matching on the title
# rather than a section number means your own documents work whatever order or numbering they use.
RULE_PATTERNS = [
    ("annual leave", "days_per_year", r"earns (\d+(?:\.\d+)?) days of annual leave", "annual"),
    ("annual leave", "notice_days", r"at least (\d+) calendar days in advance", "annual"),
    ("annual leave", "auto_approve_max_days", r"up to (\d+) working days", "annual"),
    ("annual leave", "carry_forward_days", r"[Uu]p to (\d+) unused days", "annual"),
    ("annual leave", "credit_per_month", r"credited at (\d+(?:\.\d+)?) days per month", "annual"),
    ("sick leave", "days_per_year", r"get (\d+) days of paid sick leave", "sick"),
    ("sick leave", "certificate_after_days", r"more than (\d+) consecutive working days", "sick"),
    ("casual leave", "days_per_year", r"get (\d+) days of casual leave", "casual"),
    ("casual leave", "max_consecutive_days", r"at most (\d+) consecutive days", "casual"),
    ("casual leave", "notice_days", r"at least (\d+) days? in advance", "casual"),
    ("maternity leave", "weeks", r"Maternity leave is (\d+) weeks", "maternity"),
    ("paternity leave", "working_days", r"Paternity leave is (\d+) working days", "paternity"),
    ("notice period", "days_L3_and_above", r"notice period is (\d+) days for employees at level L3", "notice"),
    ("notice period", "days_other_levels", r"and (\d+) days for others", "notice"),
    ("work from home", "max_days_per_week", r"up to (\d+) days a week", "home"),
    ("internet reimbursement", "cap_rupees_per_month", r"capped at ([\d,]+) rupees", "reimburs"),
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
    from . import policies, versions
    secs = policies.sections()
    order = {name: i for i, name in enumerate(dict.fromkeys(s["meta"]["source"] for s in secs))}
    live = versions.activation_times()
    found = {}
    for subj, pred, value, doc, title in extract(secs):
        # the same rule in two documents: the most recently approved document wins, then the first by name
        rank = (live.get(doc, ""), -order[doc])
        if (subj, pred) not in found or rank > found[(subj, pred)][0]:
            found[(subj, pred)] = (rank, value, doc, title)
    for (subj, pred), (_, value, doc, title) in found.items():
        add(subj, pred, value, f"{versions.cite(doc)}, section {title}")
    return db.q1("SELECT COUNT(*) AS n FROM kg_triples")["n"]


def extract(sections):
    """Every rule phrase in these sections, in document order: [(subject, predicate, value, document, section)]."""
    out = []
    for sec in sections:
        title = sec["meta"]["section"]
        text = re.sub(r"\s+", " ", sec["text"])  # PDF and Word text breaks lines mid-sentence
        for subj, pred, pattern, word in RULE_PATTERNS:
            if word in title.lower():
                m = re.search(pattern, text)
                if m:
                    out.append((subj, pred, m.group(1).replace(",", ""), sec["meta"]["source"], title))
    return out


def rule(subject, predicate):
    row = db.q1("SELECT object FROM kg_triples WHERE subject=? AND predicate=?", (subject, predicate))
    if row:
        return float(row["object"]) if "." in row["object"] else int(row["object"])
    return DEFAULT_RULES.get((subject, predicate))


def source(subject, predicate):
    """Where a rule came from, for citations ("Policy handbook, section 1. Annual leave")."""
    row = db.q1("SELECT source FROM kg_triples WHERE subject=? AND predicate=?", (subject, predicate))
    return row["source"] if row else "the default rules (not found in your policy documents)"


LABELS = {"days_per_year": "days a year", "notice_days": "days' notice", "auto_approve_max_days": "auto-approved up to (days)",
          "carry_forward_days": "carry forward (days)", "credit_per_month": "credited a month (days)",
          "certificate_after_days": "certificate after (days)", "max_consecutive_days": "most consecutive days",
          "weeks": "weeks", "working_days": "working days", "days_L3_and_above": "days, L3 and above",
          "days_other_levels": "days, other levels", "max_days_per_week": "days a week",
          "cap_rupees_per_month": "rupees a month"}


def rules():
    """Every rule the leave engine and the agents use, its value, and where it was read from."""
    out = []
    for subj, pred, _, _ in RULE_PATTERNS:
        row = db.q1("SELECT object, source FROM kg_triples WHERE subject=? AND predicate=?", (subj, pred))
        value = row["object"] if row else DEFAULT_RULES.get((subj, pred))
        out.append({"rule": f"{subj.capitalize()}: {LABELS.get(pred, pred)}", "subject": subj, "predicate": pred,
                    "value": value, "source": row["source"] if row else None})
    return out


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
