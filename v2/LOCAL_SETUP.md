# Run v2 on your own machine and test RAG, CAG, KAG and MAG one by one

This is the shortest path from a fresh WSL terminal to testing each knowledge layer on its own, with the output you
should see. Everything here was run on a clean Python 3.14.6 virtual environment with no Ollama and no API key.
[GUIDE.md](GUIDE.md) covers every other feature.

## 0. First: run v2 commands from the `v2` folder

The repository has two apps. `python app.py` at the repository root is the older **v1** prototype. It only knows
`demo`, `ask` and `serve`, so for any other command it prints this:

```
HR agentic AI prototype.

python app.py demo                 run all four agents on sample requests
```

If that is what you see, you are in the wrong folder. Every command below runs inside `v2`:

```bash
cd hr-agentica/v2
```

## 1. What you need to install (and what you don't)

| Piece | Do you install it yourself? | What it is |
| --- | --- | --- |
| Python 3.14 + a venv | **Yes**, once | Runs everything |
| LangGraph, LangChain core | No, `pip` installs them | The agent workflow (a library) |
| LiteLLM | No, `pip` installs it | A library that talks to Claude or Ollama. No server, no proxy needed |
| Chroma | No, `pip` installs it | Vector database embedded in the app; data lives in `var/vectors/`. No server |
| MiniLM embedding model (about 80 MB) | No, downloaded automatically the first time you index | Turns text into vectors for RAG. If it cannot be downloaded, the app uses a simpler built-in embedding (`HRAI_EMBEDDINGS=hash`) |
| Ollama | Optional | A free local model. Without it (and without a key) the agents use the **offline planner**: rules plus the same tools, so every step below still works |
| Claude API key | Optional | Model-written answers; put `ANTHROPIC_API_KEY=...` in `.env` |

Without Ollama or a key, everything below works: RAG search, the CAG cache, KAG rules and graph, MAG memory, the
web portal and all the tests. What changes with a model is only who writes the final sentence of an answer: the
model instead of the offline planner, from the same retrieved text.

## 2. Install (about 2 minutes, about 750 MB)

```bash
cd hr-agentica && git pull                  # get the latest main
cd v2
python3.14 --version                        # Python 3.14.x
python3.14 -m venv .venv                    # a venv for v2 (the one at the repository root is for v1)
source .venv/bin/activate                   # every new terminal starts with this
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env                        # leave it as it is for now
```

No `python3.14`? Run `curl -LsSf https://astral.sh/uv/install.sh | sh`, then `uv python install 3.14`, then use
`$(uv python find 3.14)` in place of `python3.14`.

Tip: the repository is under `/mnt/c/...` (the Windows drive), which WSL reads slowly. It works, but a copy in your
Linux home (`cd ~ && git clone https://github.com/Ponmanisastha/hr-agentica`) runs noticeably faster.

## 3. Create the database, logins and sample data

```bash
python app.py init --demo-users
```

The first run downloads the embedding model, so it takes about 30 seconds. It prints the **admin** password and
three demo logins (`hr_demo`, `manager_demo`, `deepa`) once, so save them. Then:

```
Vectors: {'policy': 9, 'faq': 5, 'resumes': 6, 'embedder': 'minilm'}  Knowledge graph triples: 51
```

`embedder: minilm` means the real embedding model loaded; `hash` means it fell back (fine for testing).

```bash
python app.py samples load
python app.py user add vignesh employee --employee E108     # asks for a password (10+ characters)
```

```
Employees added: 12  past leave records: 25  openings: JOB-102, JOB-103  salaries: 12
Policy documents copied: 11  resumes copied to the inbox: 10
Indexed 67 policy sections; knowledge graph has 139 facts.
```

**67 policy sections** is the number to check. If you set up before this fix and see several hundred, run
`git pull` and then `python app.py index`. An empty `HRAI_POLICY_DIR=` line in `.env` used to make the app index
the whole project folder as policies.

## 4. RAG: search the policy documents

**What it is.** Each policy file in `policies/` is split into sections, and each section is stored in Chroma as a
vector. A question is turned into a vector too, and the closest sections come back with their score, file and
section. That is what the agent reads before answering.

```bash
python app.py knowledge rag "How many sick days do I get?"
python app.py knowledge rag "Can I claim hotel for a client visit?"
```

```
0.813  Leave Policy 2026.pdf, section 2. Sick leave
       Employees get 8 days of paid sick leave per year. Sick leave needs no advance notice, ...
0.537  Leave Policy 2026.pdf, section 11. Leave encashment
...
0.494  Travel and Expense Policy.docx, section 3. Hotel stay
       Hotel stays are allowed up to 6,000 rupees a night in metro cities and up to 4,000 rupees a night elsewhere, ...
```

The top result is the right section, and the source is the file you can open. **Web portal:** ask the same
question on **Assistant** and open **How I answered**: `search_policy` lists the sections it used.

## 5. CAG: the whole handbook in the prompt, plus cached answers (with RAG when not cached)

**What it is.** Two things:

1. **Context.** When all policy text is small (under 40,000 characters; yours is about 15,600), the agent gives the
   model all of it instead of a few RAG sections, so nothing relevant is missed. Bigger than that, it uses RAG.
2. **Answer cache.** A general question ("how many sick days do we get?") is answered once, and the answer is
   saved. The next time anyone asks it, the saved answer is returned with no search and no model call. The cache
   is emptied automatically when any policy changes. Personal questions ("my", "I", an employee ID, a date) are
   never cached.

So the order is: **cache hit → return it; cache miss → retrieve (CAG or RAG) → answer → save it in the cache.**

```bash
python app.py knowledge cag "How many sick days do we get?"
```

```
Strategy: CAG  (all policy text is 15,631 characters; CAG is used up to HRAI_CAG_MAX_CHARS=40,000)
Knowledge-base version: 65ba92a2fe8a4add  (cached answers are dropped when it changes)
Cached answer for this question: none yet (ask it once)
```

Ask it twice:

```bash
python app.py ask "How many sick days do we get?"     # 1st time: not cached, so it retrieves
python app.py ask "How many sick days do we get?"     # 2nd time: from the cache
```

```
AGENT: policy   MODE: rules   TOOL CALLS: 3
  - [policy] no_model_using_rules()
  - [policy] search_policy(query=How many sick days do we get?, k=1)      <- cache miss: RAG search
  - [policy] kg_facts(question=How many sick days do we get?)
...
AGENT: policy   MODE: cache   TOOL CALLS: 1
  - [policy] answer_cache()                                               <- cache hit
```

Then `python app.py knowledge cag "How many sick days do we get?"` shows the cached answer and how often it was
reused.

**Try more:**

```bash
python app.py knowledge cag "How many sick days do I have left?"     # "Personal questions ... are never cached."
HRAI_CAG_MAX_CHARS=5000 python app.py knowledge cag "sick leave"     # "Strategy: RAG": too big for CAG, uses RAG
```

To see the cache emptied by a policy change, upload a changed policy and approve it (GUIDE.md section 23). The
knowledge-base version changes, and the next `ask` shows `search_policy` again.

**Web portal:** ask the same question twice on **Assistant**. The second answer's **How I answered** says
"cached answer (CAG)".

## 6. KAG: the knowledge graph (rules and people)

**What it is.** Facts stored as subject, relation, object triples. The leave rules are read from the policy
documents (with the file and section each came from), and the reporting lines come from the employee records. The
leave engine uses these numbers; the agent uses the facts to answer "who approves..." questions exactly.

```bash
python app.py knowledge kag rules
```

```
  Annual leave: days a year                         18   Leave Policy 2026.pdf, section 1. Annual leave
  Annual leave: days' notice                         7   Leave Policy 2026.pdf, section 1. Annual leave
  ...
  Work from home: days a week                        2   Work From Home Policy.md, section 1. Work from home eligibility
```

```bash
python app.py knowledge kag "Who approves leave for Karthik Subramanian?"
python app.py ask "Who approves leave for Karthik Subramanian?"
```

```
  Karthik Subramanian --reports_to--> Arun Kumar   [employees]
  Arun Kumar --approves_leave_for--> Karthik Subramanian   [employees]
```

**Web portal:** **Policies → Rules read from these documents** lists the same rules and sources. Change a number in
a policy, upload it and approve it: the rule and its source change there and in `kag rules`.

## 7. MAG: memory, short-term and long-term

**What it is.**

- **Short-term:** the current conversation. Follow-ups such as "and sick leave?" or "what did I ask you?" are
  answered from the earlier turns. A new conversation starts empty.
- **Long-term:** every turn is saved for that user (in SQLite and Chroma) and survives restarts. Before answering,
  the agent recalls the user's most relevant and most recent memories. Users never see each other's memories.
  With a model, it also saves lasting preferences ("prefers email").

**Short-term, on the command line:** `ask` is a new conversation every time, so use `chat`:

```bash
python app.py chat
```

```
you> How many casual leave days do we get?
RESULT: Employees get 6 days of casual leave per year ...
you> and sick leave?
REQUEST: and sick leave?
  - [policy] search_policy(query=How many sick leave days do we get?, k=1)     <- the follow-up was understood
RESULT: Employees get 8 days of paid sick leave per year ...
you> What did I ask you?
AGENT: policy   MODE: memory
RESULT: You asked: "How many sick leave days do we get?", and before that: "How many casual leave days do we get?".
you> exit
```

Run `python app.py chat` again and ask "What did I ask you?". It answers "This is the first thing you have asked
me in this conversation." because short-term memory belongs to one conversation.

**Long-term, on the command line:** command-line requests run as `cli:<your Linux user name>` (run `whoami`), or
as a login with `--as vignesh`.

```bash
python app.py knowledge mag cli:$(whoami)                          # latest memories, newest first
python app.py knowledge mag cli:$(whoami) "medical certificate"    # what would be recalled for this question
python app.py chat --as vignesh                                    # memories saved under vignesh
python app.py knowledge mag vignesh
```

```
  2026-10-06T12:08:56  [interaction] [policy] asked: How many sick leave days do we get? | answered: Employees get 8 days ...
Long-term memories cli:ponmani brings to: medical certificate
  - [policy] asked: How many sick days do we get? | answered: Employees get 8 days of paid sick leave per year ...
```

These memories are still there after you close the terminal or restart the server.

**Web portal:** `python app.py serve`, open http://localhost:8000, sign in as `vignesh`, and on **Assistant** ask
the three `chat` questions above. **New conversation** clears the short-term memory; the long-term memories stay
(check with `knowledge mag vignesh`).

## 8. Run the automated tests

```bash
python -m unittest discover -s tests -t .
```

```
Ran 115 tests in 38s
OK
```

They run offline in a temporary folder and do not touch your database.

## 9. Optional: add a model

**Ollama (free, local):**

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1:8b        # about 5 GB
ollama serve                   # if it is not already running
python app.py ask "How many sick days do we get?"
```

`MODE: llm:ollama_chat/llama3.1:8b` shows the local model wrote the answer. With `HRAI_MODE=auto` (the default), the
app checks for Ollama on every run, so nothing else changes.

**Claude:** put `ANTHROPIC_API_KEY=sk-ant-...` in `.env`. `MODE` then names the Claude model. Spending is tracked
per agent (`python app.py budget`).

Either way, the four layers work exactly as above. With a model, CAG also means the whole handbook goes into the
prompt, which is cached by the provider so repeat questions cost less.
