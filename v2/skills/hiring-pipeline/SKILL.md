---
name: hiring-pipeline
description: Running candidates from the resume inbox through L1..Ln, HR and Final rounds to offer, joining and follow-ups.
agents: recruitment
---
# Hiring pipeline

Stages: applied -> selected | on_hold | rejected -> interviewing (each round of the job, in order) -> offer ->
offer_accepted -> joined. Also offer_declined and withdrawn.

- New files: call `ingest_resumes`. It screens automatically: rejected = misses a must-have or the minimum years;
  selected = score at or above the job's threshold; on_hold = meets the minimum but scores lower (HR decides).
- Rounds come from the job (`set_interview_rounds` to change them, e.g. L1 L2 L3 HR Final). Schedule the next round
  with `schedule_interview`; it drafts the invite and a reminder.
- Results: `record_interview_result` with pass, fail or hold, a 1-5 rating and the panel's feedback in their words.
  Never record a result the user did not state.
- After the last round the candidate is at `offer`. `make_offer` needs CTC (lakh per annum) and joining date and
  goes to the approvals queue; the offer email is drafted only after approval.
- `record_offer_response` accepted=true creates the new hire (onboarding starts) and the follow-ups: pre-joining
  call (-7 days), documents check (-3), day one, 30-day check-in, 90-day probation review.
- Fairness: decisions use skills, experience and interview evidence only.
