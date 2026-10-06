---
name: onboarding
description: Onboarding a new hire end to end: documents, dated plan, and the welcome/reminder/IT drafts.
agents: onboarding
---
# Onboarding

1. Find the hire with `get_new_hire`, then `check_documents`.
2. Build the plan with `create_onboarding_plan`.
3. Drafts (all with `draft_email`, never sent):
   - Welcome email to the hire with start date, manager and first-day details.
   - If documents are missing: a reminder listing each missing document; mention payroll is blocked without
     bank details and PAN card. To cite the policy, ask the policy agent with `ask_agent("policy", ...)`.
   - IT/manager email to it-helpdesk@example.com and the manager listing their tasks with due dates.
4. Finish with a 3-5 line summary for HR: start date, blockers, drafts created.
