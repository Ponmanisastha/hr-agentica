"""The sample pack in samples/: policy documents, resumes and HR data for a fictional company, to try every
feature with realistic files before you use your own.

`python app.py samples load` copies the policies into the policy folder and the resumes into the hiring inbox,
adds the employees, openings, holidays, past leave and salaries the app does not have yet, and re-indexes.
It never overwrites a record that already exists, so it is safe to run on a database you have been using.
"""

import json
import shutil

from . import config, db

ROOT = config.ROOT / "samples"


def _read(name):
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


def _copy_new(src_dir, dest_dir):
    """Copy the files under src_dir that dest_dir does not have yet; returns their relative paths."""
    added = []
    for src in sorted(p for p in src_dir.rglob("*") if p.is_file()):
        dest = dest_dir / src.relative_to(src_dir)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            added.append(str(src.relative_to(src_dir)))
    return added


def load(policies=True, resumes=True, data=True):
    """Load the sample pack. Needs a current user (payroll records who set each salary)."""
    from . import automations, hiring, payroll
    from .knowledge import kag, policies as pol, vectors
    out = {}
    if data:
        new_emps = []
        for e in _read("employees.json"):
            if db.q1("SELECT 1 AS y FROM employees WHERE id=?", (e["id"],)):
                continue
            b = e["leave_balance"]
            db.x("INSERT INTO employees (id, name, email, department, level, manager, manager_email, annual, sick, casual, "
                 "skills, date_of_birth, joined_on) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (e["id"], e["name"], e["email"], e["department"], e["level"], e["manager"], e["manager_email"],
                  b["annual"], b["sick"], b["casual"], json.dumps(e["skills"]), e["date_of_birth"], e["joined_on"]))
            new_emps.append(e["id"])
        # past leave for the employees just added (their balances above already have it taken off)
        leave = [r for r in _read("leave_history.json") if r["employee_id"] in new_emps]
        for r in leave:
            db.x("INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, working_days, decision, "
                 "reasons, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (r["employee_id"], r["leave_type"], r["start_date"], r["end_date"], r["working_days"], "approve",
                  json.dumps(["Sample history"]), r["status"], f"{r['start_date']}T09:00:00"))
        jobs = []
        for j in _read("job_openings.json"):
            if not db.q1("SELECT 1 AS y FROM jobs WHERE id=?", (j["id"],)):
                db.x("INSERT INTO jobs (id, title, location, min_years, must_have, nice_to_have, shortlist_size, rounds) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (j["id"], j["title"], j["location"], j["min_years"], json.dumps(j["must_have"]),
                      json.dumps(j["nice_to_have"]), j["shortlist_size"],
                      json.dumps(j.get("rounds", ["L1", "L2", "HR", "Final"]))))
                jobs.append(j["id"])
        for d in _read("holidays.json"):
            db.x("INSERT OR IGNORE INTO holidays VALUES (?)", (d,))
        salaries = []
        for s in _read("salaries.json"):
            if db.q1("SELECT 1 AS y FROM employees WHERE id=?", (s["employee_id"],)) and \
                    not db.q1("SELECT 1 AS y FROM salary_structures WHERE employee_id=?", (s["employee_id"],)):
                payroll.set_structure(s["employee_id"], s["ctc_annual"], s["effective_from"], s["metro"], s["regime"],
                                      s["pt_state"], s["pan"], s["uan"], s["bank_account"], s["ifsc"], s["declarations"])
                salaries.append(s["employee_id"])
        out.update(employees_added=new_emps, leave_records_added=len(leave), jobs_added=jobs, salaries_added=salaries)
    if resumes:
        out["resumes_copied"] = _copy_new(ROOT / "resumes", hiring.inbox_root())
    if policies:
        out["policies_copied"] = _copy_new(ROOT / "policies", pol.policy_dir())
    out["index"] = automations.reindex_policies()
    vectors.index_all()
    out["index"]["kg_triples"] = kag.build()  # the new employees' reporting lines and the new openings
    return out
