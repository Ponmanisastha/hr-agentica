"""Projects and staffing: allocations and the 100% rule, capacity with leave, tasks, timesheets, risks and the API."""

import json
import threading
import unittest
import urllib.request

from tests.common import ADMIN, as_user, bootstrap

from hrai import auth, config, db, projects, triggers
from hrai import tools as T
from hrai.gateway import agent_gateway as G

A, B = "E911", "E912"


def setUpModule():
    bootstrap()
    for eid, name, skills in ((A, "Ravi Project", ["python", "sql"]), (B, "Nisha Project", ["react", "python"])):
        db.x("INSERT OR REPLACE INTO employees (id, name, email, department, level, manager, manager_email, annual, "
             "sick, casual, skills) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
             (eid, name, f"{eid.lower()}@example.com", "Engineering", "L3", "Arun Kumar", "arun.kumar@example.com",
              12, 6, 4, json.dumps(skills)))
    auth.create_user("mgr1", "manager-password-1", "manager")
    auth.create_user("ravi", "ravi-password-1", "employee", A)


class AllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with as_user(ADMIN):
            cls.p = projects.create("Test platform", "Acme", "Arun Kumar", "2026-10-01", "2026-12-31",
                                    ["python", "sql", "react"])

    def test_allocation_cannot_pass_100(self):
        with as_user(ADMIN):
            first = projects.allocate(A, self.p["id"], 60, "Engineer")
            self.assertEqual(first["now_allocated_pct"], 60)
            with self.assertRaises(projects.ProjectError) as cm:
                projects.allocate(A, self.p["id"], 50)
            self.assertIn("only 40% is free", str(cm.exception))
            projects.allocate(A, self.p["id"], 40)
            self.assertEqual(projects.allocated_percent(A), 100)

    def test_release_frees_capacity(self):
        with as_user(ADMIN):
            other = projects.create("Short job", "Internal", "", "2026-10-01", "2026-10-31")
            a = projects.allocate(B, other["id"], 100, "Lead")
            self.assertEqual(projects.allocated_percent(B), 100)
            projects.release(a["allocation_id"], "2026-10-05")
            self.assertEqual(projects.allocated_percent(B, config.today()), 0)  # ended before today
            self.assertEqual(len(projects.assignments(B)["history"]), 1)

    def test_capacity_counts_leave_and_bench(self):
        with as_user(ADMIN):
            db.x("INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, working_days, status, "
                 "created_at) VALUES (?,?,?,?,?,?,?)", (B, "annual", "2026-10-12", "2026-10-16", 5, "approved", db.now()))
            rows = {c["id"]: c for c in projects.capacity(4)}
        self.assertEqual(rows[A]["allocated_pct"], 100)
        self.assertFalse(rows[A]["over_allocated"])
        self.assertEqual(rows[B]["leave_days"], 5)
        self.assertIn(B, [c["id"] for c in projects.bench(4)])
        self.assertNotIn(A, [c["id"] for c in projects.bench(4)])

    def test_staffing_gap_suggests_a_free_person(self):
        with as_user(ADMIN):
            gap = projects.staffing_gap(self.p["id"])
        self.assertEqual(gap["gaps"], ["react"])          # nobody on the project has react
        self.assertIn(B, [s["employee_id"] for s in gap["suggestions"]])


class TaskAndTimesheetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with as_user(ADMIN):
            cls.p = projects.create("Task tests", "Internal", "", "2026-10-01", "2026-11-30")

    def test_tasks_and_overdue(self):
        with as_user(ADMIN):
            late = projects.add_task(self.p["id"], "Overdue thing", A, "2026-10-01")
            projects.add_task(self.p["id"], "Future thing", A, "2026-10-30", kind="milestone")
            rows = projects.tasks(self.p["id"])
            self.assertEqual([r["overdue"] for r in rows], [True, False])
            with self.assertRaises(projects.ProjectError):
                projects.set_task(late["task_id"], status="finished")
            projects.set_task(late["task_id"], status="done")
            self.assertEqual(len(projects.tasks(self.p["id"])), 1)

    def test_hours_are_checked(self):
        with as_user(ADMIN):
            out = projects.log_hours(A, self.p["id"], "2026-10-05", 6, "build")
            self.assertEqual(out["day_total"], 6)
            with self.assertRaises(projects.ProjectError):
                projects.log_hours(A, self.p["id"], "2026-10-05", 11)   # over 16 hours in a day
            with self.assertRaises(projects.ProjectError):
                projects.log_hours(A, self.p["id"], "2026-12-01", 4)    # in the future
            sheet = projects.timesheet(employee=A, days=7)
            self.assertEqual(sheet["total_hours"], 6)

    def test_risks(self):
        with as_user(ADMIN):
            projects.create("Unstaffed work", "Acme", "", "2026-10-01", "2026-12-31")
            kinds = {r["kind"] for r in projects.risks()}
        self.assertIn("no_team", kinds)
        self.assertIn("overdue_tasks", kinds)


class ProjectAgentTests(unittest.TestCase):
    def test_routing_and_rules_answers(self):
        out = G.handle("Who is free next month?", user=ADMIN)
        self.assertEqual(out["agent"], "projects")
        self.assertIn("utilisation", out["result"].lower())
        self.assertEqual(G.handle("What is slipping?", user=ADMIN)["agent"], "projects")
        out = G.handle("Put Nisha on the Test platform at 20%", user=ADMIN)
        self.assertIn("Nisha Project is on Test platform at 20%", out["result"])
        out = G.handle("Put Nisha on the Test platform at 90%", user=ADMIN)
        self.assertIn("is free", out["result"])            # refused, with what is actually available
        # other agents keep their work
        self.assertEqual(G.handle("How many casual leave days do we get?", user=ADMIN)["agent"], "policy")

    def test_employees_see_only_their_own(self):
        with as_user(auth.User(8, "ravi", "employee", A)):
            mine = T.run("my_projects")
            self.assertEqual(mine["employee_id"], A)
            self.assertIn("error", T.run("my_projects", employee=B))   # falls back to their own, never B's
            self.assertIn("error", T.run("team_capacity"))
            self.assertIn("error", T.run("allocate_person", employee=B, project="Test platform", percent=10))
        with as_user(auth.User(9, "mgr1", "manager")):
            self.assertIn("people", T.run("team_capacity"))

    def test_trigger(self):
        self.assertEqual(triggers.fire("project_health")["status"], "ok")


class ProjectWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from hrai import web
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, path, token, body=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_api_and_roles(self):
        mgr = auth.login("mgr1", "manager-password-1")
        code, data = self.call("/api/projects", mgr)
        self.assertEqual(code, 200)
        self.assertTrue(any(p["name"] == "Test platform" for p in data["projects"]))
        self.assertIn("utilisation", data["capacity"])
        code, out = self.call("/api/projects/create", mgr, {"name": "Web made project", "client": "Acme",
                                                            "skills": "python, sql"})
        self.assertEqual((code, out["skills"]), (200, ["python", "sql"]))
        code, out = self.call("/api/projects/task", mgr, {"project": out["id"], "title": "First task", "due": "2026-11-01"})
        self.assertEqual(code, 200)
        code, out = self.call("/api/projects/allocate", mgr, {"project": "Web made project", "employee": A, "percent": 200})
        self.assertEqual((code, "error" in out), (400, True))
        employee = auth.login("ravi", "ravi-password-1")
        self.assertEqual(self.call("/api/projects", employee)[0], 403)
        code, out = self.call("/api/projects/mine", employee)
        self.assertEqual((code, out["employee_id"]), (200, A))


if __name__ == "__main__":
    unittest.main()
