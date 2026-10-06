---
name: culture-events
description: Running cultural events, recognition and pulse surveys well: budgets and approvals, RSVPs, kudos and awards, anonymity.
agents: culture
---
# Culture, events and recognition

- **Events** have a kind (festival, town_hall, offsite, training, volunteering, celebration, sports, other), a day, an
  optional budget and an organiser. A budget past the limit (₹25,000 by default, `HRAI_EVENT_BUDGET_LIMIT`) goes to
  the approvals queue, and the event cannot be announced until it is approved.
- **Announcing drafts an email** to everyone and marks the event announced. The draft sits in the outbox until a
  person sends it; never say an invitation has gone out.
- **RSVPs** are yes, no or maybe, with guests. Headcount is the yes answers plus their guests. Employees answer only
  for themselves. People have not declined just because they have not answered: report responded against headcount.
- **Spend** is recorded against the budget. More than 10% over is refused: raise the budget so it goes through
  approval again.
- **Kudos** are public thank-yous between colleagues and cost nothing; they are not awards. Quote what someone
  actually did rather than writing a generic line.
- **Awards** are nominated by anyone and decided by HR (shortlisted, awarded, declined). An award drafts a
  congratulations email. Never announce a winner before HR has decided.
- **Pulse surveys** are one question on a 1 to N scale. Answers are anonymous: the person is stored only as a hash,
  results stay hidden until at least three people have answered, and a comment is never attributed to anyone.
- **Birthdays and work anniversaries** come from the employee record. Mention them to HR for planning; do not share
  someone's birth date itself.
