"""Insights: funnel and round numbers, the attention list, the insights agent, the daily report, the web API and CSV."""

import json
import threading
import unittest
import urllib.request

from tests.common import ADMIN, as_user, bootstrap

from hrai import auth, config, db, hiring, insights, triggers
from hrai import tools as T
from hrai.gateway import agent_gateway as G

JOB = "JOB-950"


def setUpModule():
    bootstrap()
    db.x("INSERT OR REPLACE INTO jobs (id, title, location, min_years, must_have, nice_to_have, shortlist_size, rounds, "
         "select_threshold) VALUES (?,?,?,?,?,?,?,?,?)",
         (JOB, "QA Engineer", "Pune", 2, json.dumps(["selenium", "python"]), json.dumps(["jenkins"]), 3,
          json.dumps(["L1", "HR"]), 70))
    folder = hiring.inbox_root() / JOB
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "asha.txt").write_text("Name: Asha Rao\nEmail: asha@example.com\n4 years. Selenium, Python, Jenkins.")
    (folder / "bala.txt").write_text("Name: Bala Murugan\nEmail: bala@example.com\n3 years. Selenium, Python.")
    (folder / "chitra.txt").write_text("Name: =HYPERLINK(\"http://x\")\nEmail: chitra@example.com\n5 years. Manual testing.")
    with as_user(ADMIN):
        hiring.ingest(folder, JOB)


def cid(email):
    return db.q1("SELECT id FROM candidates WHERE email=?", (email,))["id"]


class InsightsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with as_user(ADMIN):
            asha = cid("asha@example.com")
            hiring.schedule(asha, "L1", "2026-10-02T10:00")  # in the past: its result is now overdue
            hiring.record_result(asha, "L1", "pass", 4, "good")
            hiring.schedule(asha, "HR", "2026-10-03T10:00")
            hiring.record_result(asha, "HR", "pass", 5, "great")
            hiring.make_offer(asha, 9.5, "2026-11-02")
            bala = cid("bala@example.com")
            hiring.schedule(bala, "L1", "2026-10-01T10:00")

    def test_funnel_rounds_and_skills(self):
        h = insights.hiring(JOB)
        f = {s["key"]: s["count"] for s in h["funnel"]}
        self.assertEqual((f["applied"], f["screened_in"], f["interviewed"], f["offered"]), (3, 2, 1, 0))
        l1 = next(r for r in h["rounds"] if r["round"] == "L1")
        self.assertEqual((l1["pass"], l1["scheduled"], l1["pass_rate"], l1["avg_rating"]), (1, 1, 100, 4.0))
        self.assertEqual([r["round"] for r in h["rounds"]], ["L1", "HR"])  # job order, not alphabetical
        self.assertEqual({m["skill"] for m in h["missing_skills"]}, {"selenium", "python"})
        self.assertEqual(h["jobs"][0]["rejected"], 1)

    def test_attention_list(self):
        texts = [a["text"] for a in insights.attention(50)]
        self.assertTrue(any("Approval waiting: Offer for Asha Rao" in t for t in texts), texts)
        self.assertTrue(any("Record the L1 result for Bala Murugan" in t for t in texts), texts)
        self.assertFalse(any("Remind Asha Rao and the panel about L1" in t for t in texts),
                         "a reminder for an interview that already happened must be closed")
        self.assertEqual(sorted(a["priority"] for a in insights.attention(50)), [a["priority"] for a in insights.attention(50)])

    def test_overview_and_narrative(self):
        d = insights.overview()
        self.assertGreaterEqual(d["kpis"]["offers_out"], 1)
        self.assertEqual(len(d["ai"]["daily"]), 30)
        text = insights.narrate(d)
        self.assertIn("HR snapshot for 2026-10-06", text)
        self.assertIn("Needs attention", text)

    def test_csv_escapes_formulas(self):
        out = insights.candidates_csv(JOB)
        self.assertIn("'=HYPERLINK", out)
        self.assertNotIn(",=HYPERLINK", out)

    def test_agent_and_tools(self):
        out = G.handle("How is hiring going?", user=ADMIN)
        self.assertEqual(out["agent"], "insights")
        self.assertIn("HR snapshot", out["result"])
        out = G.handle("What needs my attention today?", user=ADMIN)
        self.assertEqual(out["agent"], "insights")
        self.assertIn("Asha Rao", out["result"])
        # Leave-policy questions still go to the policy agent
        self.assertEqual(G.handle("How many casual leave days do we get?", user=ADMIN)["agent"], "policy")
        with as_user(auth.User(9, "deepa", "employee", "E101")):
            self.assertIn("error", T.run("hr_insights"))
        with as_user(ADMIN):
            self.assertIn("error", T.run("hr_insights", section="salary"))
            self.assertIn("leave", T.run("hr_insights", section="leave"))

    def test_daily_report_trigger(self):
        r = triggers.fire("insights_report")
        self.assertEqual(r["status"], "ok")
        self.assertTrue((config.home() / "reports" / "insights-2026-10-06.md").exists())
        self.assertFalse(r["result"]["email_drafted"])  # 2026-10-06 is a Tuesday; the email goes out on Mondays


class InsightsWebTests(unittest.TestCase):
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

    def get(self, path, token):
        r = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read().decode()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, None, e.read().decode()

    def test_dashboard_api_and_roles(self):
        hr = auth.login("hr1", "hr-password-1")
        code, _, body = self.get(f"/api/insights?job={JOB}", hr)
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["hiring"]["jobs"][0]["job_id"], JOB)
        code, ctype, body = self.get("/api/insights/candidates.csv", hr)
        self.assertEqual((code, ctype.split(";")[0]), (200, "text/csv"))
        self.assertTrue(body.startswith("candidate_id,name"))
        employee = auth.login("deepa", "deepa-password")
        self.assertEqual(self.get("/api/insights", employee)[0], 403)
        self.assertEqual(self.get("/api/insights/candidates.csv", employee)[0], 403)


if __name__ == "__main__":
    unittest.main()
