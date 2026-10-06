---
name: payroll-india
description: Running Indian salary and payroll correctly: CTC breakup, PF, ESI, professional tax, TDS under both regimes, approvals and payslips.
agents: payroll
---
# Indian payroll

Rates and slabs live in `data/payroll_rules.json` (financial year April to March). Check them with a CA each year and
edit that file; never hard-code a rate in an answer.

- **CTC breakup:** basic is 50% of CTC, HRA 50% of basic in a metro and 40% elsewhere, special allowance takes the
  rest. Employer PF, employer ESI and gratuity sit inside CTC, so gross is CTC minus those.
- **PF:** 12% of basic, capped at the ₹15,000 monthly wage ceiling by default, from both employee and employer.
- **ESI:** only while monthly gross is at or under ₹21,000: 0.75% employee, 3.25% employer.
- **Professional tax:** by state. Tamil Nadu is half-yearly (spread over six months); Karnataka and Maharashtra are
  monthly with an extra amount in February.
- **TDS:** project the full year's income, work out the annual tax, subtract what has already been deducted this
  financial year, and spread the rest over the months left. New regime by default; the old regime takes 80C (the
  employee's PF counts), 80D, HRA exemption and professional tax. Without a PAN, TDS is at least 20%.
- **Comparing regimes** needs the employee's rent and declared investments; without them the old regime looks worse
  than it is, so say what was assumed.

## Rules of the house

- A salary revision needs approval before it takes effect, and a payroll run needs approval from someone other than
  whoever submitted it. Say what is waiting; never claim pay is done when approval is pending.
- Nothing is paid from here. Approval produces payslips and `var/payroll/<month>/bank_transfer.csv`; HR uploads that
  to the bank and then records the reference with `mark_payroll_paid`.
- An approved or paid month cannot be edited. Fix it in the next month with an arrears or recovery adjustment.
- Payslips are private: employees and managers see only their own. Never put a full PAN, UAN or bank account in a
  message; the tools already mask them.
- A missing PAN, bank account or IFSC is a warning on the run, not a reason to skip the person.
