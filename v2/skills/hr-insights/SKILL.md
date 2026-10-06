---
name: hr-insights
description: Reading HR numbers correctly (funnel, round pass rates, time to hire, headcount, leave, AI spend) and turning them into next actions.
agents: insights
---
# HR insights

- Get numbers from `hr_insights` (no section = snapshot; or hiring, workforce, leave, onboarding, ai, operations) and
  `needs_attention`. Never estimate a number the tools did not return.
- Funnel steps count candidates who *reached* the step: applied, passed screening, interviewed (a round was
  completed), offered (offer sent), accepted, joined.
- Pass rate per round = passed / decided (pass + fail + hold) in that round. Scheduled interviews are not decided.
- Time to offer / acceptance / joining are averages in days from the application date. With fewer than three
  candidates in a figure, say the sample is small.
- Offer acceptance = accepted / (accepted + declined). Offers still awaiting an answer are left out.
- "Missing must-have skills" lists the must-haves most often absent among screened-out resumes: a hint to
  widen sourcing or revisit the job's must-haves, not a judgement on candidates.
- Lead with what needs action and where: Hiring board, Approvals, Tickets or AI budget.
- Insights are for HR and admins only; do not share candidate details with other roles.
