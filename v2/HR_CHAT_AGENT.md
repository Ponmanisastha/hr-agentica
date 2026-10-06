# HR Chat Agent: how this project meets the brief

The brief: build an HR chat agent with an agentic AI framework that authenticated employees can talk to. It
answers from the company's HR policy documents and uses tools and the database for dynamic questions such as
leave balance, eligibility and leave calculations. It should show tool use, agent reasoning and workflow,
context handling and response generation.

This page maps each point to the code, says where your own policy documents go, and gives a five-minute demo.
Everything below runs offline. With an `ANTHROPIC_API_KEY` in `.env` the same agents use Claude for routing and
reasoning, and the trace shows the model's tool calls instead of the offline planner's.

## The requirements, one by one

| Brief | Where it is | How to see it |
| --- | --- | --- |
| Agentic AI framework | LangGraph orchestrator (`hrai/agents/graph.py`, `build`): recall memory → route → specialist agent → remember. Tools are LangChain `StructuredTool`s (`hrai/tools.py`, `hr_tool`). The screening crew uses CrewAI roles (`hrai/agents/crew.py`). | `DESIGN.md` §2 |
| Authenticated employees only | bcrypt passwords, lockout after 5 failures, expiring session tokens (`hrai/auth.py`, `login`, `user_for_token`). Every web, API, MCP and A2A call is tied to a user, and every tool checks the role (`hr_tool`). Employees see only their own leave and pay (`_check_employee_access` in `hrai/tools.py`). | Sign in to the console as an employee; ask "Show leave balance for E102" and it is refused. |
| Answers from your HR policy documents | `policies/` folder (`hrai/knowledge/policies.py`). The documents are split into sections and embedded in a local Chroma store for RAG (`hrai/knowledge/vectors.py`). When they are small, the whole text rides in a cached prompt (CAG, `hrai/knowledge/cag.py`). Answers cite the document and section. | Ask "How many days of maternity leave do we offer?" |
| Tools and database for dynamic questions | `leave_balance` (balances, taken, pending, upcoming, accrual), `evaluate_leave_request` (working days without weekends and holidays, notice, balance after, approve or route to manager), `record_leave_decision`, `get_employee` (level, manager, notice period). All of them read SQLite. | Ask "What is my leave balance?" and "Can I take casual leave on 2026-10-08 and 2026-10-09?" |
| Leave balance or eligibility | `leave_balance`, `get_employee`, policy rules from the knowledge graph | "What is my leave balance?", "What is my notice period?" |
| Leave calculation based on policy | The thresholds (notice days, the auto-approval limit, the casual cap, the certificate rule, monthly accrual, carry-forward) are read **from your policy documents** into a knowledge graph (KAG, `hrai/knowledge/kag.py`, `RULE_PATTERNS`), so changing the document changes the calculation. | "If I take annual leave from 2026-10-16 to 2026-10-19 how many days will be deducted?" gives 2 working days (Friday and Monday). "How much annual leave will I have accrued by December?" |
| Tool usage | Each answer carries its trace: every tool called, with its input and output. | Console → Assistant → **How I answered** under each answer |
| Agent reasoning and workflow | The router picks a specialist (policy, leave, payroll, projects, culture…) from the request, offering only the agents your role may use. The leave agent decides whether you are *asking* (evaluate only, nothing booked) or *applying* (evaluate, record, draft emails, send exceptions to the manager's approval queue). | The trace and the agent name on each answer |
| Context handling | Short-term: the last turns of the conversation (LangGraph checkpointer per conversation id), so "and sick leave?" after a casual-leave question answers for sick leave, and "what did I just ask?" works. Long-term: per-user memory (MAG, `hrai/knowledge/mag.py`). The user's identity, role and today's date are in every agent's prompt. **New conversation** in the console starts fresh. | Ask "How many casual leave days do we get?", then "and sick leave?" |
| Response generation | Two to four sentences, grounded in tool output, with the policy cited. When the documents do not cover a question it says so and opens a ticket for HR rather than guessing (`hrai/ops/tracker.py`). | "Is there a policy on pet insurance?" |
| Safety | Prompt-injection guard and PII redaction in hooks (`hrai/hooks.py`). Rate limiting and per-agent AI budgets (`hrai/gateway/`). Emails are only drafted, never sent. | `DESIGN.md` §8 |

## Where your own HR policy documents go

Put them in **`v2/policies/`** as `.pdf`, `.docx`, `.md` or `.txt`. While that folder is empty the sample
handbook (`data/policy_handbook.md`) is used. The first document you add replaces it. There are three ways to
add documents:

```bash
python app.py policies add "Leave Policy 2026.pdf" "Code of Conduct.docx"   # copies and indexes
python app.py policies                                                     # what was indexed, by section
python app.py policies reindex                                             # after editing files in place
```

You can also open **Policies** in the web console and use **Add documents** (HR and admin only). Or just copy
files into the folder: while `python app.py serve` runs, the `policy_watch` trigger re-indexes within five
minutes. To keep the documents somewhere else, set `HRAI_POLICY_DIR` in `.env`.

How the documents are read:

- **Sections.** Markdown is split on `## ` headings. PDF, Word and text files are split on heading-like lines
  such as "3. Casual leave", "2.1 Sick leave" or "SICK LEAVE". A file with no headings is cut into parts of about
  1,200 characters. Each section is cited as `<file>, section <title>`, so clear headings give clear citations.
- **Rules.** The leave engine reads its numbers from sections whose titles mention *annual*, *sick*, *casual*,
  *maternity*, *paternity*, *notice*, *home* or *reimbursement*. It matches the phrasing in `RULE_PATTERNS`
  (`hrai/knowledge/kag.py`), such as "at least N calendar days in advance" or "at most N consecutive days".
  If your wording differs, add a pattern there. A rule it cannot find falls back to a safe default, and the
  answer cites "the default rules" so you notice.
- **Balances** are not in the documents. They come from the `employees` table (seeded from
  `data/employees.json`) and the leave requests. Approved leave lowers the balance.

`policies/README.md` repeats this next to the files.

## A five-minute demo

```bash
cd v2 && source .venv/bin/activate
python app.py init --demo-users      # creates hr_demo, manager_demo and the employee login deepa (E101)
python app.py serve                  # http://localhost:8000
```

Sign in as **deepa** (the password is printed by `init`), open **Assistant**, and ask these in order:

1. "How many days of maternity leave do we offer?" The policy agent answers from the documents with a citation.
2. "and paternity leave?" A follow-up that keeps the context.
3. "What is my leave balance?" The leave agent calls `leave_balance` against the database.
4. "Can I take casual leave on 2026-10-08 and 2026-10-09?" It calculates the days and the policy decision, and
   books nothing.
5. "I need casual leave on 2026-10-08" This one is recorded. It is approved, or sent to the manager with the
   policy reason.
6. "Show leave balance for E102" This is refused, because employees see only their own data.

Open **How I answered** under any answer to see the agent and each tool call. Then sign in as **hr_demo**, open
**Policies**, add your own policy file, and ask question 1 again. The citation now names your file.

From the command line, the same flow runs with `python app.py ask "What is my leave balance?" --as deepa`.

## Tests

`python -m unittest discover -s tests -t .` runs 96 offline tests. `tests/test_chat_agent.py` covers this brief:
your own documents replacing the sample and driving the rules, Word and unstructured files, the watch trigger,
balances and accrual, questions that book nothing, applying, employee isolation, follow-ups, conversation
memory, routing, and the Policies API.
