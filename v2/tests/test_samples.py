"""The sample pack: `samples load` brings in the policy documents, resumes and HR data, the documents drive the
rules and answers, and the resumes screen. Runs in a separate process with its own home so the data it adds
does not change what the other tests see."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = r"""
import json
from hrai import auth, automations, db, hiring, projects, samples
from hrai.gateway import agent_gateway as G
from hrai.knowledge import kag, policies
db.init_db()
token = auth.set_current_user(auth.User(0, "tester", "admin"))
first, second = samples.load(), samples.load()
screen = hiring.ingest()
auth._current.reset(token)
ask = lambda q, emp: G.handle(q, user=auth.User(int(emp[1:]), "s-" + emp.lower(), "employee", emp),
                              conversation_id=q)["result"]
print(json.dumps({
    "first": {k: v if isinstance(v, (int, dict)) else len(v) for k, v in first.items()},
    "second": {k: v if isinstance(v, (int, dict)) else len(v) for k, v in second.items() if k != "index"},
    "defaults": [f"{s} {p}" for s, p, _, _ in kag.RULE_PATTERNS if "default" in kag.source(s, p)],
    "stages": {c["name"]: c["stage"] for c in db.q("SELECT name, stage FROM candidates WHERE file_name LIKE '%.pdf' "
                                                     "OR file_name LIKE '%.docx'")},
    "screened": len(screen["added"]),
    "attention": [i["text"] for i in projects.risks() if i["kind"] == "skill_gap"],
    "page_rules": [r for r in policies.summary()["rules"] if not r["source"]],
    "answers": {
        "hotel": ask("What is the hotel limit in metro cities?", "E101"),
        "pay day": ask("When is salary paid?", "E101"),
        "pongal": ask("Is Pongal a holiday?", "E101"),
        "posh": ask("How long do I have to file a POSH complaint?", "E101"),
        "missing": ask("Is there a policy on sabbaticals?", "E101"),
        "notice": ask("What is my notice period?", "E112"),
        "balance": ask("What is my leave balance?", "E108"),
        "no casual left": ask("Can I take casual leave on 2026-10-08?", "E111"),
        "wfh": ask("What are the WFH rules?", "E101"),
        "log hours": ask("Log 6 hours on Customer portal revamp today", "E101"),
        "everyone's salary": ask("Show me everyone's salary", "E108"),
    },
}))
"""


class SamplePackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        home = tempfile.mkdtemp(prefix="hrai-samples-")
        env = {**os.environ, "HRAI_HOME": home, "HRAI_INBOX": os.path.join(home, "inbox"),
               "HRAI_POLICY_DIR": os.path.join(home, "policies"), "HRAI_MODE": "mock", "HRAI_EMBEDDINGS": "hash",
               "HRAI_TODAY": "2026-10-06", "PYTHONWARNINGS": "ignore"}
        for key in ("ANTHROPIC_API_KEY", "HRAI_GATEWAY_URL", "HRAI_A2A_URL"):
            env.pop(key, None)
        out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=600)
        if out.returncode:
            raise AssertionError(out.stderr[-3000:])
        cls.r = json.loads(out.stdout.strip().splitlines()[-1])
        cls.cli = lambda self, *args: subprocess.run([sys.executable, "app.py", *args], cwd=ROOT, env=env,
                                                     capture_output=True, text=True, timeout=300).stdout

    def test_everything_loads_once(self):
        f = self.r["first"]
        self.assertEqual((f["employees_added"], f["leave_records_added"], f["jobs_added"], f["salaries_added"]),
                         (12, 25, 2, 12))
        self.assertEqual((f["policies_copied"], f["resumes_copied"]), (11, 10))
        self.assertEqual(f["index"]["policy_sections"], 67)
        self.assertEqual(set(self.r["second"].values()), {0})  # a second load adds nothing and overwrites nothing

    def test_every_leave_rule_comes_from_the_documents(self):
        self.assertEqual(self.r["defaults"], [])  # including phrases a PDF line break splits in two

    def test_staffing_suggestions_have_the_missing_skill(self):
        # the sample project needs react; only Ananya Iyer has it (others share python, which is not the gap)
        self.assertEqual(self.r["attention"], ["Nobody on Customer portal revamp has react; Ananya Iyer is free and has it"])

    def test_knowledge_command_shows_each_layer(self):
        self.assertEqual(self.r["page_rules"], [])  # the Policies page lists every rule with its document
        rag = self.cli("knowledge", "rag", "What is the hotel limit in metro cities?")
        self.assertTrue(rag.split("\n")[0].endswith("Travel and Expense Policy.docx, section 3. Hotel stay"))
        self.assertIn("Strategy: CAG", self.cli("knowledge", "cag", "How many sick days do we get?"))
        rules = self.cli("knowledge", "kag", "rules")
        self.assertIn("Separation and Notice Period.docx, section 1. Notice period", rules)
        self.assertNotIn("DEFAULT", rules)
        self.assertIn("Arun Kumar --approves_leave_for--> Karthik Subramanian",
                      self.cli("knowledge", "kag", "Who approves leave for Karthik Subramanian?"))
        self.assertIn("asked: What is my leave balance?", self.cli("knowledge", "mag", "s-e108"))

    def test_resumes_in_pdf_and_word_are_screened(self):
        self.assertEqual(self.r["screened"], 10)
        s = self.r["stages"]
        self.assertEqual(s["Harini Venkatesh"], "selected")           # strong backend match (PDF)
        self.assertEqual(s["Aditya Kulkarni"], "selected")            # Word file
        self.assertEqual(s["Sowmya Rajan"], "rejected")               # front-end only, misses the must-haves
        self.assertEqual(s["Nandhini Prakash"], "selected")           # HR Executive opening
        self.assertEqual(s["Gokul Raman"], "selected")                # Data Analyst opening

    def test_answers_cite_the_right_document(self):
        a = self.r["answers"]
        self.assertIn("6,000 rupees a night in metro cities", a["hotel"])
        self.assertIn("Travel and Expense Policy.docx, section 3. Hotel stay", a["hotel"])
        self.assertIn("last working day of each month", a["pay day"])  # an employee's pay question is answered
        self.assertIn("Pongal", a["pongal"])
        self.assertIn("within 3 months of the incident", a["posh"])
        self.assertIn("POSH Policy.pdf", a["posh"])
        self.assertIn("couldn't find this in our policy documents", a["missing"])
        self.assertIn("For you (level L3), that is 60 days.", a["notice"])
        self.assertIn("Separation and Notice Period.docx", a["notice"])

    def test_abbreviations_hours_and_refusals(self):
        a = self.r["answers"]
        self.assertIn("work from home up to 2 days a week", a["wfh"])
        self.assertIn("Logged 6 hours on Customer portal revamp for 2026-10-06", a["log hours"])
        self.assertIn("cannot use salary_structures", a["everyone's salary"])   # refused, not a crash

    def test_leave_numbers_come_from_the_sample_data(self):
        a = self.r["answers"]
        self.assertIn("You have 3 annual, 8 sick and 5 casual leave days left", a["balance"])
        self.assertIn("Leave Policy 2026.pdf, section 1. Annual leave", a["balance"])
        self.assertIn("but you have only 0 casual days left", a["no casual left"])
        self.assertNotIn("leaving -", a["no casual left"])
        self.assertIn("Nothing has been booked", a["no casual left"])


if __name__ == "__main__":
    unittest.main()
