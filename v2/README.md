# HR agentic AI v2

AI agents for HR built on LangGraph, LangChain tools, a screening crew, Claude through a LiteLLM gateway with budgets, local RAG/KAG/CAG/MAG, SQLite, logins with roles, MCP, A2A, triggers, hooks and skills. It also includes an engineering agent that turns errors and feedback into tickets and works them through to a reviewed, human-approved fix.

**How it works and why:** [DESIGN.md](DESIGN.md). **Step by step, with sample data:** [GUIDE.md](GUIDE.md). v1 (the folder above this one) is unchanged.

## Set up on WSL (Python 3.14)

```bash
cd v2
./setup_wsl.sh            # creates .venv, installs requirements, creates logins, indexes data, runs the tests
source .venv/bin/activate
```

The script prints the **admin password** and three demo logins once (hr_demo, manager_demo, and deepa, an employee). Save them.

To create the virtual environment by hand instead:

```bash
python3.14 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env && python app.py init --demo-users
```

### Models (pick any; all optional)

- **Claude:** put `ANTHROPIC_API_KEY=...` in `.env`. Sonnet 5.5 handles the harder steps and Haiku 4.5 the simple ones.
- **Free local model:** install Ollama in WSL (`curl -fsSL https://ollama.com/install.sh | sh`), then `ollama pull llama3.1:8b`. If Ollama runs on Windows instead, set `OLLAMA_API_BASE` to an address WSL can reach.
- **Neither:** everything still runs in rules-only mode.

## Use it

```bash
python app.py demo                                  # every agent on sample data, A2A call, a ticket round trip
python app.py ask "Onboard Vikram Singh"            # one request as the local admin
python app.py ask "I need casual leave on 2026-10-08" --as deepa   # as an employee (asks for the password)
python app.py serve                                 # web console http://localhost:8000
python app.py triggers run                          # scheduler (reminders, ticket sweep, budget watch, reindex)
python app.py budget                                # spend vs budget per agent
python app.py budget set screening 20 --on-exceed block
```

Web console pages depend on your role: Dashboard (HR insights), Assistant (with 👍 and 👎 feedback, and **How I answered** showing each tool the agent called), Policies, Hiring, Projects, Culture, Payroll, My pay (for employees), Approvals, Tickets (approve or reject fixes), AI budget, and Outbox (email drafts). The search bar at the top (Ctrl K) sends any question to the agents from every page. Light and dark themes follow your system, or pick one with the Theme button.

## Sample data

`samples/` has 11 policy documents (PDF, Word, Markdown), 10 resumes for three openings, and 15 employees with leave
history and salaries, all for a fictional company. Load it, then follow [GUIDE.md](GUIDE.md):

```bash
python app.py samples load                                         # adds only what is missing
python app.py knowledge rag|cag|kag "<question>"                   # what each knowledge layer returns
python app.py knowledge kag rules                                  # leave rules read from the documents
python app.py knowledge mag <username>                             # what the assistant remembers about a user
```

## Your HR policy documents

Put your own policies (`.pdf`, `.docx`, `.md`, `.txt`) in `v2/policies/`, or upload them on the **Policies** page.
They replace the sample handbook, answers cite them by file and section, and the leave rules (notice, limits,
accrual, carry-forward) are read from them. See `policies/README.md`.

```bash
python app.py policies add "Leave Policy 2026.pdf"   # stage it; prints what changes
python app.py policies pending                       # waiting for approval
python app.py policies approve ID                    # publish (the old version is archived)
python app.py policies history NAME                  # versions; rollback NAME VERSION restores one
python app.py policies                               # what is live
```

A policy uploaded again becomes a new version that HR approves before it goes live: the agent shows the changed
sections and rules and warns when another document disagrees. The old version is kept and can be restored, so
answers never mix two versions.

## Uploads in the web portal

Each upload sits on the page for its step, and each file goes to its own folder:

- **Policies:** policy documents, versioned as above (`policies/`, `policies/.pending/`, `policies/.archive/`).
- **Hiring:** resumes for an opening (`inbox/<JOB-ID>/`, sorted by stage after screening).
- **Documents:** onboarding documents for new hires (`documents/new-hires/<NH-ID>/<type>/`) and employee documents
  such as medical certificates and investment proofs (`documents/employees/<E-ID>/<type>/`). HR verifies or
  rejects each one; employees see and upload only their own. Uploads unblock the matching onboarding tasks.

```bash
python app.py documents NH-202                       # checklist for a new hire or employee
python app.py documents add NH-202 pan_card pan.pdf
python app.py documents verify ID                    # or: reject ID --note "Not readable"
```

[GUIDE.md section 23](GUIDE.md#23-uploads-where-each-file-goes-and-policy-versions) walks through it.

[HR_CHAT_AGENT.md](HR_CHAT_AGENT.md) maps the HR chat agent brief (authentication, policy answers, leave balance and
calculation tools, reasoning, context) to the code, with a five-minute demo.

## Hiring pipeline

Drop resumes (.pdf, .docx, .txt, .md) into `inbox/<JOB-ID>/` (for example `inbox/JOB-101/`), or drag them onto the
Hiring tab in the web console. They are read, screened and sorted into **selected**, **on hold** or **rejected**, and a
copy lands in `inbox/<JOB-ID>/sorted/<stage>/` so the folder shows the result too. With `python app.py triggers run`
going, new files are picked up every 2 minutes.

Each job has its own interview rounds (default L1, L2, HR, Final). Change them in the Hiring tab or with
`python app.py hiring rounds JOB-101 L1 L2 L3 HR Final`. From the Hiring board you schedule rounds, record pass, fail or
hold with a 1-5 rating and feedback, request an offer (it waits in Approvals), record the candidate's answer, and mark
them joined. Accepting an offer creates the new hire, starts onboarding, and sets follow-ups: a pre-joining call,
a documents check, day one, a 30-day check-in and a 90-day probation review. Every step is on the candidate's timeline.

```bash
python app.py hiring sample          # copy three sample resumes into inbox/JOB-101/
python app.py hiring ingest          # read and screen them now
python app.py hiring                 # pipeline counts per stage and round
python app.py hiring followups
python app.py ask "Divya cleared L1 with rating 4, schedule the next round on 2026-10-12 at 15:00"
```

The `inbox/` folder is git-ignored because resumes are personal data.

## Insights dashboard

HR and admins land on the Dashboard: active candidates, interviews this week, offers out, joiners in the next 30 days
and follow-ups due; a **Needs your attention** list (overdue follow-ups, approvals, candidates with no interview,
missing interview results, joiners with missing documents, budgets past 80%, fixes waiting for review), each with an
Open button; the hiring funnel, results by round, time to offer/acceptance/joining, offer acceptance, the must-have
skills most often missing from rejected resumes; headcount by department, joiners by month, leave by type,
onboarding progress; and AI spend per day and per agent. Pick a job to narrow the hiring numbers. Every chart has a
Table button, and **Export candidates (CSV)** downloads the pipeline for a spreadsheet.

```bash
python app.py insights                # plain-language snapshot
python app.py insights attention      # what needs HR today
python app.py insights csv > pipeline.csv
python app.py ask "How is hiring going?"
```

A snapshot is saved to `var/reports/` every morning (`insights_report` trigger); on Mondays it is also drafted as an
email to HR (`HRAI_HR_EMAIL`).

## Projects and staffing

Projects hold who is on them (a percentage of their time between two dates), tasks and milestones, and timesheets.
Nobody can be booked past 100%, so capacity, the bench and utilisation all come from the same numbers, with approved
leave taken off.

```bash
python app.py projects                 # board: team size, FTE, open and overdue tasks, hours, days left
python app.py projects capacity        # who is booked how much, and who is free
python app.py projects risks           # overdue work, unstaffed projects, skill gaps, people rolling off
python app.py ask "Who is free next month?"
python app.py ask "Put Deepa on the Customer portal revamp at 40%"
python app.py ask "What is slipping?"
```

A project lists the skills it needs; when nobody on it has one, the Projects page says so and names people who are
free and do. Managers get the Projects page; employees see their own projects and log their own hours.

## Culture, recognition and HR activities

Events (festivals, town halls, offsites, training, volunteering, sports) carry a budget, an audience and RSVPs. A
budget over the limit (`HRAI_EVENT_BUDGET_LIMIT`, ₹25,000 by default) goes to **Approvals** first, and nothing is
announced until a human says yes. The invitation itself is a draft in the outbox; the app never emails anyone.

```bash
python app.py culture                  # events this year, spend against budget, kudos, pulse scores
python app.py culture calendar         # events, public holidays, birthdays and work anniversaries
python app.py culture kudos            # the kudos wall
python app.py culture awards           # nominations and winners
python app.py culture pulse            # survey results, once three people have answered
python app.py ask "Plan a Diwali lunch on 2026-11-08 with a budget of 20000"
python app.py ask "Kudos to Deepa for covering the on-call weekend"
python app.py ask "What is coming up this month?"
```

Anyone can send kudos, nominate a colleague for an award and answer a pulse survey; HR decides awards and starts
surveys. Pulse answers keep only a hash of who answered, and results stay hidden until three people have answered, so
no single answer can be traced back. The **Culture** page shows all of it.

## Salary and payroll

Indian payroll: CTC breakup (basic, HRA, special allowance, employer PF, gratuity), employee PF and ESI, professional
tax by state, and TDS under the new or the old regime, projected across the financial year. Employees see their own
payslip on **My pay**; HR runs payroll on the **Payroll** page.

```bash
python app.py payroll run --month 2026-10      # draft: pay, PF, ESI, PT and TDS for everyone
python app.py payroll show --month 2026-10
python app.py payroll submit --month 2026-10   # goes to Approvals; someone else must approve it
python app.py payroll paid NEFT-5521 --month 2026-10
python app.py payroll slip E101 --month 2026-10
python app.py ask "What is the breakup for a 12 lakh CTC?"
python app.py ask "Give Deepa a 10% hike from next month"     # waits for approval
```

A salary revision and a payroll run both need human approval, and payroll cannot be approved by whoever submitted it.
Approval writes payslips and `var/payroll/<month>/bank_transfer.csv`; **nothing is ever paid from here**. HR uploads
that file to the bank and then records the reference. An approved month is locked: fix it next month with an arrears
or recovery item.

Rates, slabs and the professional-tax tables live in `data/payroll_rules.json`. Check them with your CA at the start
of each financial year and edit that file; no code change is needed.

## Tickets and auto-fixes

```bash
python app.py tickets list
python app.py tickets work 3        # triage, patch, test in a git worktree, AI review, open a PR
python app.py tickets show 3        # patch, review, history
python app.py tickets approve 3     # human approval: merges and closes the ticket with the resolution
python app.py tickets reject 3 --note "HR will write this policy"
python app.py tickets sync          # closes tickets whose PR was merged or closed on GitHub
```

For real PRs, set `HRAI_GITHUB_REPO=Ponmanisastha/hr-agentica` in `.env` and run `gh auth login` once. Without it, fixes stay on local branches. Nothing merges without an approval.

## MCP and A2A

```bash
python app.py token create admin --label claude-desktop     # token for MCP or A2A clients
HRAI_MCP_TOKEN=<token> python app.py mcp                    # stdio; see mcp_config.example.json
python app.py serve &                                       # A2A cards at /.well-known/agent-card.json
python app.py a2a send http://localhost:8000/a2a/policy "How long is paternity leave?" --token <token>
```

## Tests

```bash
python -m unittest discover -s tests -t .     # 114 tests, offline, about 35 seconds
```

## Layout

| Path | What it is |
| --- | --- |
| `app.py` | Command line |
| `hrai/agents/` | LangGraph orchestrator, specialist agents, screening crew |
| `hrai/gateway/` | AI gateway (LiteLLM, budgets), agent gateway, optional LiteLLM proxy config |
| `hrai/knowledge/` | Chroma vectors (RAG), knowledge graph (KAG), CAG, memory (MAG) |
| `hrai/tools.py` | The HR tools (LangChain), with role checks |
| `hrai/hiring.py` | Hiring pipeline: inbox, screening, rounds, offers, joining, follow-ups |
| `hrai/insights.py` | HR analytics, the needs-attention list, daily report, CSV export |
| `hrai/payroll.py` | Salary structures, Indian payroll, payslips, payroll runs |
| `hrai/projects.py` | Projects, allocations, capacity and the bench, tasks, timesheets |
| `hrai/knowledge/policies.py`, `policies/` | Your HR policy documents: reading, sections, citations |
| `hrai/knowledge/versions.py` | Policy versions: staging, changes and conflicts, approval, archive, rollback |
| `hrai/documents.py`, `documents/` | Uploaded employee and new-hire documents (git-ignored) |
| `hrai/engage.py` | Events and budgets, RSVPs, kudos, awards, anonymous pulse surveys |
| `hrai/auth.py`, `hrai/db.py` | Logins and roles; SQLite schema |
| `hrai/hooks.py`, `hooks.d/` | Hooks |
| `hrai/triggers.py`, `hrai/automations.py` | Triggers |
| `hrai/skills.py`, `skills/` | Skills |
| `hrai/a2a.py`, `hrai/mcp_server.py`, `hrai/web.py` | A2A, MCP, web console and API |
| `hrai/ops/` | Ticket tracker and ticket agent |
| `data/payroll_rules.json` | PF, ESI, professional tax and income-tax rates, editable each year |
| `data/` | Sample HR data (same as v1); `kb_additions.md` appears when approved fixes add to the handbook |
| `var/` | Runtime state: database, vectors, reports, worktrees (git-ignored) |
