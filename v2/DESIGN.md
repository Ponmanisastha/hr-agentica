# HR agentic AI v2: design

v2 rebuilds the v1 prototype on standard agent tooling. It keeps v1's three HR jobs (resume screening, onboarding, leave and policy questions) and adds a login system, a database, a vector store, an AI gateway with budgets, MCP and A2A, triggers, hooks, skills, and an engineering agent that turns every error and complaint into a ticket and works it through to a reviewed, approved fix.

Everything runs locally on WSL with Python 3.14. A Claude API key is optional: without one, the app uses a free Ollama model if one is running, and otherwise a rules-only mode that calls the same tools in a fixed order.

## 1. What changed from v1

| Area | v1 | v2 |
| --- | --- | --- |
| Agent framework | Hand-written tool loop and keyword router | **LangGraph** runs the main flow and the ticket workflow. **LangChain** tools are shared by every agent. The screening job is a three-role **crew** (CrewAI on Python 3.13, a LangGraph crew on 3.14). |
| Models | Claude Opus 5.5 for everything, or the offline mock | **Claude Sonnet 5.5** for multi-step work, **Claude Haiku 4.5** for routing and simple steps, and a free **Ollama** model (llama3.1:8b) as fallback. The offline rules-only mode is kept. |
| AI gateway | None | **LiteLLM** gateway. It handles model tiers, fallback chains, per-agent and total monthly budgets, cost and token logging, prompt caching, and an optional LiteLLM proxy. |
| Agent gateway | None | One front door for every channel. It handles authentication, rate limits, hooks, role checks, auditing, and turning errors into tickets. |
| RAG / vector DB | Keyword counting over the handbook | **Chroma** stored locally, with local MiniLM embeddings and hybrid re-ranking. It indexes the handbook, the candidate FAQ, resumes and user memories. |
| MAG / KAG / CAG | None | Each one is used where it helps (section 4). |
| Database | JSON files | **SQLite**, holding HR records, users, sessions, approvals, outbox, LLM usage, budgets, the audit log, memories, the knowledge graph, the answer cache, tickets and trigger runs. |
| Authentication | None | **bcrypt** password hashes, 8-hour session tokens stored as SHA-256 hashes, lockout after 5 failed logins, service tokens, and roles (admin, hr, manager, employee, service). |
| Tools, skills, hooks, triggers | Tools only | All four (section 5). |
| MCP | None | MCP server with the HR tools, the handbook and FAQ as resources, and skills as prompts. It uses role-filtered tools and works over stdio or HTTP. |
| A2A | None | Every agent has an A2A agent card and a JSON-RPC endpoint. Agents call each other through A2A, in-process or over HTTP. |
| Error and feedback tracking | None | Ticket tracker plus the engineering agent (section 7). |
| Human approval | Emails were drafts | Emails are still drafts. Leave exceptions go to a manager approval queue, and code or knowledge fixes wait for admin approval. |

## 2. Architecture

```
 Channels:  CLI · Web console (login) · A2A (JSON-RPC) · MCP (stdio/HTTP) · Triggers (schedule/event)
                                   │
                                   ▼
 ┌──────────────────────── Agent gateway (hrai/gateway/agent_gateway.py) ────────────────────────┐
 │ authenticate → rate limit → pre_request hooks (injection guard, PII redaction, size limit)     │
 │ → role check → LangGraph orchestrator → post_response hooks → audit log;  crash → on_error →   │
 │ ticket                                                                                         │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘
                                   │
 LangGraph orchestrator (hrai/agents/graph.py)
   START → recall_memory (MAG) → route (Haiku / keywords) → policy | leave | onboarding | screening → remember → END
                                                               │          │         │            │
                                       CAG+KAG+RAG ────────────┘   KAG rules│   A2A ask_agent   crew: sourcer →
                                                                            │   to policy        fairness reviewer →
                                                                            │                    coordinator
                                   │ every model call
                                   ▼
 AI gateway (hrai/gateway/llm.py, LiteLLM): tier → chain (Sonnet → Haiku → Ollama) → budget check → call → log cost
                                   │ every tool call
                                   ▼
 Tools (hrai/tools.py, LangChain): role check → pre_tool hooks (audit) → tool → post_tool hooks; failure → ticket
                                   │
 Storage: SQLite (var/hrai.db) · Chroma (var/chroma) · reports (var/reports) · ticket checkpoints (var/ticket_graph.db)

 Ops: tracker (errors, ERROR logs, feedback, knowledge gaps → tickets) → ticket agent (LangGraph, durable) →
      git worktree + tests → AI review → PR on GitHub → human approval → merge → ticket closed with resolution
```

## 3. Models and cost

| Tier | Default model | Used for | Price per million tokens (input / output) |
| --- | --- | --- | --- |
| smart | `anthropic/claude-sonnet-5-5` | Onboarding, screening crew, code fixes, code review | $2 / $10 |
| fast | `anthropic/claude-haiku-4-5-20251001` | Routing, policy and leave answers, triage, memory extraction | $1 / $5 |
| local | `ollama_chat/llama3.1:8b` | Fallback when Claude is down, when there is no key, or when a budget is spent | free |

Prices are LiteLLM's built-in price table, which the gateway uses to compute cost.

- Each tier falls back down its chain on any error: smart → fast → local.
- **Budgets** are in USD per agent per month, plus a total. Defaults are router $1, policy $5, leave $5, onboarding $5, screening $10, memory $1, ticket $15, and $40 total. They can be changed with `app.py budget set` or `HRAI_BUDGET_<AGENT>`.
- When a budget is spent, the gateway either **downgrades** to the free local model (the default) or **blocks** the call. In both cases the agent finishes in rules-only mode instead of failing.
- The `budget_watch` trigger drafts a warning to the admin when an agent passes 80% of its budget.
- Every call is logged in `llm_usage` with agent, user, model, tokens, cached tokens, cost, latency and errors. The web console's Budget tab shows the totals.
- The model names are settings, so any LiteLLM model (OpenAI, Gemini, Groq, a different Ollama model) can be dropped in.

## 4. MAG, KAG, CAG and RAG: where each one is used

| Technique | Where | Why it helps here |
| --- | --- | --- |
| **CAG** (cache-augmented generation) | Policy agent | The handbook is small and stable, so the whole handbook is preloaded into the system prompt instead of retrieving chunks. That means no retrieval misses, and the gateway marks the prompt for Anthropic prompt caching, so repeat calls read it at about a tenth of the input price. Generic questions ("how many sick days do we get?") are also answered from an SQLite answer cache keyed on the handbook's hash, so a handbook change invalidates it automatically. Personal questions are never cached. Above `HRAI_CAG_MAX_CHARS` (40k characters) the agent switches to RAG automatically. |
| **KAG** (knowledge-augmented generation, with a knowledge graph) | Leave rules, "who approves" and reporting questions, job skills | Exact facts beat similar paragraphs. The graph (`kg_triples`) holds reporting lines, approvers, departments, job skills and leave thresholds extracted from the handbook. **The leave rules engine reads its thresholds from the graph**, so the decision and the explanation come from one source. |
| **RAG** (vector search) | Resume search ("payments APIs on AWS"), candidate FAQ, the handbook once it outgrows CAG | Semantic matching over unstructured text. It uses Chroma with local MiniLM embeddings and is re-ranked with keyword overlap (hybrid search). |
| **MAG** (memory-augmented generation) | Every agent, per user | Follow-ups and preferences ("and my sick leave?", "I prefer email"). Short-term memory is the LangGraph conversation thread. Long-term memory is per-user rows in SQLite and Chroma, recalled before each request. The fast model extracts durable facts when available. |

MAG is read here as memory-augmented generation. If you meant multimodal generation (reading scanned ID proofs or offer letters), that fits onboarding document checks and can be added as a later phase.

## 5. Tools, skills, hooks and triggers

- **Tools** (`hrai/tools.py`): 21 LangChain tools. Each one checks the caller's role, so employees can only touch their own leave records. Each one runs the `pre_tool` and `post_tool` hooks, and a tool that crashes reports an error and opens a ticket instead of crashing the agent. The same tools are served to LangGraph, CrewAI, MCP and the rules-only plans.
- **Skills** (`skills/*/SKILL.md`): instruction packs in the Agent Skills layout. They are resume-screening (including the fairness rules), leave-policy, onboarding, and ticket-fix-review. Agents see each skill's name and description and load the full text with `load_skill` when needed. Adding a skill is adding a folder. Skills are also exposed as MCP prompts.
- **Hooks** (`hrai/hooks.py`, plus your own in `hooks.d/`): code that runs at fixed points of every request.
  - Points: pre_request, post_response, pre_tool, post_tool, on_error, on_feedback.
  - Built-in hooks: a prompt-injection guard, PII redaction (PAN, Aadhaar, phone, account numbers) for logs and tickets, a request size limit, tool-call auditing, a knowledge-gap-to-ticket hook, an error-to-ticket hook, and a negative-feedback-to-ticket hook.
- **Triggers** (`hrai/triggers.py`, `hrai/automations.py`):
  - Scheduled: `onboarding_document_chase` (daily 09:00), `ticket_sweep` (every 10 minutes), `budget_watch` (daily 18:00), `reindex_knowledge` (daily 02:00).
  - Events: `new_hire.created` → onboarding plan; `candidate.added` → index and score; `ticket.created` → start the ticket agent right away when `HRAI_TICKET_AUTOWORK=1`.
  - `app.py triggers run` runs the scheduler, and every run is logged.

## 6. Protocols: MCP and A2A

- **MCP** (`hrai/mcp_server.py`, MCP Python SDK 2.x):
  - It serves the HR tools the token's role allows, plus `ask_hr`, which routes a question through the full agent stack. It also serves the handbook and FAQ as resources and the skills as prompts.
  - It runs over stdio for Claude Desktop and Claude Code (see `mcp_config.example.json`), or over streamable HTTP.
  - The `HRAI_MCP_TOKEN` setting identifies the user, so MCP calls get the same permission checks, hooks and audit log as the web console.
- **A2A** (`hrai/a2a.py`, A2A protocol 0.3):
  - Discovery: an agent card at `/.well-known/agent-card.json` and one card per agent under `/a2a/<agent>/`.
  - Calls: JSON-RPC `message/send` (`SendMessage` from A2A 1.0 also works) and `tasks/get`, with bearer-token auth and role checks.
  - Inside the app, agents use the `ask_agent` tool. For example, the onboarding agent asks the policy agent which documents are mandatory.
  - That call goes through the same A2A envelope, in-process by default or over HTTP when `HRAI_A2A_URL` points at another server. Delegation depth is capped at 2.

## 7. Error, feedback and ticket harness

**Sources of tickets:** unhandled exceptions (requests, tools, hooks, triggers), ERROR log records, a 👎 on any answer in the web console (with a comment), answers that admit a handbook gap, and agents calling `report_issue`. The same problem seen again bumps the open ticket's occurrence count (by fingerprint) instead of opening a new ticket. Tickets are redacted for PII.

**Workflow** (`hrai/ops/ticket_agent.py`, LangGraph with a SQLite checkpointer, one durable thread per ticket):

1. **triage**: kind, severity and component, using Haiku or rules.
2. **propose_fix**:
   - A knowledge gap becomes a "Pending HR confirmation" entry in `data/kb_additions.md`. It never invents policy.
   - A code bug gets a patch from Sonnet, built from the traceback's files and the ticket-fix-review skill. Forbidden paths (`.env`, `var/`, secrets) are refused.
   - With no model available, a code bug goes to `needs_human`.
3. **test**: the patch is applied on branch `ticket/<id>` in its own **git worktree**, and the test suite runs there.
4. **ai_review**: rule checks (tests green, diff size, no secrets, allowed paths, Python compiles), then a separate Sonnet reviewer prompt. If changes are requested, it loops back to step 2, at most twice.
5. **raise_pr**: commits, pushes the branch, opens a PR on `HRAI_GITHUB_REPO` with `gh`, and posts the AI review as a PR comment. Without GitHub, the branch stays local.
6. **await_approval**: the workflow **stops** (LangGraph interrupt) until an admin approves or rejects, either with `app.py tickets approve|reject <id>` or in the web console. Nothing is merged before this.
7. **finish**:
   - Approve: merge (`gh pr merge --squash`, or a local `--no-ff` merge), refresh the vectors and knowledge graph if data changed, and **close the ticket with the resolution**.
   - Reject: close the PR, delete the branch, and close the ticket as rejected with the reason.

`app.py tickets sync`, which also runs in `ticket_sweep`, closes tickets whose PR someone merged or closed directly on GitHub. Every step is recorded in `ticket_events`, which shows up as the ticket history.

## 8. Security notes

- Passwords: bcrypt with cost 12, at least 10 characters. Unknown usernames take the same time to fail as wrong passwords. Accounts lock for 15 minutes after 5 failures.
- Sessions: random tokens stored only as SHA-256 hashes, with an HttpOnly, SameSite=Strict cookie. POSTs must be JSON. Security headers (CSP, nosniff, DENY framing) are set.
- Roles are checked twice: at the agent gateway (which agents you may use) and inside every tool (which records you may touch).
- The server binds to 127.0.0.1 unless `HRAI_HOST` is set. Secrets live in `.env`, which git ignores.
- No email is ever sent. Leave exceptions, code fixes and knowledge additions all need a human decision.

## 9. Hiring pipeline (added in the HR suite, phase 1)

`hrai/hiring.py`, the **recruitment agent**, 12 tools, the Hiring tab and three triggers.

- **Inbox.** `inbox/<JOB-ID>/` takes .pdf (pypdf), .docx (read directly from the file's XML), .txt and .md. A file
  is read once (SHA-256 of its bytes); a second resume with the same email for the same job is reported as a duplicate.
  Name, email, phone, years and skills are pulled out with rules.
- **Screening.** rejected = misses a must-have or the minimum years; selected = score at or above the job's
  threshold (default 70); on hold = meets the minimum but scores lower, so HR decides. A copy of the file goes to
  `sorted/<stage>/`.
- **Rounds.** Each job stores its own ordered list of rounds (L1..Ln, HR, Final). Passing a round moves the candidate
  to the next one; passing the last makes them ready for an offer. Fail rejects them (a regret email is drafted); hold
  parks them.
- **Offer and joining.** An offer request goes to the approvals queue; the offer email is drafted only after
  approval. Acceptance creates the new hire, which starts the onboarding plan through the `new_hire.created` trigger,
  and adds follow-ups at -7, -3, 0, +30 and +90 days from joining.
- **Tracking.** `candidate_events` is each candidate's timeline; `followups` holds what is due.
- **Triggers.** `inbox_watch` (every 2 minutes), `hiring_followups` (08:30: HR digest and next-day interview
  reminders), `stale_candidates` (10:00: anyone stuck 5+ days gets a follow-up).

## 10. Insights and the console redesign (HR suite, phase 2)

`hrai/insights.py` computes every number from SQLite on request (no stale cache): the hiring funnel (candidates who
*reached* each step), per-round pass rate (passed / decided) and average rating, average days from application to
offer, acceptance and joining, offer acceptance (accepted / answered), missing must-have skills among rejected
resumes, headcount, joiners by month, leave by type, onboarding progress, AI spend per day, per agent and per tier,
and tickets and feedback.

- **Insights agent** (fast tier, `hr_insights` and `needs_attention` tools, `hr-insights` skill). It answers with tool
  numbers only. The router sends analytics questions to it; leave-policy questions still go to the policy agent.
- **Needs attention** is the UX agent behind the dashboard: concrete next steps, most urgent first, each naming the
  page (and candidate or ticket) to act on. Actions also close follow-ups they make moot: recording a round closes its
  reminder, scheduling a round closes "schedule next round", requesting an offer closes "prepare the offer", and
  rejection or withdrawal closes the rest.
- **Console.** Sidebar navigation, a Ctrl K ask bar on every page, a light and a dark theme (system by default), a
  phone layout, and charts drawn in plain HTML/SVG with no chart library or CDN, so it works offline. Charts follow
  one validated palette (colour-blind checked in both themes), keep values readable without hover, and each has a
  table view. Status colours always come with an icon and a word.
- **Export.** `/api/insights/candidates.csv` (HR and admin only); cells that start with `=`, `+`, `-` or `@` are
  prefixed so resume text cannot run as a spreadsheet formula.

## 11. Salary and payroll (HR suite, phase 3)

`hrai/payroll.py` with the rates in `data/payroll_rules.json` (not in code, because they change every financial year).

- **Structure.** Basic 50% of CTC, HRA 50% of basic in a metro and 40% elsewhere, special allowance the rest.
  Employer PF, employer ESI and gratuity sit inside CTC. ESI applies only while monthly gross is at or under ₹21,000.
- **TDS.** Project the year's income from what has been paid so far plus the months left, compute the annual tax (new
  regime by default; old regime takes 80C including PF, 80D, HRA exemption and professional tax), subtract TDS already
  deducted, and spread the rest. No PAN means at least 20%. When a month is processed before earlier months of the
  same financial year exist in the system, the payslip says so.
- **Two approvals.** A salary revision waits for approval before it takes effect; a payroll run is submitted, and
  whoever submitted it cannot approve it (checked in `decide_approval`). Approval writes the payslips and
  `var/payroll/<month>/bank_transfer.csv` and drafts the payslip emails; HR pays through the bank and then records
  the reference. A submitted month is locked against edits; an approved or paid month cannot be recomputed.
- **Privacy.** `my_payslip` gives employees and managers only their own payslip, and only once approved. PAN, UAN and
  bank account are masked everywhere. The bank file and payroll summary need `payroll:view` (HR and admin).
- **Agent.** The payroll agent answers breakup, regime-comparison, run, submit, revision and payslip requests, and the
  router sends salary wording to it. The dashboard and the needs-attention list pick up payroll state too.

## 12. Projects and staffing (HR suite, phase 4)

`hrai/projects.py`, the **projects agent**, 14 tools, the Projects page and a daily `project_health` trigger.

- **Allocation** is a percent of someone's time between two dates. Overlapping allocations are added up, and
  `allocate` refuses anything that would pass 100%, saying how much is actually free. Releasing sets an end date and
  keeps the history, so capacity stays truthful.
- **Capacity** looks four weeks ahead and subtracts approved leave. The bench is anyone at or under 50%.
  Utilisation is the average across everyone.
- **Tasks and milestones** carry a status, owner and due date; overdue is past due and not done. Timesheets record
  hours per person per project per day (16 hours a day maximum, nothing in the future).
- **Risks** is the staffing version of needs-attention: overdue tasks, an active project with nobody on it, a project
  ending with work open, someone over 100%, a skill nobody on the project has (with free people who do), and people
  rolling off in the next two weeks. These also appear on the HR dashboard.
- **Skills** now live on employees (`employees.skills`) and on projects, which is what drives staffing suggestions
  and gaps; a gap with nobody free to fill it is a hiring signal, next to the hiring funnel on the same dashboard.
- Employees see and log only their own; staffing changes need `projects:manage` (admin, HR, manager).

## 13. Culture, recognition and HR activities (HR suite, phase 5)

`hrai/engage.py`, the **culture agent**, 16 tools, the Culture page and two triggers (`culture_calendar` in the
morning, `event_wrap_up` in the evening).

- **Events** carry a kind, a day, a venue, an audience, a budget and spend. A budget over
  `HRAI_EVENT_BUDGET_LIMIT` (₹25,000 by default) opens an `event_budget` approval, and `announce` refuses until a
  human decides, so no event is announced on money nobody agreed to. Spend past 110% of the budget is refused too:
  raise the budget, which goes through approval again.
- **Announcing** drafts the all-hands invitation into the outbox and marks the event announced. As everywhere else in
  the app, nothing is emailed by itself.
- **RSVPs** are one row per person per event (yes, no, maybe, plus guests), upserted so changing your mind is normal.
  Attendance counts heads including guests, and the response rate uses headcount.
- **The calendar** merges events, public holidays and occasions. Occasions are birthdays and work anniversaries
  computed from `employees.date_of_birth` and `joined_on` against the year the date next falls in (29 February lands
  on 1 March), so no second table needs maintaining.
- **Recognition** has two levels: kudos, which anyone can give to anyone but themselves and which cost nothing, and
  awards, which anyone can nominate for but only HR decides; an award drafts a congratulations email.
- **Pulse surveys** are one question on a 2-10 scale. Answers store a SHA-256 hash of the person and the survey
  instead of their id, which stops a second answer without recording who answered, and results stay hidden until
  three people have answered so a single answer cannot be picked out.
- **Engagement numbers** (events, attendance, spend against budget, kudos reach, award states, pulse averages) feed
  the HR dashboard: upcoming events and kudos become KPIs, and an event within a week that nobody has announced, or
  an occasion in the next three days, becomes an attention item.

## 14. Phases

| Phase | Content | Status |
| --- | --- | --- |
| 1 | SQLite, login and roles, LiteLLM gateway with budgets | Done |
| 2 | Chroma RAG, KAG graph, CAG, MAG | Done |
| 3 | LangGraph orchestrator, LangChain tools, screening crew, hooks, skills | Done |
| 4 | MCP server, A2A, triggers, web console | Done |
| 5 | Ticket tracker and auto-fix agent with PRs and human approval | Done |
| 6 | Hiring pipeline: resume inbox, screening, L1..Ln/HR/Final rounds, offers, joining, follow-ups | Done (HR suite phase 1) |
| 7 | Insights and analytics dashboard; UI redesign | Done (HR suite phase 2) |
| 8 | Salary management: CTC breakup, PF, ESI, professional tax, TDS, payslips, payroll approvals | Done (HR suite phase 3) |
| 9 | Project management: projects, allocations, capacity and the bench, tasks, timesheets | Done (HR suite phase 4) |
| 10 | Cultural events and HR activities: events and budgets, RSVPs, kudos, awards, pulse surveys | Done (HR suite phase 5) |
| 11 | Port the v1 voice-call agent; real HRMS/ATS connectors; email sending behind approval | Later |
| 12 | Multimodal document checks (ID proofs, offer letters); evaluation suite for answer quality | Later |

## 15. What was tested, and what was not

**Tested (82 automated tests on Python 3.14.6, offline):**
- Login, hashing, lockout and roles
- All four agents in rules-only mode
- The LLM tool loop with a scripted model response
- Gateway fallback, downgrade, block and cost logging
- CAG prompt caching flag and answer cache
- KAG rules and facts, RAG, MAG
- Injection block, PII redaction, crash-to-ticket and feedback-to-ticket
- A2A over HTTP, including the role-based rejection
- Web login and permissions
- MCP tools filtered by role
- Triggers
- The hiring pipeline: .pdf, .docx and .txt resumes, sorting, duplicates, a five-round journey to offer approval,
  joining and follow-ups, fail and hold, the web upload, and the triggers
- Insights: funnel, rounds, missing skills, the attention list and its ordering, moot follow-ups closing, the
  insights agent and routing, the daily report, the dashboard API and CSV (including role checks and formula escaping)
- Payroll: CTC breakup (metro and not, with and without ESI), professional tax by state, 87A rebate, surcharge,
  no-PAN TDS, HRA exemption, LOP and one-off items, run to submit to approve to paid (including the submitter being
  refused), TDS spread across months, revisions behind approval, payslip privacy and the web API
- Projects: the 100% allocation rule, releasing, capacity with approved leave, the bench, staffing gaps and
  suggestions, task status and overdue, timesheet limits, risks, the agent in rules mode, role checks and the API
- Culture: a budget over the limit waiting for a human before anything is announced, runaway spend refused, RSVP
  changes and counts, the calendar with holidays and occasions, kudos (including to yourself), awards needing HR, a
  pulse survey staying hidden until three answers and refusing a second answer from the same person, role checks,
  the triggers and the API
- The full ticket workflow (gap → patch → worktree tests → review → approve → merge → closed, plus the reject and needs-human paths) in a repo where the app sits in a subfolder

**Also checked by hand:** the MCP server over stdio with a real MCP client, the CrewAI wiring on Python 3.13 (crew assembly and tools, with the model call mocked), and MiniLM semantic search.

The payroll figures follow the rules in `data/payroll_rules.json` and were checked against worked examples, but they
have not been reviewed by a tax professional; confirm them with your CA before the first live run.

**Not tested here:**
- Live Claude calls, because there was no API key in the build environment.
- A live Ollama model, because the download was blocked.
- A live CrewAI run against a model.
- The LiteLLM proxy server.
- Real `gh` PR creation from the ticket agent. The agent's own PR path needs `gh auth login` on your machine.
