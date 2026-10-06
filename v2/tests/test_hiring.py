"""Hiring pipeline: inbox -> screening -> rounds -> offer approval -> joining -> follow-ups."""

import base64
import json
import shutil
import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path

from tests.common import ADMIN, as_user, bootstrap

from hrai import auth, config, db, hiring, triggers
from hrai import tools as T
from hrai.gateway import agent_gateway as G


def setUpModule():
    bootstrap()
    # A fresh job so these tests do not depend on the sample candidates other tests move around.
    db.x("INSERT OR REPLACE INTO jobs (id, title, location, min_years, must_have, nice_to_have, shortlist_size, rounds, "
         "select_threshold) VALUES (?,?,?,?,?,?,?,?,?)",
         ("JOB-900", "Data Engineer", "Chennai", 3, json.dumps(["python", "sql"]), json.dumps(["spark", "airflow"]), 3,
          json.dumps(["L1", "L2", "L3", "HR", "Final"]), 80))


def make_docx(path, lines):
    body = "".join(f"<w:p><w:r><w:t>{l}</w:t></w:r></w:p>" for l in lines)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<w:document xmlns:w="w"><w:body>{body}</w:body></w:document>')


def make_pdf(path, text):
    """A minimal one-page PDF with real text, written by hand (no PDF library needed)."""
    stream = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    Path(path).write_bytes(out)


class HiringPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inbox = hiring.inbox_root() / "JOB-900"
        cls.inbox.mkdir(parents=True, exist_ok=True)
        (cls.inbox / "ravi.txt").write_text("Name: Ravi Teja\nEmail: ravi@example.com\nPhone: +91 98765 43210\n"
                                            "5 years experience. Python, SQL, Spark, Airflow pipelines.")
        make_docx(cls.inbox / "lata.docx", ["Lata Menon", "lata@example.com", "4 years experience",
                                            "Skills: Python, SQL, dashboards"])
        make_pdf(cls.inbox / "omar.pdf", "Omar Sheikh omar@example.com 6 years experience Java and Spring only")
        (cls.inbox / "notes.xlsx").write_text("ignored")
        with as_user(ADMIN):
            cls.report = T.run("ingest_resumes", job_id="JOB-900")
        cls.ids = {a["name"].split()[0]: a["candidate_id"] for a in cls.report["added"]}
        cls.sorted_files = {p.relative_to(cls.inbox / "sorted").as_posix() for p in (cls.inbox / "sorted").rglob("*.*")}

    def test_ingest_reads_all_formats_and_sorts(self):
        stages = {a["name"].split()[0]: a["stage"] for a in self.report["added"]}
        self.assertEqual(stages["Ravi"], "selected")    # all must-haves, nice-to-haves, 5 years
        self.assertEqual(stages["Lata"], "on_hold")     # .docx read; meets the minimum, scores under the threshold
        self.assertEqual(stages["Omar"], "rejected")    # .pdf read; misses python and sql
        self.assertEqual(hiring.get(self.ids["Omar"])["email"], "omar@example.com")
        c = hiring.get(self.ids["Ravi"])
        self.assertEqual((c["email"], c["phone"], c["years"]), ("ravi@example.com", "+91 98765 43210", 5))
        self.assertEqual(self.sorted_files, {"selected/ravi.txt", "on_hold/lata.docx", "rejected/omar.pdf"})

    def test_reingest_is_quiet_and_duplicates_detected(self):
        with as_user(ADMIN):
            again = T.run("ingest_resumes", job_id="JOB-900")
            self.assertEqual(again["added"], [])
            (self.inbox / "ravi_v2.txt").write_text("Name: Ravi T\nEmail: ravi@example.com\n5 years. Python SQL Spark")
            dup = T.run("ingest_resumes", job_id="JOB-900")
        self.assertEqual(dup["added"], [])
        self.assertIn("ravi_v2.txt", dup["duplicates"][0])

    def test_full_journey_through_custom_rounds(self):
        cid = self.ids["Ravi"]
        with as_user(ADMIN):
            for i, rnd in enumerate(["L1", "L2", "L3", "HR"]):
                s = T.run("schedule_interview", candidate=cid, when=f"2026-10-{10 + i}T11:00", interviewer="Arun Kumar")
                self.assertEqual(s["round"], rnd)  # defaults to the next round
                r = T.run("record_interview_result", candidate=cid, round_name=rnd, result="pass", rating=4, feedback="good")
                self.assertEqual(r["stage"], "interviewing")
            r = T.run("record_interview_result", candidate=cid, round_name="Final", result="pass", rating=5)
            self.assertEqual(r["stage"], "offer")
            self.assertIn("error", T.run("record_offer_response", candidate=cid, accepted=True))  # nothing sent yet
            o = T.run("make_offer", candidate=cid, ctc_lpa=18.5, joining_date="2026-11-16")
            self.assertEqual(o["status"], "pending_approval")
            T.decide_approval(o["approval_id"], True)
            self.assertEqual(db.q1("SELECT status FROM offers WHERE id=?", (o["offer_id"],))["status"], "sent")
            self.assertTrue(db.q1("SELECT 1 AS y FROM outbox WHERE subject LIKE 'Offer of employment%' AND to_addr='ravi@example.com'"))
            acc = T.run("record_offer_response", candidate=cid, accepted=True)
            self.assertEqual(acc["joining_date"], "2026-11-16")
            hire = db.q1("SELECT * FROM new_hires WHERE id=?", (acc["new_hire_id"],))
            self.assertEqual(hire["candidate_id"], cid)
            self.assertGreater(db.q1("SELECT COUNT(*) AS n FROM onboarding_tasks WHERE hire_id=?", (hire["id"],))["n"], 0)
            kinds = {f["kind"]: f["due"] for f in db.q("SELECT * FROM followups WHERE candidate_id=?", (cid,))}
            self.assertEqual(kinds["pre_joining_call"], "2026-11-09")
            self.assertEqual(kinds["probation_review"], "2027-02-14")
            self.assertEqual(T.run("mark_joined", candidate=cid)["stage"], "joined")
            tl = T.run("candidate_timeline", candidate="Ravi Teja")
        events = [e["event"] for e in tl["events"]]
        for e in ("applied", "screened_selected", "L1_pass", "Final_pass", "offer_sent", "moved_joined"):
            self.assertIn(e, events)

    def test_fail_rejects_and_hold(self):
        cid = self.ids["Lata"]
        with as_user(ADMIN):
            T.run("move_candidate", candidate=cid, stage="selected", note="HR override: strong portfolio")
            T.run("schedule_interview", candidate=cid, round_name="L1", when="2026-10-12T10:00")
            self.assertEqual(T.run("record_interview_result", candidate=cid, round_name="L1", result="hold")["stage"], "on_hold")
            T.run("schedule_interview", candidate=cid, round_name="L1", when="2026-10-14T10:00")
            out = T.run("record_interview_result", candidate=cid, round_name="L1", result="fail", feedback="weak SQL")
        self.assertEqual(out["stage"], "rejected")
        self.assertTrue(db.q1("SELECT 1 AS y FROM outbox WHERE to_addr='lata@example.com' AND subject LIKE 'Your application%'"))

    def test_board_and_validation(self):
        with as_user(ADMIN):
            board = T.run("pipeline_summary", job_id="JOB-900")["board"][0]
            self.assertEqual([c["key"] for c in board["columns"]][3:8], ["L1", "L2", "L3", "HR", "Final"])
            self.assertIn("error", T.run("record_interview_result", candidate=self.ids["Omar"], round_name="L9", result="maybe"))
            self.assertIn("error", T.run("schedule_interview", candidate=self.ids["Omar"]))  # rejected people can't be scheduled
        with as_user(auth.get_user("deepa")):
            self.assertIn("error", T.run("pipeline_summary"))  # employees cannot see hiring

    def test_agent_rules_mode_and_routing(self):
        out = G.handle("What hiring follow-ups are due?", user=ADMIN)
        self.assertEqual(out["agent"], "recruitment")
        out = G.handle("Show the hiring pipeline status for JOB-900", user=ADMIN)
        self.assertIn("Data Engineer", out["result"])

    def test_triggers(self):
        self.assertIn("inbox_watch", triggers.SCHEDULES)
        (self.inbox / "kiran.txt").write_text("Name: Kiran Rao\nEmail: kiran@example.com\n4 years. Python SQL Spark")
        r = triggers.fire("inbox_watch")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["result"]["added"][0]["name"], "Kiran Rao")
        self.assertEqual(triggers.fire("hiring_followups")["status"], "ok")
        self.assertEqual(triggers.fire("stale_candidates")["status"], "ok")


class HiringWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from hrai import web
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.token = auth.login("hr1", "hr-password-1")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, path, body):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_upload_and_actions(self):
        content = base64.b64encode(b"Name: Web Upload\nEmail: web@example.com\n6 years. Python SQL Spark Airflow").decode()
        code, out = self.post("/api/hiring/upload", {"job_id": "JOB-900", "files": [{"name": "web upload.txt", "content_b64": content}]})
        self.assertEqual(code, 200)
        cid = out["added"][0]["candidate_id"]
        self.assertEqual(out["added"][0]["stage"], "selected")
        code, _ = self.post("/api/hiring/upload", {"job_id": "JOB-900", "files": [{"name": "evil.sh", "content_b64": content}]})
        self.assertEqual(code, 400)
        code, out = self.post(f"/api/hiring/candidates/{cid}/schedule", {"round": "L1", "when": "2026-10-20T10:00"})
        self.assertEqual((code, out["round"]), (200, "L1"))
        code, out = self.post(f"/api/hiring/candidates/{cid}/result", {"round": "L1", "result": "pass", "rating": "5"})
        self.assertEqual(out["next_round"], "L2")


if __name__ == "__main__":
    unittest.main()
