"""The HR chat agent brief: your own policy documents, leave balances and calculations from the database,
questions that never book anything, follow-ups in context, and employees kept to their own data."""

import base64
import io
import json
import threading
import unittest
import urllib.request
import zipfile

from tests.common import ADMIN, as_user, bootstrap

from hrai import auth, automations, db, triggers
from hrai import tools as T
from hrai.gateway import agent_gateway as G
from hrai.knowledge import kag, policies

EMP = auth.User(31, "chat-employee", "employee", "E101")

OWN_POLICY = """Acme Leave Policy 2026

1. Annual leave
Every full-time employee earns 24 days of annual leave per calendar year, credited at 2 days per month.
Annual leave must be applied for at least 10 calendar days in advance. Requests of up to 3 working days are
approved automatically. Up to 8 unused days can be carried forward.

2. Casual leave
Employees get 7 days of casual leave per year. Casual leave can be taken for at most 3 consecutive days and
should be applied for at least 2 days in advance.

3. Bereavement leave
Employees get 5 days of paid bereavement leave on the death of an immediate family member.
"""


def docx(paragraphs):
    """A minimal Word file: enough for the reader, which only needs word/document.xml."""
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="w"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def clear_policies():
    for p in policies.policy_dir().rglob("*"):
        if p.is_file():
            p.unlink()
    automations.reindex_policies()


def setUpModule():
    bootstrap()
    clear_policies()
    auth.create_user("chat-hr", "chat-hr-password", "hr")
    auth.create_user("chat-emp", "chat-emp-password", "employee", "E101")


def tearDownModule():
    clear_policies()  # the other test modules expect the sample handbook


class OwnPolicyDocumentTests(unittest.TestCase):
    def tearDown(self):
        clear_policies()

    def test_sample_is_used_until_you_add_your_own(self):
        s = policies.summary()
        self.assertTrue(s["using_sample"])
        self.assertEqual(s["documents"][0]["name"], "Policy handbook")
        self.assertEqual(kag.rule("casual leave", "max_consecutive_days"), 2)

    def test_your_documents_replace_the_sample_and_drive_the_rules(self):
        (policies.policy_dir() / "Acme leave policy.txt").write_text(OWN_POLICY)
        out = automations.reindex_policies()
        self.assertEqual(out["policy_sections"], 3)
        s = policies.summary()
        self.assertFalse(s["using_sample"])
        self.assertEqual(s["documents"][0]["sections"], ["1. Annual leave", "2. Casual leave", "3. Bereavement leave"])
        # the leave rules now come from your document, with it as the citation
        self.assertEqual(kag.rule("casual leave", "max_consecutive_days"), 3)
        self.assertEqual(kag.rule("annual leave", "credit_per_month"), 2)
        self.assertIn("Acme leave policy.txt", kag.source("casual leave", "max_consecutive_days"))
        with as_user(ADMIN):
            hit = T.run("search_policy", query="bereavement leave days")["results"][0]
        self.assertEqual((hit["document"], hit["section"]), ("Acme leave policy.txt", "3. Bereavement leave"))
        out = G.handle("How many days of bereavement leave do we get?", user=EMP, conversation_id="own-docs")
        self.assertIn("5 days of paid bereavement leave", out["result"])
        self.assertIn("Source: Acme leave policy.txt, section 3. Bereavement leave", out["result"])
        # a 3-day casual leave is fine under your policy, though the sample would have refused it
        with as_user(EMP):
            ev = T.run("evaluate_leave_request", employee_id="E101", leave_type="casual",
                       start_date="2026-10-13", end_date="2026-10-15")
        self.assertFalse(any("consecutive" in r for r in ev["reasons"]))

    def test_word_files_with_capital_headings_and_unstructured_text(self):
        (policies.policy_dir() / "Travel.docx").write_bytes(docx(
            ["TRAVEL POLICY", "Book economy class for flights under six hours.", "HOTELS",
             "Hotels are capped at 6,000 rupees a night in metro cities."]))
        (policies.policy_dir() / "Notes.txt").write_text("Office opens at 9.\n\n" + "Badge rules apply. " * 120)
        automations.reindex_policies()
        docs = {d["name"]: d["sections"] for d in policies.summary()["documents"]}
        self.assertEqual(docs["Travel.docx"], ["TRAVEL POLICY", "HOTELS"])
        self.assertTrue(all(s.startswith("Notes (part") for s in docs["Notes.txt"]))
        self.assertGreater(len(docs["Notes.txt"]), 1)

    def test_watch_trigger_picks_up_changes(self):
        triggers.fire("policy_watch")
        self.assertFalse(triggers.fire("policy_watch")["result"]["changed"])
        (policies.policy_dir() / "Acme leave policy.txt").write_text(OWN_POLICY)
        self.assertTrue(triggers.fire("policy_watch")["result"]["changed"])
        self.assertEqual(kag.rule("casual leave", "days_per_year"), 7)


class LeaveQuestionTests(unittest.TestCase):
    def test_balance_comes_from_the_database(self):
        out = G.handle("What is my leave balance?", user=EMP, conversation_id="bal")
        self.assertEqual(out["agent"], "leave")
        e = db.q1("SELECT annual, sick, casual FROM employees WHERE id='E101'")
        self.assertIn(f"You have {e['annual']:g} annual, {e['sick']:g} sick and {e['casual']:g} casual", out["result"])
        self.assertIn("leave_balance", [t["tool"] for t in out["trace"]])

    def test_accrual_projection(self):
        with as_user(EMP):
            b = T.run("leave_balance", as_of="2026-12-31")
        self.assertEqual(b["annual_accrual"]["accrued_this_year"], 18)        # 1.5 a month, capped at 18
        out = G.handle("How much annual leave will I have accrued by December?", user=EMP, conversation_id="acc")
        self.assertIn("18 days have accrued by 2026-12-31", out["result"])

    def test_a_question_about_dates_books_nothing(self):
        before = db.q1("SELECT COUNT(*) AS n FROM leave_requests")["n"]
        casual = db.q1("SELECT casual FROM employees WHERE id='E101'")["casual"]
        out = G.handle("Can I take casual leave on 2026-10-08 and 2026-10-09?", user=EMP, conversation_id="ask")
        self.assertIn("uses 2 working days", out["result"])
        self.assertIn("Nothing has been booked", out["result"])
        self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM leave_requests")["n"], before)
        self.assertEqual(db.q1("SELECT casual FROM employees WHERE id='E101'")["casual"], casual)
        self.assertNotIn("record_leave_decision", [t["tool"] for t in out["trace"]])
        # weekends and holidays are not counted
        out = G.handle("If I take annual leave from 2026-10-16 to 2026-10-19 how many days will be deducted?",
                       user=EMP, conversation_id="ask")
        self.assertIn("uses 2 working days", out["result"])                    # Fri + Mon

    def test_applying_does_book(self):
        before = db.q1("SELECT COUNT(*) AS n FROM leave_requests")["n"]
        out = G.handle("I need sick leave on 2026-10-07", user=EMP, conversation_id="apply")
        self.assertIn("record_leave_decision", [t["tool"] for t in out["trace"]])
        self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM leave_requests")["n"], before + 1)

    def test_employees_only_see_their_own(self):
        out = G.handle("Show leave balance for E102", user=EMP, conversation_id="other")
        self.assertIn("only see and request their own leave", out["result"])
        with as_user(EMP):
            self.assertIn("error", T.run("leave_balance", employee_id="E102"))
            self.assertIn("error", T.run("get_employee", employee_id="E102"))

    def test_personal_policy_answer(self):
        out = G.handle("What is my notice period?", user=EMP, conversation_id="notice")
        self.assertIn("For you (level L2), that is 30 days.", out["result"])


class ConversationTests(unittest.TestCase):
    def test_follow_up_keeps_the_subject(self):
        G.handle("How many casual leave days do we get?", user=EMP, conversation_id="follow")
        out = G.handle("and sick leave?", user=EMP, conversation_id="follow")
        self.assertIn("8 days of paid sick leave", out["result"])

    def test_it_remembers_what_was_asked(self):
        G.handle("What is the internet reimbursement limit?", user=EMP, conversation_id="memory")
        out = G.handle("What did I just ask you?", user=EMP, conversation_id="memory")
        self.assertIn("internet reimbursement", out["result"])
        out = G.handle("What did I just ask you?", user=EMP, conversation_id="fresh")
        self.assertIn("first thing you have asked", out["result"])

    def test_employee_is_never_routed_to_an_agent_they_cannot_use(self):
        out = G.handle("How many days of maternity leave do we offer?", user=EMP, conversation_id="offer")
        self.assertEqual(out["agent"], "policy")
        self.assertIn("26 weeks", out["result"])
        out = G.handle("Am I eligible for leave encashment on my offer?", user=EMP, conversation_id="offer")
        self.assertEqual(out["agent"], "policy")                               # answered, not refused
        with self.assertRaises(G.GatewayError) as cm:                           # real HR-only work is still refused
            G.handle("Screen the new resumes", user=EMP, conversation_id="offer")
        self.assertEqual(cm.exception.status, 403)


class PolicyWebTests(unittest.TestCase):
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
        clear_policies()

    def call(self, path, token, body=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_upload_list_and_remove(self):
        hr, emp = auth.login("chat-hr", "chat-hr-password"), auth.login("chat-emp", "chat-emp-password")
        doc = {"name": "Acme leave policy.txt", "content_b64": base64.b64encode(OWN_POLICY.encode()).decode()}
        self.assertEqual(self.call("/api/policies/upload", emp, {"files": [doc]})[0], 403)
        code, out = self.call("/api/policies/upload", hr, {"files": [doc]})
        self.assertEqual((code, out["staged"][0]["status"], out["using_sample"]), (200, "pending", True))  # not live yet
        code, out = self.call("/api/policies/decide", hr, {"id": out["staged"][0]["id"], "approve": True})
        self.assertEqual((code, out["decided"]["status"], out["using_sample"]), (200, "active", False))
        code, out = self.call("/api/policies", emp)                               # everyone can see the sources
        self.assertEqual(out["documents"][0]["name"], "Acme leave policy.txt")
        code, out = self.call("/api/policies/upload", hr, {"files": [{"name": "evil.exe", "content_b64": ""}]})
        self.assertEqual(code, 400)
        self.assertEqual(self.call("/api/policies/delete", hr, {"name": "../../app.py"})[0], 400)
        code, out = self.call("/api/policies/delete", hr, {"name": "Acme leave policy.txt"})
        self.assertEqual((code, out["using_sample"]), (200, True))
        code, out = self.call("/api/ask", emp, {"request": "What is my leave balance?", "conversation_id": "web"})
        self.assertEqual((code, out["agent"]), (200, "leave"))
        self.assertTrue(out["trace"])                                             # the steps the console shows


if __name__ == "__main__":
    unittest.main()
