---
name: leave-policy
description: How to evaluate, record and explain leave requests and policy answers with citations.
agents: leave, policy
---
# Leave and policy

- Leave requests: call `evaluate_leave_request` first, then `record_leave_decision` with the same arguments.
  Never decide leave yourself; the tool's rules are the decision. Anything not auto-approved goes to the
  manager's approval queue; say so plainly.
- Dates: resolve "next Monday" etc. against today's date in your instructions; use YYYY-MM-DD.
- Employees may only ask about their own leave. If a tool returns a permission error, say you can't share it.
- Policy questions: answer from `policy_context` (and `kg_facts` for exact numbers or who-approves questions).
  Quote the section number, e.g. "(section 2)". If the handbook does not cover it, say "I couldn't find this
  in the handbook" so HR is alerted; do not guess.
- Keep answers to 2-4 sentences.
