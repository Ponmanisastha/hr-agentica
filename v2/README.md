# HR agentic AI v2

AI agents for HR built on LangGraph, LangChain tools, a screening crew, Claude through a LiteLLM gateway with budgets, local RAG/KAG/CAG/MAG, SQLite, logins with roles, MCP, A2A, triggers, hooks and skills. It also includes an engineering agent that turns errors and feedback into tickets and works them through to a reviewed, human-approved fix.

**How it works and why:** [DESIGN.md](DESIGN.md). v1 (the folder above this one) is unchanged.

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

Web console tabs depend on your role: Ask (with 👍 and 👎 feedback), Approvals, Tickets (approve or reject fixes), Budget, and Outbox (email drafts).

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
python -m unittest discover -s tests -t .     # 28 tests, offline, about 15 seconds
```

## Layout

| Path | What it is |
| --- | --- |
| `app.py` | Command line |
| `hrai/agents/` | LangGraph orchestrator, specialist agents, screening crew |
| `hrai/gateway/` | AI gateway (LiteLLM, budgets), agent gateway, optional LiteLLM proxy config |
| `hrai/knowledge/` | Chroma vectors (RAG), knowledge graph (KAG), CAG, memory (MAG) |
| `hrai/tools.py` | The HR tools (LangChain), with role checks |
| `hrai/auth.py`, `hrai/db.py` | Logins and roles; SQLite schema |
| `hrai/hooks.py`, `hooks.d/` | Hooks |
| `hrai/triggers.py`, `hrai/automations.py` | Triggers |
| `hrai/skills.py`, `skills/` | Skills |
| `hrai/a2a.py`, `hrai/mcp_server.py`, `hrai/web.py` | A2A, MCP, web console and API |
| `hrai/ops/` | Ticket tracker and ticket agent |
| `data/` | Sample HR data (same as v1); `kb_additions.md` appears when approved fixes add to the handbook |
| `var/` | Runtime state: database, vectors, reports, worktrees (git-ignored) |
