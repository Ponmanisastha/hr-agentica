"""Uploads from the web console: policy documents become new versions that HR approves (with a diff and
conflict check, an archive and rollback), and employee and new-hire documents land in one folder per person
and type, where HR checks them."""

import base64
import json
import threading
import unittest
import urllib.request

from tests.common import ADMIN, as_user, bootstrap

from hrai import auth, automations, db, documents
from hrai import tools as T
from hrai.knowledge import kag, policies, versions

HR = auth.User(41, "up-hr", "hr")
MANAGER = auth.User(42, "up-manager", "manager", "E102")
EMP = auth.User(43, "up-employee", "employee", "E101")

LEAVE = """Up Leave Policy

1. Casual leave
Employees get 6 days of casual leave per year. Casual leave can be taken for at most 2 consecutive days.

2. Annual leave
Every full-time employee earns 20 days of annual leave per calendar year.
"""


def clear_policies():
    for p in policies.policy_dir().rglob("*"):
        if p.is_file():
            p.unlink()
    db.x("DELETE FROM policy_versions")
    db.x("DELETE FROM approvals WHERE kind='policy_version'")
    automations.reindex_policies()


def setUpModule():
    bootstrap()
    clear_policies()


def tearDownModule():
    clear_policies()


class PolicyVersionTests(unittest.TestCase):
    def setUp(self):
        clear_policies()

    def publish(self, name, text):
        with as_user(HR):
            v = versions.stage(name, text.encode())
            return versions.decide(v["id"], True)

    def test_upload_waits_for_approval_then_goes_live(self):
        with as_user(HR):
            v = versions.stage("Up leave.md", LEAVE.encode())
        self.assertEqual((v["status"], v["version"]), ("pending", 1))
        self.assertFalse((policies.policy_dir() / "Up leave.md").exists())     # not used yet
        self.assertIn("new document", v["message"])
        approval = db.q1("SELECT * FROM approvals WHERE id=?", (v["approval_id"],))
        self.assertEqual((approval["kind"], approval["status"]), ("policy_version", "pending"))
        with as_user(HR):
            live = versions.decide(v["id"], True)
        self.assertEqual(live["status"], "active")
        self.assertEqual(db.q1("SELECT status FROM approvals WHERE id=?", (v["approval_id"],))["status"], "approved")
        self.assertEqual(kag.rule("annual leave", "days_per_year"), 20)
        with as_user(HR):                                                       # same file again: nothing to do
            self.assertEqual(versions.stage("Up leave.md", LEAVE.encode())["status"], "unchanged")

    def test_new_version_shows_changes_and_can_be_rolled_back(self):
        self.publish("Up leave.md", LEAVE)
        with as_user(HR):
            v = versions.stage("Up leave.md", LEAVE.replace("20 days", "22 days").encode())
        changes = v["changes"]
        self.assertEqual(v["version"], 2)
        self.assertEqual([s["section"] for s in changes["changed"]], ["2. Annual leave"])
        rule = next(r for r in changes["rule_changes"] if r["rule"].startswith("Annual leave: days a year"))
        self.assertEqual((rule["before"], str(rule["after"])), ("20", "22"))
        self.assertEqual(kag.rule("annual leave", "days_per_year"), 20)         # old version still answers
        with as_user(HR):
            versions.decide(v["id"], True)
        self.assertEqual(kag.rule("annual leave", "days_per_year"), 22)
        self.assertEqual(versions.cite("Up leave.md"), "Up leave.md (version 2)")
        hist = versions.history("Up leave.md")
        self.assertEqual(sorted((h["version"], h["status"]) for h in hist), [(1, "archived"), (2, "active")])
        with as_user(HR):
            versions.rollback("Up leave.md", 1)
        self.assertEqual(kag.rule("annual leave", "days_per_year"), 20)
        self.assertEqual(versions.active("Up leave.md")["version"], 1)

    def test_conflicts_with_another_document_are_flagged(self):
        self.publish("Up leave.md", LEAVE)
        with as_user(HR):
            v = versions.stage("Casual update.md",
                               b"Casual leave (2027)\nCasual leave can be taken for at most 3 consecutive days.\n")
        clashes = v["conflicts"]
        rule = [c for c in clashes if c["kind"] == "rule"]
        self.assertEqual(len(rule), 1)
        self.assertEqual((rule[0]["here"], rule[0]["other_value"]), ("3", "2"))
        self.assertIn("Up leave.md", rule[0]["other"])
        self.assertIn("disagree", v["message"])
        with as_user(HR):
            versions.decide(v["id"], True)
        self.assertEqual(kag.rule("casual leave", "max_consecutive_days"), 3)   # newest approved document wins

    def test_reject_withdraw_retire(self):
        self.publish("Up leave.md", LEAVE)
        with as_user(HR):
            a = versions.stage("Up leave.md", LEAVE.replace("20 days", "21 days").encode())
            b = versions.stage("Up leave.md", LEAVE.replace("20 days", "25 days").encode())
            self.assertEqual(versions.get(a["id"])["status"], "withdrawn")      # a newer upload replaces it
            versions.decide(b["id"], False, "not agreed")
        self.assertEqual(versions.get(b["id"])["status"], "rejected")
        self.assertEqual(kag.rule("annual leave", "days_per_year"), 20)
        with as_user(HR):
            versions.retire("Up leave.md")
        self.assertFalse((policies.policy_dir() / "Up leave.md").exists())
        self.assertIn("Up leave.md", [r["name"] for r in policies.retired()])
        with as_user(HR):
            versions.rollback("Up leave.md", 1)
        self.assertTrue((policies.policy_dir() / "Up leave.md").exists())

    def test_files_copied_into_the_folder_are_picked_up(self):
        (policies.policy_dir() / "Dropped.md").write_text(LEAVE)
        automations.reindex_policies()
        self.assertEqual(versions.active("Dropped.md")["version"], 1)

    def test_only_policy_managers_stage_or_approve(self):
        with as_user(EMP), self.assertRaises(PermissionError):
            versions.stage("Up leave.md", LEAVE.encode())
        with as_user(HR):
            v = versions.stage("Up leave.md", LEAVE.encode())
        with as_user(MANAGER), self.assertRaises(PermissionError):              # managers decide leave, not policy
            T.decide_approval(v["approval_id"], True)
        self.assertEqual(db.q1("SELECT status FROM approvals WHERE id=?", (v["approval_id"],))["status"], "pending")
        with as_user(HR):
            T.decide_approval(v["approval_id"], True)                           # the Approvals page works too
        self.assertEqual(versions.get(v["id"])["status"], "active")
        with as_user(HR), self.assertRaises(versions.PolicyError):
            versions.stage("run.exe", b"x")


class DocumentTests(unittest.TestCase):
    def test_new_hire_documents_unblock_onboarding(self):
        with as_user(ADMIN):
            T.run("create_onboarding_plan", hire_id="NH-202")
        tasks = {t["task"][:13]: t["status"] for t in db.q("SELECT task, status FROM onboarding_tasks WHERE hire_id='NH-202'")}
        self.assertEqual(tasks["Set up payrol"], "blocked: missing documents")
        with as_user(HR):
            for t in ("pan_card", "bank_details"):
                d = documents.save("NH-202", t, f"{t}.pdf", b"%PDF-1.4 " + t.encode())
                self.assertTrue((documents.root() / d["path"]).is_file())
                self.assertIn(f"new-hires/NH-202/{t}/", d["path"])
        status = db.q1("SELECT status FROM onboarding_tasks WHERE hire_id='NH-202' AND task LIKE 'Set up payroll%'")
        self.assertEqual(status["status"], "to do")
        with as_user(HR):
            c = documents.checklist("NH-202")
            self.assertNotIn("pan_card", c["missing"])
            pan = next(i for i in c["items"] if i["type"] == "pan_card")
            documents.review(pan["files"][0]["id"], "rejected", "blurred")      # asked for again
            self.assertIn("pan_card", documents.checklist("NH-202")["missing"])
        status = db.q1("SELECT status FROM onboarding_tasks WHERE hire_id='NH-202' AND task LIKE 'Set up payroll%'")
        self.assertEqual(status["status"], "blocked: missing documents")

    def test_employees_see_only_their_own(self):
        with as_user(EMP):
            d = documents.save("E101", "medical_certificate", "note.txt", b"rest for 3 days")
            self.assertEqual(documents.save("E101", "medical_certificate", "again.txt", b"rest for 3 days")["id"], d["id"])
            with self.assertRaises(PermissionError):
                documents.save("E102", "medical_certificate", "x.txt", b"x")
            with self.assertRaises(PermissionError):
                documents.checklist("E102")
            with self.assertRaises(PermissionError):
                documents.review(d["id"], "verified")
            with self.assertRaises(documents.DocumentError):
                documents.save("E101", "medical_certificate", "x.exe", b"x")
        self.assertIn("employees/E101/medical_certificate/", d["path"])
        with as_user(HR):
            self.assertEqual(documents.review(d["id"], "verified")["status"], "verified")


class UploadWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from hrai import web
        auth.create_user("up-web-hr", "up-web-hr-password", "hr")
        auth.create_user("up-web-emp", "up-web-emp-password", "employee", "E103")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        clear_policies()

    def call(self, path, token, body=None, raw=False):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(r) as resp:
                data = resp.read()
                return resp.status, (data, resp.headers) if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_policy_upload_diff_and_rollback(self):
        clear_policies()
        hr = auth.login("up-web-hr", "up-web-hr-password")
        b64 = lambda s: base64.b64encode(s.encode()).decode()  # noqa: E731
        code, out = self.call("/api/policies/upload", hr, {"files": [{"name": "Web leave.md", "content_b64": b64(LEAVE)}]})
        self.call("/api/policies/decide", hr, {"id": out["staged"][0]["id"], "approve": True})
        code, out = self.call("/api/policies/upload", hr, {"files": [{"name": "Web leave.md",
                                                                       "content_b64": b64(LEAVE.replace("20 days", "24 days"))}]})
        self.assertEqual(code, 200)
        self.assertEqual(out["pending"][0]["name"], "Web leave.md")
        self.call("/api/policies/decide", hr, {"id": out["staged"][0]["id"], "approve": True})
        code, out = self.call("/api/policies", hr)
        doc = next(d for d in out["documents"] if d["name"] == "Web leave.md")
        self.assertEqual(doc["version"], 2)
        code, out = self.call("/api/policies/rollback", hr, {"name": "Web leave.md", "version": 1})
        self.assertEqual(code, 200)
        self.assertEqual(kag.rule("annual leave", "days_per_year"), 20)

    def test_documents_upload_download_and_privacy(self):
        hr, emp = auth.login("up-web-hr", "up-web-hr-password"), auth.login("up-web-emp", "up-web-emp-password")
        file = {"name": "proof.txt", "content_b64": base64.b64encode(b"80C proof").decode()}
        code, out = self.call("/api/documents/upload", emp, {"doc_type": "investment_proof", "files": [file]})
        self.assertEqual((code, out["owner_id"]), (200, "E103"))
        doc_id = out["saved"][0]["id"]
        code, (data, headers) = self.call(f"/api/documents/file/{doc_id}", emp, raw=True)
        self.assertEqual((code, data), (200, b"80C proof"))
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.call("/api/documents/checklist?owner=E101", emp)[0], 403)
        self.assertEqual(self.call("/api/documents/upload", emp, {"owner_id": "E101", "doc_type": "other",
                                                                  "files": [file]})[0], 403)
        code, out = self.call("/api/documents", hr)
        self.assertGreaterEqual(out["to_review"], 1)
        code, out = self.call("/api/documents/review", hr, {"id": doc_id, "status": "verified"})
        self.assertEqual((code, out["reviewed"]["status"]), (200, "verified"))
        self.assertEqual(self.call("/api/documents/review", emp, {"id": doc_id, "status": "verified"})[0], 403)


if __name__ == "__main__":
    unittest.main()
