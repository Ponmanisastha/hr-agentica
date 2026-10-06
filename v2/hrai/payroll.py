"""Salary management with Indian payroll rules: CTC breakup, PF, ESI, professional tax, TDS, payslips and payroll runs.

The rates live in data/payroll_rules.json so HR can update them each financial year without a code change.
Nothing is paid from here: a payroll run is computed as a draft, approved by a second person, and only then produces
payslips and a bank transfer file. HR marks it paid after uploading the file to the bank.

Simplifications (shown to users where they matter): previous-employer income is not included in TDS, ESI eligibility
is decided on the structure's gross, a revision applies from the start of its effective month, and loss-of-pay is
calendar-day based.
"""

import calendar
import csv
import io
import json
from datetime import date

from . import auth, config, db


class PayrollError(Exception):
    pass


def rules():
    return json.loads((config.DATA / "payroll_rules.json").read_text(encoding="utf-8"))


def _r(x):
    return int(round(x))


def _month(month):
    """'2026-10' (or a date) -> (year, month, first day, last day)."""
    m = str(month or config.today().isoformat())[:7]
    try:
        y, mo = int(m[:4]), int(m[5:7])
        first = date(y, mo, 1)
    except ValueError:
        raise PayrollError(f"Month must look like 2026-10, not {month!r}")
    return f"{y:04d}-{mo:02d}", first, date(y, mo, calendar.monthrange(y, mo)[1])


def fy_of(month):
    m, first, _ = _month(month)
    start = first.year if first.month >= 4 else first.year - 1
    return f"{start}-{str(start + 1)[2:]}", f"{start:04d}-04", f"{start + 1:04d}-03"


def months_left_in_fy(month):
    """Months from this one to March, inclusive (April -> 12, March -> 1)."""
    return (3 - _month(month)[1].month) % 12 + 1


def mask(value, keep=4):
    v = str(value or "")
    return ("•" * max(len(v) - keep, 0) + v[-keep:]) if v else ""


# ---------------------------------------------------------------- CTC breakup

def breakup(ctc_annual, metro=False, pf_capped=None):
    """Split an annual CTC into monthly and annual components the way most Indian employers do.

    Basic is a share of CTC (50% by default, the wage floor under the labour codes); HRA a share of basic; employer
    PF, employer ESI and gratuity are inside CTC; special allowance takes the rest.
    """
    R = rules()
    s, pf, esi = R["structure"], R["pf"], R["esi"]
    ctc = float(ctc_annual)
    if ctc <= 0:
        raise PayrollError("CTC must be more than zero")
    cap = pf["cap_at_ceiling"] if pf_capped is None else pf_capped
    basic = ctc * s["basic_pct_of_ctc"] / 100
    basic_m = basic / 12
    pf_wage_m = min(basic_m, pf["wage_ceiling_monthly"]) if cap else basic_m
    employer_pf = pf_wage_m * pf["rate_pct"] / 100 * 12
    gratuity = basic * s["gratuity_pct_of_basic"] / 100 if s["include_gratuity_in_ctc"] else 0
    gross = ctc - employer_pf - gratuity
    employer_esi = 0.0
    if gross / 12 <= esi["gross_ceiling_monthly"]:
        gross = gross / (1 + esi["employer_pct"] / 100)
        if gross / 12 <= esi["gross_ceiling_monthly"]:
            employer_esi = gross * esi["employer_pct"] / 100
        else:  # just over the ceiling once ESI is taken out: no ESI after all
            gross = ctc - employer_pf - gratuity
    hra = basic * (s["hra_pct_of_basic_metro"] if metro else s["hra_pct_of_basic_non_metro"]) / 100
    special = gross - basic - hra
    if special < 0:
        hra, special = max(gross - basic, 0), 0.0
    comp = {"basic": basic, "hra": hra, "special_allowance": special}
    monthly = {k: _r(v / 12) for k, v in comp.items()}
    gross_m = sum(monthly.values())
    emp_pf_m = _r(pf_wage_m * pf["rate_pct"] / 100)
    emp_esi_m = _r(gross_m * esi["employee_pct"] / 100) if employer_esi else 0
    return {
        "ctc_annual": _r(ctc), "metro": bool(metro), "pf_capped": bool(cap),
        "monthly": {**monthly, "gross": gross_m},
        "annual": {**{k: _r(v) for k, v in comp.items()}, "gross": _r(gross), "employer_pf": _r(employer_pf),
                   "employer_esi": _r(employer_esi), "gratuity": _r(gratuity)},
        "employee_deductions_monthly": {"pf": emp_pf_m, "esi": emp_esi_m},
        "esi_applicable": bool(employer_esi),
    }


# ---------------------------------------------------------------- tax

def professional_tax(gross_monthly, month, state=None):
    R = rules()["professional_tax"]
    table = R.get((state or R["default_state"]).upper()) or R["NONE"]
    if "half_yearly" in table:
        half = gross_monthly * 6
        due = max(t for start, t in table["half_yearly"] if half >= start)
        return _r(due / 6)
    due = max(t for start, t in table["monthly"] if gross_monthly >= start)
    if due and _month(month)[1].month == 2:
        due += table.get("february_extra", 0)
    return due


def _slab_tax(income, slabs):
    tax = 0.0
    for i, (start, pct) in enumerate(slabs):
        end = slabs[i + 1][0] if i + 1 < len(slabs) else float("inf")
        if income > start:
            tax += (min(income, end) - start) * pct / 100
    return tax


def income_tax(gross_annual, regime="new", deductions=None, employee_pf=0, professional_tax_annual=0, hra_exempt=0,
               pan=True):
    """Annual income tax on salary for one financial year, with standard deduction, 87A rebate, surcharge and cess.

    Old regime also takes 80C (including the employee's PF), 80D, HRA exemption and professional tax.
    Without a PAN, TDS is at least 20% of taxable income (section 206AA).
    """
    R = rules()["income_tax"]
    reg = R["old" if regime == "old" else "new"]
    taxable = gross_annual - reg["standard_deduction"]
    detail = {"regime": "old" if regime == "old" else "new", "gross": _r(gross_annual),
              "standard_deduction": reg["standard_deduction"]}
    if regime == "old":
        d = deductions or {}
        lim = reg["limits"]
        c80 = min(float(d.get("80C", 0)) + employee_pf, lim["80C"])
        d80 = min(float(d.get("80D", 0)), lim["80D_senior"] if d.get("senior_parents") else lim["80D"])
        taxable -= c80 + d80 + hra_exempt + professional_tax_annual
        detail.update({"80C": _r(c80), "80D": _r(d80), "hra_exempt": _r(hra_exempt), "professional_tax": _r(professional_tax_annual)})
    taxable = max(0.0, taxable)
    tax = _slab_tax(taxable, reg["slabs"])
    rb = reg["rebate_87a"]
    rebate = 0.0
    if taxable <= rb["income_up_to"]:
        rebate = min(tax, rb["max_rebate"])
    elif rb.get("marginal_relief"):
        over = taxable - rb["income_up_to"]
        if tax > over:
            rebate = tax - over
    tax -= rebate
    rate = 0
    for start, pct in reg["surcharge"]:
        if taxable > start:
            rate = pct
    surcharge = tax * rate / 100
    cess = (tax + surcharge) * R["cess_pct"] / 100
    total = tax + surcharge + cess
    if not pan:
        total = max(total, taxable * 0.20)
    detail.update({"taxable": _r(taxable), "tax_before_rebate": _r(tax + rebate), "rebate_87a": _r(rebate),
                   "surcharge": _r(surcharge), "cess": _r(cess), "total_tax": _r(total),
                   "effective_rate_pct": round(100 * total / gross_annual, 2) if gross_annual else 0})
    return detail


def hra_exemption(basic_annual, hra_annual, rent_annual, metro):
    if not rent_annual:
        return 0
    return max(0, min(hra_annual, rent_annual - 0.1 * basic_annual, basic_annual * (0.5 if metro else 0.4)))


def compare_regimes(ctc_annual, metro=False, deductions=None):
    """Tax and take-home under both regimes for a CTC, so an employee can choose."""
    b = breakup(ctc_annual, metro)
    d = deductions or {}
    out = {}
    for regime in ("new", "old"):
        pt = professional_tax(b["monthly"]["gross"], "2026-04", d.get("pt_state")) * 12
        hra = hra_exemption(b["annual"]["basic"], b["annual"]["hra"], float(d.get("rent_annual", 0)), metro)
        t = income_tax(b["annual"]["gross"], regime, d, b["employee_deductions_monthly"]["pf"] * 12, pt, hra)
        take_home = b["annual"]["gross"] - t["total_tax"] - pt - 12 * (b["employee_deductions_monthly"]["pf"] +
                                                                       b["employee_deductions_monthly"]["esi"])
        out[regime] = {"tax": t["total_tax"], "take_home_annual": _r(take_home), "take_home_monthly": _r(take_home / 12),
                       "detail": t}
    out["better"] = "new" if out["new"]["tax"] <= out["old"]["tax"] else "old"
    return out


# ---------------------------------------------------------------- salary structures and revisions

def _emp(employee_id):
    e = db.q1("SELECT * FROM employees WHERE id=? OR lower(name)=lower(?)", (employee_id, employee_id))
    if not e:
        e = db.q1("SELECT * FROM employees WHERE lower(name) LIKE lower(?)", (f"%{employee_id}%",))
    if not e:
        raise PayrollError(f"No employee {employee_id!r}")
    return e


def structure(employee_id, month=None):
    """The salary structure in force for an employee in a month (latest one effective by the month's last day)."""
    _, _, last = _month(month)
    s = db.q1("SELECT * FROM salary_structures WHERE employee_id=? AND effective_from<=? AND status='active' "
              "ORDER BY effective_from DESC, id DESC LIMIT 1", (employee_id, last.isoformat()))
    if s:
        s["declarations"] = json.loads(s["declarations"] or "{}")
    return s


def set_structure(employee_id, ctc_annual, effective_from=None, metro=False, regime="new", pt_state="", pan="", uan="",
                  bank_account="", ifsc="", declarations=None, _approved=False):
    """First salary set-up for an employee. Later changes go through propose_revision (they need approval)."""
    e = _emp(employee_id)
    if structure(e["id"], "9999-12") and not _approved:
        raise PayrollError(f"{e['name']} already has a salary; propose a revision instead (it needs approval)")
    if regime not in ("new", "old"):
        raise PayrollError("regime must be new or old")
    eff = effective_from or config.today().replace(day=1).isoformat()
    date.fromisoformat(eff)
    b = breakup(ctc_annual, metro)
    old = structure(e["id"], "9999-12")
    keep = lambda k, v: v or (old or {}).get(k) or ""  # noqa: E731  carry identifiers over on a revision
    sid = db.x("INSERT INTO salary_structures (employee_id, ctc_annual, effective_from, metro, regime, pt_state, pan, uan, "
               "bank_account, ifsc, declarations, breakup, status, created_by, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (e["id"], b["ctc_annual"], eff, int(bool(metro)), regime, keep("pt_state", pt_state).upper(),
                keep("pan", pan).upper(), keep("uan", uan), keep("bank_account", bank_account), keep("ifsc", ifsc).upper(),
                json.dumps(declarations or (old or {}).get("declarations") or {}), json.dumps(b), "active",
                _actor(), db.now()))
    db.audit(_actor(), "salary.set", {"employee": e["id"], "ctc": b["ctc_annual"], "effective_from": eff})
    return {"structure_id": sid, "employee_id": e["id"], "name": e["name"], "effective_from": eff, "breakup": b,
            "missing": [k for k in ("pan", "bank_account", "ifsc") if not keep(k, locals().get(k))]}


def update_details(employee_id, **fields):
    """PAN, UAN, bank account, IFSC, PT state, regime or tax declarations on the current structure (no pay change)."""
    e = _emp(employee_id)
    s = structure(e["id"], "9999-12")
    if not s:
        raise PayrollError(f"{e['name']} has no salary set up yet")
    allowed = {"pan", "uan", "bank_account", "ifsc", "pt_state", "regime", "declarations", "metro"}
    sets = {k: v for k, v in fields.items() if k in allowed and v not in (None, "")}
    if "regime" in sets and sets["regime"] not in ("new", "old"):
        raise PayrollError("regime must be new or old")
    if "declarations" in sets:
        sets["declarations"] = json.dumps(sets["declarations"])
    for k in ("pan", "ifsc", "pt_state"):
        if k in sets:
            sets[k] = str(sets[k]).upper()
    if sets:
        db.x(f"UPDATE salary_structures SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), s["id"]))
        db.audit(_actor(), "salary.details", {"employee": e["id"], "fields": sorted(sets)})
    return {"employee_id": e["id"], "updated": sorted(sets)}


def propose_revision(employee_id, new_ctc_annual=None, pct=None, effective_from=None, reason=""):
    e = _emp(employee_id)
    cur = structure(e["id"], "9999-12")
    if not cur:
        raise PayrollError(f"{e['name']} has no salary yet; set one up first")
    if new_ctc_annual is None and pct is None:
        raise PayrollError("Give the new CTC or a percentage")
    new = float(new_ctc_annual) if new_ctc_annual else cur["ctc_annual"] * (1 + float(pct) / 100)
    eff = effective_from or (config.today().replace(day=1) if config.today().day == 1 else
                             date(config.today().year + (config.today().month == 12), config.today().month % 12 + 1, 1)).isoformat()
    date.fromisoformat(eff)
    change = round(100 * (new - cur["ctc_annual"]) / cur["ctc_annual"], 1)
    rid = db.x("INSERT INTO salary_revisions (employee_id, old_ctc, new_ctc, pct, effective_from, reason, status, "
               "requested_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (e["id"], cur["ctc_annual"], _r(new), change, eff, reason, "pending_approval", _actor(), db.now()))
    aid = db.x("INSERT INTO approvals (kind, ref, summary, payload, requested_by, created_at) VALUES (?,?,?,?,?,?)",
               ("salary_revision", str(rid), f"Salary revision for {e['name']}: ₹{cur['ctc_annual']:,} to ₹{_r(new):,} "
                f"({change:+}%) from {eff}" + (f". {reason}" if reason else ""), json.dumps({"revision_id": rid}),
                _actor(), db.now()))
    db.x("UPDATE salary_revisions SET approval_id=? WHERE id=?", (aid, rid))
    return {"revision_id": rid, "approval_id": aid, "old_ctc": cur["ctc_annual"], "new_ctc": _r(new), "pct": change,
            "effective_from": eff, "status": "pending_approval"}


def on_revision_decision(revision_id, approved):
    rv = db.q1("SELECT * FROM salary_revisions WHERE id=?", (revision_id,))
    if not approved:
        db.x("UPDATE salary_revisions SET status='rejected' WHERE id=?", (revision_id,))
        return
    cur = structure(rv["employee_id"], "9999-12")
    set_structure(rv["employee_id"], rv["new_ctc"], rv["effective_from"], bool(cur["metro"]), cur["regime"], _approved=True)
    db.x("UPDATE salary_revisions SET status='approved' WHERE id=?", (revision_id,))
    e = db.q1("SELECT name, email FROM employees WHERE id=?", (rv["employee_id"],))
    _draft(e["email"], "Your salary revision",
           f"Dear {e['name']},\n\nYour annual CTC is revised to ₹{rv['new_ctc']:,} from {rv['effective_from']}."
           f"\n\nRegards,\nHR")


def add_adjustment(employee_id, month, kind, amount, note=""):
    """One-off pay items for a month: bonus, incentive, reimbursement, arrears (earnings); recovery (deduction);
    lop_days (unpaid days)."""
    kinds = {"bonus", "incentive", "reimbursement", "arrears", "recovery", "lop_days"}
    if kind not in kinds:
        raise PayrollError(f"kind must be one of {', '.join(sorted(kinds))}")
    e = _emp(employee_id)
    m, _, last = _month(month)
    if float(amount) < 0 or (kind == "lop_days" and float(amount) > last.day):
        raise PayrollError("amount out of range")
    _editable(m)
    aid = db.x("INSERT INTO pay_adjustments (employee_id, month, kind, amount, note, created_by, created_at) "
               "VALUES (?,?,?,?,?,?,?)", (e["id"], m, kind, float(amount), note, _actor(), db.now()))
    return {"adjustment_id": aid, "employee_id": e["id"], "month": m, "kind": kind, "amount": float(amount)}


# ---------------------------------------------------------------- payslips and runs

def _editable(month):
    run = db.q1("SELECT status FROM payroll_runs WHERE month=?", (month,))
    if run and run["status"] != "draft":
        raise PayrollError(f"Payroll for {month} is {run['status']}; it can no longer change")


def compute_payslip(employee_id, month):
    """One employee's payslip for a month, from the structure in force, joining date, LOP and adjustments."""
    m, first, last = _month(month)
    s = structure(employee_id, m)
    if not s:
        return None
    R = rules()
    b = json.loads(s["breakup"])
    days = last.day
    joined = db.q1("SELECT start_date FROM new_hires WHERE employee_id=?", (employee_id,))
    start = date.fromisoformat(joined["start_date"]) if joined and joined["start_date"] else None
    before = (start - first).days if start and first < start <= last else 0
    if start and start > last:
        return None
    adj = db.q("SELECT kind, amount, note FROM pay_adjustments WHERE employee_id=? AND month=?", (employee_id, m))
    lop = min(sum(a["amount"] for a in adj if a["kind"] == "lop_days"), days - before)
    paid_days = days - before - lop
    f = paid_days / days
    earn = {k: _r(b["monthly"][k] * f) for k in ("basic", "hra", "special_allowance")}
    for a in adj:
        if a["kind"] in ("bonus", "incentive", "reimbursement", "arrears"):
            earn[a["kind"]] = earn.get(a["kind"], 0) + _r(a["amount"])
    taxable_extra = sum(v for k, v in earn.items() if k in ("bonus", "incentive", "arrears"))
    gross = sum(earn.values())
    pfr = R["pf"]
    pf_wage = min(earn["basic"], pfr["wage_ceiling_monthly"]) if b["pf_capped"] else earn["basic"]
    pf = _r(pf_wage * pfr["rate_pct"] / 100)
    esi = _r((gross - earn.get("reimbursement", 0)) * R["esi"]["employee_pct"] / 100) if b["esi_applicable"] else 0
    pt = professional_tax(gross - earn.get("reimbursement", 0), m, s["pt_state"]) if paid_days else 0
    tds = monthly_tds(employee_id, m, s, b, gross - earn.get("reimbursement", 0), pf, pt, taxable_extra)
    ded = {"pf": pf, "esi": esi, "professional_tax": pt, "tds": tds}
    for a in adj:
        if a["kind"] == "recovery":
            ded["recovery"] = ded.get("recovery", 0) + _r(a["amount"])
    total_ded = sum(ded.values())
    notes = [a["note"] for a in adj if a["note"]] + ([] if s["pan"] else ["No PAN on file: TDS at 20% minimum"]) + _tds_note
    return {"employee_id": employee_id, "month": m, "days_in_month": days, "paid_days": _r(paid_days), "lop_days": _r(lop),
            "earnings": earn, "gross": _r(gross), "deductions": ded, "total_deductions": _r(total_ded),
            "net": _r(gross - total_ded),
            "employer": {"pf": _r(pf_wage * pfr["rate_pct"] / 100),
                         "esi": _r((gross - earn.get("reimbursement", 0)) * R["esi"]["employer_pct"] / 100) if b["esi_applicable"] else 0},
            "regime": s["regime"], "ctc_annual": s["ctc_annual"],
            "notes": notes}


def monthly_tds(employee_id, month, s, b, gross_now, pf_now, pt_now, extra_now=0):
    """TDS this month = (projected tax for the financial year - TDS already deducted this year) / months left."""
    m, first, _ = _month(month)
    _, fy_start, _ = fy_of(m)
    prev = db.q("SELECT p.gross, p.deductions, p.earnings FROM payslips p JOIN payroll_runs r ON r.id=p.run_id "
                "WHERE p.employee_id=? AND p.month>=? AND p.month<? AND r.status IN ('approved','paid')",
                (employee_id, fy_start, m))
    paid_gross = sum(p["gross"] - json.loads(p["earnings"]).get("reimbursement", 0) for p in prev)
    paid_tds = sum(json.loads(p["deductions"]).get("tds", 0) for p in prev)
    paid_pf = sum(json.loads(p["deductions"]).get("pf", 0) for p in prev)
    paid_pt = sum(json.loads(p["deductions"]).get("professional_tax", 0) for p in prev)
    left = months_left_in_fy(m) - 1
    proj_gross = paid_gross + gross_now + left * b["monthly"]["gross"]
    proj_pf = paid_pf + pf_now + left * b["employee_deductions_monthly"]["pf"]
    proj_pt = paid_pt + pt_now + left * professional_tax(b["monthly"]["gross"], m, s["pt_state"])
    d = s["declarations"] if isinstance(s["declarations"], dict) else json.loads(s["declarations"] or "{}")
    hra = hra_exemption(b["annual"]["basic"], b["annual"]["hra"], float(d.get("rent_annual", 0)), bool(s["metro"]))
    tax = income_tax(proj_gross, s["regime"], d, proj_pf, proj_pt, hra, pan=bool(s["pan"]))["total_tax"]
    _tds_note[:] = [] if prev or fy_start == m else [
        f"TDS is projected from {_label(m)} onward; earlier months of {fy_of(m)[0]} were not processed here, so a "
        f"shortfall may show up in the employee's own return."]
    return max(0, _r((tax - paid_tds) / (left + 1)))


_tds_note = []


def run_payroll(month):
    """Compute (or recompute) the draft payroll for a month for everyone with a salary. Returns totals and warnings."""
    m, _, _ = _month(month)
    _editable(m)
    run = db.q1("SELECT * FROM payroll_runs WHERE month=?", (m,))
    rid = run["id"] if run else db.x("INSERT INTO payroll_runs (month, status, created_by, created_at) VALUES (?,?,?,?)",
                                     (m, "draft", _actor(), db.now()))
    db.x("DELETE FROM payslips WHERE run_id=?", (rid,))
    warnings, count = [], 0
    for e in db.q("SELECT id, name FROM employees ORDER BY id"):
        p = compute_payslip(e["id"], m)
        if not p:
            continue
        s = structure(e["id"], m)
        missing = [k.replace("_", " ") for k in ("pan", "bank_account", "ifsc") if not s[k]]
        if missing:
            warnings.append(f"{e['name']}: missing {', '.join(missing)}")
        db.x("INSERT INTO payslips (run_id, employee_id, month, gross, total_deductions, net, earnings, deductions, "
             "employer, paid_days, lop_days, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
             (rid, e["id"], m, p["gross"], p["total_deductions"], p["net"], json.dumps(p["earnings"]),
              json.dumps(p["deductions"]), json.dumps(p["employer"]), p["paid_days"], p["lop_days"], json.dumps(p["notes"])))
        count += 1
    db.x("UPDATE payroll_runs SET updated_at=?, warnings=? WHERE id=?", (db.now(), json.dumps(warnings), rid))
    db.audit(_actor(), "payroll.computed", {"month": m, "employees": count})
    return summary(m)


def summary(month):
    m, _, _ = _month(month)
    run = db.q1("SELECT * FROM payroll_runs WHERE month=?", (m,))
    if not run:
        return {"month": m, "status": "not_started", "employees": 0}
    slips = db.q("SELECT p.*, e.name, e.department FROM payslips p JOIN employees e ON e.id=p.employee_id "
                 "WHERE run_id=? ORDER BY p.employee_id", (run["id"],))
    tot = lambda key, part=None: _r(sum(json.loads(s[part]).get(key, 0) if part else s[key] for s in slips))  # noqa: E731
    return {"month": m, "status": run["status"], "run_id": run["id"], "employees": len(slips),
            "gross": tot("gross"), "deductions": tot("total_deductions"), "net": tot("net"),
            "employer_pf": tot("pf", "employer"), "employer_esi": tot("esi", "employer"),
            "employer_cost": tot("gross") + tot("pf", "employer") + tot("esi", "employer"),
            "statutory": {"pf_employee": tot("pf", "deductions"), "pf_employer": tot("pf", "employer"),
                          "esi_employee": tot("esi", "deductions"), "esi_employer": tot("esi", "employer"),
                          "professional_tax": tot("professional_tax", "deductions"), "tds": tot("tds", "deductions")},
            "warnings": json.loads(run["warnings"] or "[]"), "approval_id": run["approval_id"],
            "approved_by": run["approved_by"], "paid_reference": run["paid_reference"],
            "payslips": [{"employee_id": s["employee_id"], "name": s["name"], "department": s["department"],
                          "gross": _r(s["gross"]), "deductions": _r(s["total_deductions"]), "net": _r(s["net"]),
                          "paid_days": _r(s["paid_days"]), "lop_days": _r(s["lop_days"]),
                          "tds": json.loads(s["deductions"]).get("tds", 0)} for s in slips]}


def submit(month):
    """Send the draft run for approval. Someone other than the submitter must approve it."""
    m, _, _ = _month(month)
    run = db.q1("SELECT * FROM payroll_runs WHERE month=?", (m,))
    if not run or run["status"] != "draft":
        raise PayrollError(f"There is no draft payroll for {m}; run it first")
    s = summary(m)
    if not s["employees"]:
        raise PayrollError(f"The {m} payroll has no payslips")
    aid = db.x("INSERT INTO approvals (kind, ref, summary, payload, requested_by, created_at) VALUES (?,?,?,?,?,?)",
               ("payroll", m, f"Payroll {m}: {s['employees']} employees, net ₹{s['net']:,}, employer cost "
                f"₹{s['employer_cost']:,}" + (f". Warnings: {'; '.join(s['warnings'])}" if s["warnings"] else ""),
                json.dumps({"month": m}), _actor(), db.now()))
    db.x("UPDATE payroll_runs SET status='pending_approval', approval_id=?, submitted_by=?, updated_at=? WHERE id=?",
         (aid, _actor(), db.now(), run["id"]))
    return {"month": m, "status": "pending_approval", "approval_id": aid}


def on_run_decision(month, approved, decided_by):
    run = db.q1("SELECT * FROM payroll_runs WHERE month=?", (month,))
    if not approved:
        db.x("UPDATE payroll_runs SET status='draft', approval_id=NULL, updated_at=? WHERE id=?", (db.now(), run["id"]))
        return
    db.x("UPDATE payroll_runs SET status='approved', approved_by=?, updated_at=? WHERE id=?",
         (decided_by, db.now(), run["id"]))
    folder = config.home() / "payroll" / month
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bank_transfer.csv").write_text(bank_file(month), encoding="utf-8")
    for p in db.q("SELECT p.*, e.name, e.email FROM payslips p JOIN employees e ON e.id=p.employee_id WHERE run_id=?",
                  (run["id"],)):
        (folder / f"payslip-{p['employee_id']}.txt").write_text(render_payslip(p["employee_id"], month), encoding="utf-8")
        _draft(p["email"], f"Your payslip for {_label(month)}",
               f"Dear {p['name']},\n\nYour salary for {_label(month)} has been processed. Net pay: ₹{p['net']:,}. "
               f"You can view the payslip in the HR console under My pay.\n\nRegards,\nPayroll")


def mark_paid(month, reference):
    m, _, _ = _month(month)
    run = db.q1("SELECT * FROM payroll_runs WHERE month=?", (m,))
    if not run or run["status"] != "approved":
        raise PayrollError(f"The {m} payroll must be approved before it is marked paid")
    if not reference:
        raise PayrollError("Give the bank reference for the transfer")
    db.x("UPDATE payroll_runs SET status='paid', paid_reference=?, paid_at=?, updated_at=? WHERE id=?",
         (reference, db.now(), db.now(), run["id"]))
    db.audit(_actor(), "payroll.paid", {"month": m, "reference": reference})
    return {"month": m, "status": "paid", "reference": reference}


def bank_file(month):
    m, _, _ = _month(month)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["employee_id", "name", "bank_account", "ifsc", "amount", "narration"])
    for p in db.q("SELECT p.employee_id, p.net, e.name FROM payslips p JOIN payroll_runs r ON r.id=p.run_id "
                  "JOIN employees e ON e.id=p.employee_id WHERE r.month=? ORDER BY p.employee_id", (m,)):
        s = structure(p["employee_id"], m)
        w.writerow([p["employee_id"], p["name"], s["bank_account"] or "MISSING", s["ifsc"] or "MISSING", _r(p["net"]),
                    f"Salary {_label(m)}"])
    return out.getvalue()


def payslip(employee_id, month):
    m, _, _ = _month(month)
    p = db.q1("SELECT p.*, r.status AS run_status FROM payslips p JOIN payroll_runs r ON r.id=p.run_id "
              "WHERE p.employee_id=? AND p.month=?", (employee_id, m))
    if not p:
        return None
    e = db.q1("SELECT id, name, department, level FROM employees WHERE id=?", (employee_id,))
    s = structure(employee_id, m)
    for k in ("earnings", "deductions", "employer", "notes"):
        p[k] = json.loads(p[k] or "{}")
    for k in ("gross", "total_deductions", "net", "paid_days", "lop_days"):
        p[k] = _r(p[k])
    p["days_in_month"] = _month(m)[2].day
    p.update({"name": e["name"], "department": e["department"], "level": e["level"], "pan": mask(s["pan"]),
              "uan": mask(s["uan"]), "bank_account": mask(s["bank_account"]), "regime": s["regime"],
              "label": _label(m), "final": p["run_status"] in ("approved", "paid")})
    return p


def render_payslip(employee_id, month):
    p = payslip(employee_id, month)
    if not p:
        return ""
    lines = [f"PAYSLIP {p['label'].upper()}" + ("" if p["final"] else "  (DRAFT, not yet approved)"),
             f"{p['name']} ({employee_id}), {p['department']}, {p['level']}",
             f"PAN {p['pan'] or '-'}  UAN {p['uan'] or '-'}  Bank {p['bank_account'] or '-'}  Tax regime: {p['regime']}",
             f"Paid days {p['paid_days']} of {p['days_in_month']}  LOP {p['lop_days']}", ""]
    lines.append("EARNINGS")
    labels = {"hra": "HRA"}
    lines += [f"  {labels.get(k, k.replace('_', ' ').capitalize()):28}{v:>12,}" for k, v in p["earnings"].items() if v]
    lines.append(f"  {'Gross':28}{p['gross']:>12,}")
    lines.append("DEDUCTIONS")
    names = {"pf": "Provident fund", "esi": "ESI", "professional_tax": "Professional tax", "tds": "Income tax (TDS)"}
    lines += [f"  {names.get(k, k.replace('_', ' ').capitalize()):28}{v:>12,}" for k, v in p["deductions"].items() if v]
    lines.append(f"  {'Total deductions':28}{p['total_deductions']:>12,}")
    lines += ["", f"  {'NET PAY':28}{p['net']:>12,}", "",
              f"Employer contributions (not in net pay): PF {p['employer'].get('pf', 0):,}, ESI {p['employer'].get('esi', 0):,}"]
    lines += [f"Note: {n}" for n in p["notes"]]
    return "\n".join(lines)


def history(months=6):
    """Payroll cost per month for the last runs (for charts)."""
    rows = db.q("SELECT month FROM payroll_runs ORDER BY month DESC LIMIT ?", (months,))
    return [{k: summary(r["month"])[k] for k in ("month", "status", "employees", "gross", "net", "employer_cost")}
            for r in reversed(rows)]


def structures():
    out = []
    for e in db.q("SELECT id, name, department, level FROM employees ORDER BY id"):
        s = structure(e["id"], "9999-12")
        pend = db.q1("SELECT new_ctc, pct, effective_from FROM salary_revisions WHERE employee_id=? AND "
                     "status='pending_approval' ORDER BY id DESC LIMIT 1", (e["id"],))
        out.append({**e, "ctc_annual": s["ctc_annual"] if s else None, "monthly_gross": json.loads(s["breakup"])["monthly"]["gross"] if s else None,
                    "regime": s["regime"] if s else None, "effective_from": s["effective_from"] if s else None,
                    "missing": [k.replace("_", " ") for k in ("pan", "bank_account", "ifsc") if s and not s[k]],
                    "pending_revision": pend})
    return out


# ---------------------------------------------------------------- helpers

def _label(month):
    m, first, _ = _month(month)
    return first.strftime("%B %Y")


def _actor():
    try:
        return auth.current_user().username
    except Exception:
        return "system"


def _draft(to, subject, body):
    from . import tools as T
    if to:
        T.run("draft_email", to=to, subject=subject, body=body)


def seed_sample_salaries():
    """Sample salaries for the demo employees (only when none exist)."""
    if db.q1("SELECT 1 AS y FROM salary_structures LIMIT 1"):
        return
    samples = {"E101": (1200000, True, "new", "ABCPN1234D", "100200300400", "50100012345678", "HDFC0001234"),
               "E102": (1800000, True, "old", "BCDPP2345E", "100200300401", "50100023456789", "ICIC0002345"),
               "E103": (2400000, True, "new", "", "100200300402", "", "")}
    for eid, (ctc, metro, regime, pan, uan, acct, ifsc) in samples.items():
        if db.q1("SELECT 1 AS y FROM employees WHERE id=?", (eid,)):
            set_structure(eid, ctc, "2026-04-01", metro, regime, "TN", pan, uan, acct, ifsc,
                          {"80C": 100000, "80D": 15000, "rent_annual": 240000} if regime == "old" else {})
