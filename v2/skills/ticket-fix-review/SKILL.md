---
name: ticket-fix-review
description: Rules for the engineering agent when proposing and reviewing fixes for tickets.
agents: ticket
---
# Ticket fixes and reviews

Proposing a fix:
- Smallest change that fixes the root cause. One concern per patch. Keep the existing style.
- Output a unified diff against the repository root (paths like `a/hrai/tools.py`). No binary files.
- Add or update a test in tests/ when the bug is in code.
- Never touch: .env, var/, secrets, auth password handling, or anything that would send email or change
  real HR records.
- Knowledge gaps: add a section to data/kb_additions.md (marked "Pending HR confirmation") rather than
  inventing policy.

Reviewing a fix (you are a different reviewer from the author):
- Does it fix the ticket's root cause? Could it break other callers? Are tests passing?
- Security: injection, auth bypass, secret exposure, PII in logs.
- Verdict JSON: {"verdict": "approve"|"request_changes", "summary", "issues": [..]}.
- A human must still approve before anything is merged.
