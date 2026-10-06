"""SQLite database: HR records, users, approvals, LLM usage, memories, knowledge graph and tickets.

One file (var/hrai.db), standard library only. Each thread gets its own connection.
"""

import json
import re
import sqlite3
import threading
from datetime import datetime

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','hr','manager','employee','service')),
    employee_id TEXT, failed_attempts INTEGER DEFAULT 0, locked_until TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT DEFAULT 'login',
    label TEXT, expires_at TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS employees (
    id TEXT PRIMARY KEY, name TEXT, email TEXT, department TEXT, level TEXT,
    manager TEXT, manager_email TEXT, annual REAL, sick REAL, casual REAL);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, title TEXT, location TEXT, min_years INTEGER,
    must_have TEXT, nice_to_have TEXT, shortlist_size INTEGER);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY, file_name TEXT, name TEXT, email TEXT, resume_text TEXT,
    job_id TEXT, score INTEGER, decision TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS new_hires (
    id TEXT PRIMARY KEY, name TEXT, email TEXT, role TEXT, department TEXT,
    manager TEXT, start_date TEXT, documents TEXT);
CREATE TABLE IF NOT EXISTS holidays (day TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS leave_requests (
    id INTEGER PRIMARY KEY, employee_id TEXT, leave_type TEXT, start_date TEXT, end_date TEXT,
    working_days REAL, decision TEXT, reasons TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS onboarding_tasks (
    id INTEGER PRIMARY KEY, hire_id TEXT, due TEXT, owner TEXT, task TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, to_addr TEXT, subject TEXT, body TEXT, status TEXT DEFAULT 'draft',
    agent TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY, kind TEXT, ref TEXT, summary TEXT, payload TEXT, status TEXT DEFAULT 'pending',
    requested_by TEXT, decided_by TEXT, decided_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY, ts TEXT, agent TEXT, username TEXT, model TEXT, tier TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER, cached_tokens INTEGER, cost_usd REAL,
    latency_ms INTEGER, ok INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS budgets (agent TEXT PRIMARY KEY, monthly_usd REAL, on_exceed TEXT DEFAULT 'downgrade');
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, ts TEXT, username TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, username TEXT, kind TEXT, text TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS kg_triples (
    subject TEXT, predicate TEXT, object TEXT, source TEXT, PRIMARY KEY (subject, predicate, object));
CREATE TABLE IF NOT EXISTS answer_cache (key TEXT PRIMARY KEY, answer TEXT, kb_hash TEXT, hits INTEGER DEFAULT 0, ts TEXT);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY, source TEXT, title TEXT, detail TEXT, fingerprint TEXT, kind TEXT,
    severity TEXT, component TEXT, status TEXT, occurrences INTEGER DEFAULT 1, branch TEXT, patch TEXT,
    test_output TEXT, review TEXT, pr_url TEXT, resolution TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS ticket_events (id INTEGER PRIMARY KEY, ticket_id INTEGER, ts TEXT, actor TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY, ts TEXT, username TEXT, request TEXT, answer TEXT, rating INTEGER, comment TEXT, ticket_id INTEGER);
CREATE TABLE IF NOT EXISTS trigger_runs (id INTEGER PRIMARY KEY, name TEXT, ts TEXT, status TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS interviews (
    id INTEGER PRIMARY KEY, candidate_id TEXT, job_id TEXT, round TEXT, scheduled_at TEXT, interviewer TEXT,
    mode TEXT, status TEXT DEFAULT 'scheduled', result TEXT, rating INTEGER, feedback TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS candidate_events (
    id INTEGER PRIMARY KEY, candidate_id TEXT, ts TEXT, actor TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY, candidate_id TEXT, job_id TEXT, ctc_lpa REAL, joining_date TEXT, status TEXT,
    approval_id INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT, client TEXT, manager TEXT, start_date TEXT, end_date TEXT,
    status TEXT DEFAULT 'active', health TEXT DEFAULT 'on_track', skills TEXT, notes TEXT, created_by TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY, employee_id TEXT, project_id TEXT, percent REAL, role TEXT, start_date TEXT,
    end_date TEXT, status TEXT DEFAULT 'active', created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS project_tasks (
    id INTEGER PRIMARY KEY, project_id TEXT, title TEXT, owner_id TEXT, due TEXT, status TEXT DEFAULT 'todo',
    kind TEXT DEFAULT 'task', estimate_hours REAL, note TEXT, created_by TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS timesheets (
    id INTEGER PRIMARY KEY, employee_id TEXT, project_id TEXT, day TEXT, hours REAL, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS project_events (
    id INTEGER PRIMARY KEY, project_id TEXT, ts TEXT, actor TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS salary_structures (
    id INTEGER PRIMARY KEY, employee_id TEXT, ctc_annual REAL, effective_from TEXT, metro INTEGER DEFAULT 0,
    regime TEXT DEFAULT 'new', pt_state TEXT, pan TEXT, uan TEXT, bank_account TEXT, ifsc TEXT, declarations TEXT,
    breakup TEXT, status TEXT DEFAULT 'active', created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS salary_revisions (
    id INTEGER PRIMARY KEY, employee_id TEXT, old_ctc REAL, new_ctc REAL, pct REAL, effective_from TEXT, reason TEXT,
    status TEXT DEFAULT 'pending_approval', approval_id INTEGER, requested_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS pay_adjustments (
    id INTEGER PRIMARY KEY, employee_id TEXT, month TEXT, kind TEXT, amount REAL, note TEXT, created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS payroll_runs (
    id INTEGER PRIMARY KEY, month TEXT UNIQUE, status TEXT DEFAULT 'draft', approval_id INTEGER, submitted_by TEXT,
    approved_by TEXT, paid_reference TEXT, paid_at TEXT, warnings TEXT, created_by TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS payslips (
    id INTEGER PRIMARY KEY, run_id INTEGER, employee_id TEXT, month TEXT, gross REAL, total_deductions REAL, net REAL,
    earnings TEXT, deductions TEXT, employer TEXT, paid_days REAL, lop_days REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY, candidate_id TEXT, due TEXT, kind TEXT, note TEXT, status TEXT DEFAULT 'open',
    created_at TEXT, done_at TEXT);
"""

# Columns added after the first release; init_db adds any that an older database is missing.
MIGRATIONS = {
    "candidates": {"phone": "TEXT", "skills": "TEXT", "years": "INTEGER", "file_hash": "TEXT", "source_path": "TEXT",
                   "stage": "TEXT DEFAULT 'applied'", "status_note": "TEXT", "created_at": "TEXT"},
    "jobs": {"rounds": "TEXT", "select_threshold": "INTEGER DEFAULT 70", "status": "TEXT DEFAULT 'open'"},
    "new_hires": {"candidate_id": "TEXT", "employee_id": "TEXT"},
    "employees": {"skills": "TEXT"},
}


def _migrate():
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in q(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in have:
                conn().execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn().commit()


def now():
    return datetime.now().isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    path = str(config.home() / "hrai.db")
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != path:
        c = sqlite3.connect(path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn, _local.path = c, path
    return c


def q(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def x(sql, args=()):
    c = conn()
    cur = c.execute(sql, args)
    c.commit()
    return cur.lastrowid


def audit(username, action, detail=None):
    x("INSERT INTO audit_log (ts, username, action, detail) VALUES (?,?,?,?)",
      (now(), username, action, json.dumps(detail or {}, default=str)[:4000]))


def init_db(seed=True):
    conn().executescript(SCHEMA)
    _migrate()
    for agent, amount in config.DEFAULT_BUDGETS.items():
        amount = float(config.env(f"HRAI_BUDGET_{agent.upper()}", amount))
        x("INSERT OR IGNORE INTO budgets (agent, monthly_usd) VALUES (?,?)", (agent, amount))
    if seed and not q1("SELECT 1 AS y FROM employees LIMIT 1"):
        seed_sample_data()


def seed_sample_data():
    """Load the sample HR data from data/ (a stand-in for an HRMS/ATS export)."""
    D = config.DATA
    for e in json.loads((D / "employees.json").read_text(encoding="utf-8")):
        b = e["leave_balance"]
        x("INSERT OR REPLACE INTO employees (id, name, email, department, level, manager, manager_email, annual, sick, "
          "casual, skills) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
          (e["id"], e["name"], e["email"], e.get("department"), e.get("level"), e["manager"],
           e["manager_email"], b["annual"], b["sick"], b["casual"], json.dumps(e.get("skills", []))))
    for j in json.loads((D / "job_openings.json").read_text(encoding="utf-8")):
        x("INSERT OR REPLACE INTO jobs (id, title, location, min_years, must_have, nice_to_have, shortlist_size) "
          "VALUES (?,?,?,?,?,?,?)",
          (j["id"], j["title"], j["location"], j["min_years"], json.dumps(j["must_have"]),
           json.dumps(j["nice_to_have"]), j["shortlist_size"]))
        x("UPDATE jobs SET rounds=? WHERE id=?", (json.dumps(j.get("rounds", ["L1", "L2", "HR", "Final"])), j["id"]))
    for h in json.loads((D / "new_hires.json").read_text(encoding="utf-8")):
        x("INSERT OR REPLACE INTO new_hires (id, name, email, role, department, manager, start_date, documents) "
          "VALUES (?,?,?,?,?,?,?,?)",
          (h["id"], h["name"], h["email"], h["role"], h["department"], h["manager"], h["start_date"],
           json.dumps(h["documents_submitted"])))
    for d in json.loads((D / "holidays.json").read_text(encoding="utf-8")):
        x("INSERT OR IGNORE INTO holidays VALUES (?)", (d,))
    job = q1("SELECT id FROM jobs LIMIT 1")
    for i, path in enumerate(sorted((D / "resumes").glob("*.txt")), 1):
        text = path.read_text(encoding="utf-8")
        name = re.search(r"^name:\s*(.+)$", text, re.M | re.I)
        email = re.search(r"^email:\s*(.+)$", text, re.M | re.I)
        x("INSERT OR REPLACE INTO candidates (id, file_name, name, email, resume_text, job_id, updated_at, stage, "
          "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
          (f"C-{i:03d}", path.name, name.group(1).strip() if name else path.stem,
           email.group(1).strip() if email else None, text, job["id"] if job else None, now(), "applied", now()))
    from . import payroll, projects
    payroll.seed_sample_salaries()
    projects.seed_sample_projects()
