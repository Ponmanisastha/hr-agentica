"""Salary and payroll: CTC breakup, PF/ESI/PT/TDS, revisions and runs behind approval, payslip privacy, the web API."""

import json
import threading
import unittest
import urllib.request

from tests.common import ADMIN, as_user, bootstrap

from hrai import auth, config, db, payroll, triggers
from hrai import tools as T
from hrai.gateway import agent_gateway as G

EMP = "E901"
MONTH = "2026-10"


def setUpModule():
    bootstrap()
    db.x("INSERT OR REPLACE INTO employees (id, name, email, department, level, manager, manager_email, annual, sick, "
         "casual) VALUES (?,?,?,?,?,?,?,?,?,?)",
         (EMP, "Payroll Tester", "ptester@example.com", "Engineering", "L2", "Arun Kumar", "arun.kumar@example.com", 12, 6, 4))
    auth.create_user("hr2", "hr-password-2", "hr")
    auth.create_user("ptester", "ptester-password", "employee", EMP)
    with as_user(ADMIN):
        payroll.set_structure(EMP, 1200000, "2026-04-01", True, "new", "TN", "ABCPT1234F", "100200300999",
                              "50100099999999", "HDFC0009999")


def finalise_month(month=MONTH):
    """Make sure the month's payroll exists and is approved (tests run in any order)."""
    if payroll.summary(month)["status"] in ("approved", "paid"):
        return
    with as_user(ADMIN):
        payroll.run_payroll(month)
        out = payroll.submit(month)
    with as_user(auth.User(5, "hr2", "hr")):
        T.decide_approval(out["approval_id"], True)


class BreakupAndTaxTests(unittest.TestCase):
    def test_breakup_components_add_up(self):
        b = payroll.breakup(1200000, metro=True)
        self.assertEqual(b["annual"]["basic"], 600000)          # 50% of CTC
        self.assertEqual(b["annual"]["hra"], 300000)            # 50% of basic in a metro
        self.assertEqual(b["employee_deductions_monthly"]["pf"], 1800)  # 12% of the 15,000 ceiling
        self.assertFalse(b["esi_applicable"])
        self.assertEqual(b["annual"]["gross"] + b["annual"]["employer_pf"] + b["annual"]["gratuity"], 1200000)
        self.assertEqual(payroll.breakup(1200000)["annual"]["hra"], 240000)  # 40% outside a metro

    def test_low_salary_gets_esi(self):
        b = payroll.breakup(240000)
        self.assertTrue(b["esi_applicable"])
        self.assertGreater(b["annual"]["employer_esi"], 0)
        self.assertGreater(b["employee_deductions_monthly"]["esi"], 0)
        self.assertAlmostEqual(b["annual"]["gross"] + b["annual"]["employer_pf"] + b["annual"]["employer_esi"]
                               + b["annual"]["gratuity"], 240000, delta=2)

    def test_professional_tax_by_state(self):
        self.assertEqual(payroll.professional_tax(95795, MONTH, "TN"), 208)   # 1,250 per half year
        self.assertEqual(payroll.professional_tax(95795, MONTH, "KA"), 200)
        self.assertEqual(payroll.professional_tax(95795, "2026-02", "KA"), 300)  # February extra
        self.assertEqual(payroll.professional_tax(95795, MONTH, "NONE"), 0)

    def test_income_tax_rebate_and_regimes(self):
        self.assertEqual(payroll.income_tax(1100000, "new")["total_tax"], 0)      # inside the 87A rebate
        self.assertGreater(payroll.income_tax(2000000, "new")["total_tax"], 0)
        self.assertGreater(payroll.income_tax(5100000, "new")["surcharge"], 0)
        no_pan = payroll.income_tax(1500000, "new", pan=False)["total_tax"]
        self.assertGreaterEqual(no_pan, 0.20 * (1500000 - 75000))
        cmp = payroll.compare_regimes(1200000, True, {"rent_annual": 240000, "80C": 150000, "80D": 25000})
        self.assertIn(cmp["better"], ("new", "old"))
        self.assertEqual(cmp["new"]["tax"], 0)

    def test_hra_exemption(self):
        self.assertEqual(payroll.hra_exemption(600000, 300000, 0, True), 0)
        self.assertEqual(payroll.hra_exemption(600000, 300000, 240000, True), 180000)  # rent - 10% of basic


class PayrollRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # this class owns the run for MONTH, so it starts from a clean slate
        db.x("DELETE FROM payslips WHERE month=?", (MONTH,))
        db.x("DELETE FROM payroll_runs WHERE month=?", (MONTH,))
        db.x("DELETE FROM pay_adjustments WHERE month=?", (MONTH,))

    def test_payslip_lop_and_adjustments(self):
        with as_user(ADMIN):
            payroll.add_adjustment(EMP, MONTH, "bonus", 50000, "Festival bonus")
            payroll.add_adjustment(EMP, MONTH, "lop_days", 2, "Unpaid days")
            p = payroll.compute_payslip(EMP, MONTH)
        self.assertEqual((p["paid_days"], p["lop_days"]), (29, 2))
        self.assertEqual(p["earnings"]["bonus"], 50000)
        self.assertLess(p["earnings"]["basic"], 50000)  # pro-rated for the unpaid days
        self.assertEqual(p["net"], p["gross"] - p["total_deductions"])
        self.assertEqual(p["deductions"]["professional_tax"], 208)

    def test_run_submit_approve_and_pay(self):
        with as_user(ADMIN):
            s = payroll.run_payroll(MONTH)
            self.assertGreaterEqual(s["employees"], 1)
            self.assertEqual(s["status"], "draft")
            with self.assertRaises(payroll.PayrollError):
                payroll.mark_paid(MONTH, "REF")          # not approved yet
            out = payroll.submit(MONTH)
            self.assertEqual(out["status"], "pending_approval")
            with self.assertRaises(payroll.PayrollError):
                payroll.add_adjustment(EMP, MONTH, "bonus", 1000)   # locked once submitted
            # the submitter may not approve their own run
            with self.assertRaises(PermissionError):
                T.decide_approval(out["approval_id"], True)
            self.assertEqual(db.q1("SELECT status FROM approvals WHERE id=?", (out["approval_id"],))["status"], "pending")
        with as_user(auth.User(5, "hr2", "hr")):
            T.decide_approval(out["approval_id"], True)
        self.assertEqual(payroll.summary(MONTH)["status"], "approved")
        folder = config.home() / "payroll" / MONTH
        self.assertTrue((folder / "bank_transfer.csv").exists())
        self.assertTrue((folder / f"payslip-{EMP}.txt").exists())
        self.assertIn("HDFC0009999", payroll.bank_file(MONTH))
        with as_user(ADMIN):
            self.assertEqual(payroll.mark_paid(MONTH, "NEFT-77881")["status"], "paid")
            with self.assertRaises(payroll.PayrollError):
                payroll.run_payroll(MONTH)               # a paid month cannot be recomputed

    def test_tds_spreads_over_the_year(self):
        with as_user(ADMIN):
            db.x("INSERT OR REPLACE INTO employees (id, name, email, department, level, manager, manager_email) "
                 "VALUES ('E902','High Earner','he@example.com','Engineering','L4','Arun Kumar','arun.kumar@example.com')")
            payroll.set_structure("E902", 3600000, "2026-04-01", True, "new", "TN", "ABCHE1234G", "1", "2", "HDFC0001")
            p = payroll.compute_payslip("E902", "2026-11")
        self.assertGreater(p["deductions"]["tds"], 0)
        year_tax = payroll.income_tax(payroll.breakup(3600000, True)["annual"]["gross"], "new")["total_tax"]
        self.assertLess(p["deductions"]["tds"], year_tax)  # one month's share, not the whole year


class RevisionTests(unittest.TestCase):
    def test_revision_needs_approval(self):
        with as_user(ADMIN):
            before = payroll.structure(EMP, "2026-12")["ctc_annual"]
            rv = payroll.propose_revision(EMP, pct=10, effective_from="2026-12-01", reason="Appraisal")
            self.assertEqual(rv["new_ctc"], 1320000)
            self.assertEqual(payroll.structure(EMP, "2026-12")["ctc_annual"], before)  # nothing changes yet
        with as_user(auth.User(5, "hr2", "hr")):
            T.decide_approval(rv["approval_id"], True)
        self.assertEqual(payroll.structure(EMP, "2026-12")["ctc_annual"], 1320000)
        self.assertEqual(payroll.structure(EMP, "2026-11")["ctc_annual"], before)  # earlier months keep the old pay

    def test_second_structure_needs_a_revision(self):
        with as_user(ADMIN):
            with self.assertRaises(payroll.PayrollError):
                payroll.set_structure(EMP, 1500000)


class PayrollAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        finalise_month()

    def test_routing_and_answers(self):
        out = G.handle("What is the breakup for a 12 lakh CTC?", user=ADMIN)
        self.assertEqual(out["agent"], "payroll")
        self.assertIn("basic", out["result"].lower())
        out = G.handle("Which tax regime is better for 12 lakh?", user=ADMIN)
        self.assertIn("regime", out["result"])
        out = G.handle(f"Show the payroll summary for {MONTH}", user=ADMIN)
        self.assertIn("Payroll 2026-10", out["result"])

    def test_employee_sees_only_their_own_payslip(self):
        with as_user(auth.User(7, "ptester", "employee", EMP)):
            mine = T.run("my_payslip", month=MONTH)
            self.assertIn("PAYSLIP OCTOBER 2026", mine["text"])
            self.assertNotIn("ABCPT1234F", mine["text"])            # PAN is masked
            self.assertIn("error", T.run("my_payslip", month=MONTH, employee="E101"))
            self.assertIn("error", T.run("payroll_summary", month=MONTH))
            self.assertIn("error", T.run("run_payroll", month=MONTH))

    def test_reminder_trigger(self):
        r = triggers.fire("payroll_reminder")
        self.assertEqual(r["status"], "ok")


class PayrollWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from hrai import web
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        finalise_month()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, path, token, body=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_api_and_roles(self):
        hr = auth.login("hr2", "hr-password-2")
        code, body = self.call(f"/api/payroll?month={MONTH}", hr)
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIn(data["summary"]["status"], ("approved", "paid"))
        self.assertTrue(any(e["id"] == EMP for e in data["structures"]))
        self.assertEqual(self.call(f"/api/payroll/bank.csv?month={MONTH}", hr)[0], 200)
        code, body = self.call("/api/payroll/breakup", hr, {"ctc_annual": 600000})
        self.assertEqual(json.loads(body)["annual"]["basic"], 300000)
        employee = auth.login("ptester", "ptester-password")
        self.assertEqual(self.call(f"/api/payroll?month={MONTH}", employee)[0], 403)
        self.assertEqual(self.call(f"/api/payroll/bank.csv?month={MONTH}", employee)[0], 403)
        code, body = self.call(f"/api/payroll/mine?month={MONTH}", employee)
        self.assertEqual((code, "PAYSLIP" in body), (200, True))
        code, body = self.call(f"/api/payroll/payslip/E101?month={MONTH}", employee)
        self.assertEqual((code, "error" in body), (403, True))  # someone else's payslip is not theirs to read


if __name__ == "__main__":
    unittest.main()
