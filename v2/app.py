"""HR agentic AI v2 - command line.

  python app.py init [--demo-users]        create the database, admin login, vectors, knowledge graph, git repo
  python app.py demo                       run sample requests through every agent, plus a ticket round trip
  python app.py ask "<request>" [--as USER] one request (as the local admin, or as USER after a password prompt)
  python app.py serve [--port 8000]        web console + HTTP API + A2A endpoints
  python app.py mcp [--http --port 8765]   MCP server (stdio by default); needs HRAI_MCP_TOKEN
  python app.py user add NAME ROLE [--employee E101] | user list
  python app.py token create USER [--label L --days 90]
  python app.py triggers list | run | fire NAME
  python app.py tickets list | show ID | work [ID] | approve ID | reject ID [--note TEXT] | sync
  python app.py budget [show] | budget set AGENT USD [--on-exceed downgrade|block]
  python app.py hiring ingest [--job JOB-101] | board | followups | rounds JOB-101 L1 L2 L3 HR Final | sample
  python app.py payroll run|show|submit|paid REF|slip E101 [--month 2026-10]     salary and payroll
  python app.py insights [--job JOB-101] | attention | report | csv     HR analytics; csv prints the pipeline
  python app.py index                      rebuild vectors and the knowledge graph
  python app.py a2a send URL "<text>" --token T   call any A2A agent
"""

import argparse
import getpass
import json
import logging
import sys

from hrai import auth, config, db
from hrai.ops import tracker

# Silence chatty third-party loggers on the console; our ERROR logs still become tickets.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "LiteLLM", "litellm", "chromadb"):
    logging.getLogger(noisy).setLevel(logging.ERROR)


def setup():
    db.init_db()
    tracker.install()
    from hrai import automations  # noqa: F401  registers built-in triggers
    from hrai.knowledge import vectors
    if not db.q1("SELECT 1 AS y FROM kg_triples LIMIT 1"):
        from hrai.knowledge import kag
        vectors.index_all()
        kag.build()


def cli_user():
    return auth.User(0, f"cli:{getpass.getuser()}", "admin")


def print_result(req, res):
    print("=" * 78)
    print(f"REQUEST: {req}")
    print(f"AGENT: {res['agent']}   MODE: {res['mode']}   TOOL CALLS: {len(res['trace'])}")
    for s in res["trace"]:
        args = ", ".join(f"{k}={str(v)[:40]!s}" for k, v in s["input"].items() if k not in ("body", "decisions"))
        print(f"  - [{s['agent']}] {s['tool']}({args[:100]})")
    print("RESULT:\n  " + res["result"].replace("\n", "\n  "))


def demo():
    from datetime import timedelta
    from hrai.gateway import agent_gateway as G
    from hrai.gateway import llm
    from hrai.ops import ticket_agent
    nxt = lambda d: (config.today() + timedelta(days=d)).isoformat()
    user = cli_user()
    requests = [
        "Screen all resumes for the Backend Engineer opening and shortlist the best candidates.",
        "Onboard our new hires Priya Raman and Vikram Singh.",
        f"Employee E101 wants annual leave from {nxt(14)} to {nxt(16)}.",
        f"E102 is asking for annual leave from {nxt(3)} to {nxt(9)}.",
        "How many days of maternity leave do we offer?",
        "Who approves leave for Sanjay Patel?",
        "Is there a policy on pet insurance?",
    ]
    for r in requests:
        print_result(r, G.handle(r, user=user, channel="cli"))
    print("=" * 78)
    print("A2A: the leave agent asks the policy agent directly")
    from hrai import a2a
    token = auth.set_current_user(user)
    print("  ", json.dumps(a2a.delegate("policy", "How much notice does casual leave need?"))[:300])
    auth._current.reset(token)
    print("=" * 78)
    print("TICKETS: the knowledge-gap question above opened a ticket; the ticket agent works it now")
    for t in tracker.list_tickets("new"):
        print(f"  ticket #{t['id']} ({t['kind']}): {t['title']}")
        print("   ->", ticket_agent.work(t["id"]))
    for t in tracker.list_tickets("awaiting_approval"):
        print(f"  #{t['id']} waits for a human: python app.py tickets show {t['id']}  then  tickets approve {t['id']}")
    print("=" * 78)
    print("HIRING PIPELINE: sample resumes dropped into the inbox, then moved through the rounds")
    import shutil
    from hrai import hiring
    shutil.copytree(config.DATA / "sample_inbox", hiring.inbox_root(), dirs_exist_ok=True)
    for r in ["Read the new resumes in the inbox",
              f"Schedule Divya Krishnan for L1 on {nxt(2)} at 11:00",
              f"Divya cleared L1 with rating 4, schedule the next round on {nxt(4)} at 15:00",
              "What is the hiring pipeline status?"]:
        print_result(r, G.handle(r, user=user, channel="cli"))
    print("=" * 78)
    print("PAYROLL: salary breakup, a draft run, and the approval it waits on")
    for r in ["What is the breakup for a 12 lakh CTC?", f"Run payroll for {config.today().strftime('%Y-%m')}",
              "Submit the payroll for approval"]:
        print_result(r, G.handle(r, user=user, channel="cli"))
    print("=" * 78)
    print("INSIGHTS: numbers and what needs attention (the web console's Dashboard shows the same)")
    for r in ["How is hiring going?", "What needs my attention today?"]:
        print_result(r, G.handle(r, user=user, channel="cli"))
    print("=" * 78)
    print("BUDGET:", json.dumps(llm.budget_report()["total"]))
    print("Drafted emails:", db.q1("SELECT COUNT(*) AS n FROM outbox")["n"], " Pending approvals:",
          db.q1("SELECT COUNT(*) AS n FROM approvals WHERE status='pending'")["n"])


def main(argv):
    p = argparse.ArgumentParser(description="HR agentic AI v2", usage=__doc__)
    p.add_argument("cmd")
    p.add_argument("args", nargs="*")
    p.add_argument("--as", dest="as_user")
    p.add_argument("--port", type=int)
    p.add_argument("--http", action="store_true")
    p.add_argument("--employee")
    p.add_argument("--label", default="cli")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--note", default="")
    p.add_argument("--on-exceed", default="downgrade")
    p.add_argument("--token")
    p.add_argument("--demo-users", action="store_true")
    p.add_argument("--job", default="")
    p.add_argument("--month", default="")
    a = p.parse_args(argv)
    cmd, args = a.cmd, a.args

    if cmd == "mcp":
        from hrai import mcp_server
        return mcp_server.main(http=a.http, port=a.port or 8765)
    setup()
    if cmd == "init":
        from hrai.knowledge import kag, vectors
        from hrai.ops import ticket_agent
        pw = auth.bootstrap_admin(config.env("HRAI_ADMIN_PASSWORD") or None)
        if pw:
            print(f"Admin login created. Username: admin  Password: {pw}  (shown once; change it with `user passwd`)")
        if a.demo_users:
            import secrets
            for name, role, emp in (("hr_demo", "hr", None), ("manager_demo", "manager", None), ("deepa", "employee", "E101")):
                if not auth.get_user(name):
                    pw = secrets.token_urlsafe(9)
                    auth.create_user(name, pw, role, emp)
                    print(f"Demo user {name} ({role}{', ' + emp if emp else ''}): password {pw}")
        print("Vectors:", vectors.index_all(), " Knowledge graph triples:", kag.build())
        print("Git repository created." if ticket_agent.ensure_repo() else "Git repository: already present.")
    elif cmd == "demo":
        demo()
    elif cmd == "ask":
        from hrai.gateway import agent_gateway as G
        user = cli_user()
        if a.as_user:
            token = auth.login(a.as_user, getpass.getpass(f"Password for {a.as_user}: "))
            user = auth.user_for_token(token)
        req = " ".join(args)
        try:
            print_result(req, G.handle(req, user=user, channel="cli"))
        except G.GatewayError as exc:
            print(f"Error: {exc}")
    elif cmd == "serve":
        from hrai import web
        web.serve(a.port or 8000)
    elif cmd == "user":
        if args[:1] == ["add"]:
            pw = getpass.getpass("New password (10+ characters): ")
            print(auth.create_user(args[1], pw, args[2], a.employee))
        elif args[:1] == ["passwd"]:
            pw = getpass.getpass("New password (10+ characters): ")
            db.x("UPDATE users SET password_hash=? WHERE username=?", (auth.hash_password(pw), args[1].lower()))
            print("Password changed.")
        else:
            for u in db.q("SELECT username, role, employee_id, created_at FROM users"):
                print(u)
    elif cmd == "token":
        print(auth.create_service_token(args[1], a.label, a.days))
    elif cmd == "triggers":
        from hrai import triggers
        if args[:1] == ["run"]:
            triggers.run_forever()
        elif args[:1] == ["fire"]:
            print(json.dumps(triggers.fire(args[1]), indent=2, default=str))
        else:
            for n, s in triggers.SCHEDULES.items():
                print(f"{n:28} {'every ' + str(s['every']) + 's' if s['every'] else 'daily at ' + s['daily']:18} {s['doc']}")
            print("Event triggers:", ", ".join(sorted(triggers._subscribers)))
    elif cmd == "tickets":
        from hrai.ops import ticket_agent
        sub = args[0] if args else "list"
        token = auth.set_current_user(cli_user())
        try:
            if sub == "list":
                for t in tracker.list_tickets():
                    print(f"#{t['id']:<4} {t['status']:18} {t['kind'] or '':14} x{t['occurrences']:<3} {t['title'][:70]}")
            elif sub == "show":
                t = tracker.get(int(args[1]))
                for k in ("id", "title", "status", "kind", "severity", "component", "branch", "pr_url", "resolution"):
                    print(f"{k:11}: {t[k]}")
                print("\nDETAIL:\n" + t["detail"][:3000])
                if t["review"]:
                    print("\nAI REVIEW:\n" + t["review"])
                if t["patch"]:
                    print("\nPATCH:\n" + t["patch"])
                print("\nHISTORY:")
                for e in t["events"]:
                    print(f"  {e['ts']} {e['actor']}: {e['event']} {e['detail'] or ''}"[:200])
            elif sub == "work":
                print(ticket_agent.work(int(args[1])) if len(args) > 1 else ticket_agent.work_new_tickets())
            elif sub in ("approve", "reject"):
                t = ticket_agent.decide(int(args[1]), sub == "approve", a.note)
                print(f"#{t['id']} {t['status']}: {t['resolution']}")
            elif sub == "sync":
                print(ticket_agent.sync_github())
        finally:
            auth._current.reset(token)
    elif cmd == "budget":
        from hrai.gateway import llm
        if args[:1] == ["set"]:
            llm.set_budget(args[1], float(args[2]), a.on_exceed)
        print(json.dumps(llm.budget_report(), indent=2))
    elif cmd == "hiring":
        from hrai import hiring
        from hrai import tools as T
        sub = args[0] if args else "board"
        token = auth.set_current_user(cli_user())
        try:
            if sub == "sample":  # copy the sample resumes into the inbox
                import shutil
                src = config.DATA / "sample_inbox"
                shutil.copytree(src, hiring.inbox_root(), dirs_exist_ok=True)
                print(f"Copied sample resumes into {hiring.inbox_root()}; now run: python app.py hiring ingest")
            elif sub == "ingest":
                print(json.dumps(T.run("ingest_resumes", job_id=a.job), indent=2, default=str))
            elif sub == "followups":
                for f in hiring.followups(14):
                    print(f"#{f['id']:<4} {f['due']}  {f['name']:20} {f['note']}")
            elif sub == "rounds":
                print(hiring.set_rounds(args[1], args[2:]))
            else:
                print(hiring.summary(a.job or None))
        finally:
            auth._current.reset(token)
    elif cmd == "payroll":
        from hrai import payroll
        from hrai import tools as T
        sub = args[0] if args else "show"
        month = a.month or ""
        token = auth.set_current_user(cli_user())
        try:
            if sub == "run":
                print(json.dumps(T.run("run_payroll", month=month), indent=2, default=str))
            elif sub == "submit":
                print(json.dumps(T.run("submit_payroll", month=month), indent=2))
            elif sub == "paid":
                print(json.dumps(T.run("mark_payroll_paid", month=month, reference=args[1]), indent=2))
            elif sub == "slip":
                print(payroll.render_payslip(args[1], month) or "No payslip for that employee and month")
            else:
                s = payroll.summary(month)
                print(json.dumps({k: v for k, v in s.items() if k != "payslips"}, indent=2, default=str))
                for p in s.get("payslips", []):
                    print(f"  {p['employee_id']:6} {p['name']:22} net {p['net']:>12,}")
        finally:
            auth._current.reset(token)
    elif cmd == "insights":
        from hrai import insights
        sub = args[0] if args else ""
        token = auth.set_current_user(cli_user())
        try:
            if sub == "attention":
                for i, item in enumerate(insights.attention(20), 1):
                    print(f"{i:>2}. [{item['kind']}] {item['text']}  ({item['tab']})")
            elif sub == "report":
                print("Saved", insights.save_report())
            elif sub == "csv":
                sys.stdout.write(insights.candidates_csv(a.job or None))
            else:
                print(insights.narrate(insights.overview(a.job or None)))
        finally:
            auth._current.reset(token)
    elif cmd == "index":
        from hrai.knowledge import kag, vectors
        print(vectors.index_all(), kag.build())
    elif cmd == "a2a":
        from hrai import a2a
        client = a2a.A2AClient(a.token or config.env("HRAI_A2A_TOKEN"))
        if args[:1] == ["card"]:
            print(json.dumps(client.card(args[1]), indent=2))
        else:
            print(a2a.answer_of(client.send(args[1], " ".join(args[2:])))[1])
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
