---
name: resume-screening
description: Fair, structured shortlisting against a job's must-haves; use before any shortlist decision.
agents: screening
---
# Resume screening

1. Score every candidate for the job with `score_candidate`. Do not skip anyone.
2. Shortlist only candidates who meet the minimum: every must-have skill and at least the job's minimum years.
3. Rank by score; keep at most the job's `shortlist_size`. Ties go to more matched nice-to-haves.
4. Borderline (within 5 points of the cut, or missing one must-have but strong otherwise): read the resume and
   say why in the decision reason. Never shortlist someone who misses a must-have; flag them for HR instead.
5. Fairness: never use or mention name, gender, age, religion, caste, marital status, photos or nationality.
   Judge skills and experience only. If a reason mentions a protected trait, rewrite it.
6. Save with `save_shortlist`, then draft one email per candidate with `draft_email`: an invitation to a
   45-minute technical interview for shortlisted people, a short respectful decline for the others.
