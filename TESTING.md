# How to test the HR agentic AI prototype

Allow about 20 minutes. Everything works offline; only step 7 needs an API key.

## 1. Set up (once)

You need Python 3.9 or newer.

**Windows**
1. Install Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during setup.
2. Unzip `hr-agentic-prototype.zip`, open the `hr-agentic-prototype` folder, click the address bar, type `cmd` and press Enter.
3. Check Python: `python --version`

**Mac**
1. Open Terminal. Check Python: `python3 --version`. If it is missing, install it from https://www.python.org/downloads/.
2. Unzip `hr-agentic-prototype.zip` and go into the folder: `cd ~/Downloads/hr-agentic-prototype`

On a Mac, type `python3` wherever this guide says `python`.

## 2. Run the self-test

```
python test_prototype.py
```

Expected: `27 of 27 checks passed.` It covers routing, all four agents and the safety rules. If any line says FAIL, send that line.

## 3. Resume screening

```
python app.py ask "Screen all resumes for the Backend Engineer opening"
```

Check:
- 6 candidates are scored; Anita, Meera and Fatima are shortlisted.
- `outputs/shortlist_JOB-101.md` holds the ranked table.
- `outputs/outbox/` holds 3 interview invites and 3 decline emails, each marked DRAFT.

Try your own: add a resume as a `.txt` file in `data/resumes/` (copy the format of an existing one) and run the command again.

## 4. Onboarding

```
python app.py ask "Onboard our new hires Priya Raman and Vikram Singh"
```

Check:
- Priya has every document; Vikram is missing 6, and his payroll task is marked blocked.
- `outputs/onboarding_NH-202.md` holds Vikram's dated checklist.
- `outputs/outbox/` holds a welcome email, a missing-documents reminder and an IT/manager task email.

Try your own: add a document name to Vikram's `documents_submitted` in `data/new_hires.json` and run it again.

## 5. Leave and policy questions

```
python app.py ask "How many days of maternity leave do we offer?"
python app.py ask "Inbound call from E101: how many sick days do I have left?"
python app.py ask "E101 wants annual leave from 2026-12-21 to 2026-12-23"
python app.py ask "E102 wants annual leave from 2026-12-21 to 2026-12-31"
```

Check:
- The maternity answer says 26 weeks and names the handbook section.
- E101's short leave is approved (if it starts at least 7 days from today).
- E102's request goes to the manager with the reasons (not enough balance).
- `outputs/leave_decisions.json` records each decision.

Change the dates to test the rules: under 7 days' notice, more than 5 working days, or casual leave over 2 days all go to the manager. Public holidays and weekends are not counted.

## 6. Voice calls (simulated, no real calls)

```
python app.py ask "Make reminder calls for the interviews in the next 3 days"
python app.py ask "Make follow-up calls to candidates from yesterday's interviews"
python app.py ask "Inbound call from E102: how many annual days do I have left?"
```

Check:
- Reminders: Anita, Arun and Ravi confirm; Fatima doesn't answer (a retry and an email are queued); Meera and Arun each ask to move an interview, and each becomes a proposal.
- Follow-ups: Sneha's question gets the FAQ answer; Arjun, who missed his interview, is offered a new slot.
- `outputs/voice/transcripts/` has one transcript per call; `outputs/voice/reschedule_proposals.json` shows every proposal as "awaiting HR approval", and no slot is proposed twice.

Try your own: edit what people say in `data/voice_simulation.json`. For example, change Anita's reply to "Sorry, I have a clash. I'm free on" plus a date and time in the same style as Meera's, then rerun the reminders. To clear old call logs, run `python app.py demo` (it resets them).

## 7. Web console

```
python app.py serve
```

Open http://localhost:8000. Click any sample request, or type your own, and press **Run agent**. Each result shows which agent handled it; open **Agent steps** to see every tool call and its output. Press Ctrl+C in the terminal to stop.

## 8. Claude mode (needs an API key)

So far the agents followed a scripted plan. With a key, Claude plans the steps itself and writes the replies.

1. Create a key at https://console.anthropic.com (API usage is billed to that account).
2. Install the SDK: `pip install -r requirements.txt` (Mac: `pip3`).
3. Set the key: on Windows `set ANTHROPIC_API_KEY=sk-ant-...`, on Mac `export ANTHROPIC_API_KEY=sk-ant-...`.
4. Run any command above, for example `python app.py demo`.

Check: each result says `mode: claude` instead of `mode: mock`. Results are worded differently each run, but should reach the same decisions. If a call to Claude fails, the agent finishes the job in mock mode and the trace shows the error.

To switch back to offline mode, open a new terminal window or set `HR_AGENT_MODE=mock`.

## What it never does

It never sends an email, places a real phone call, or changes an HR record. Emails are drafts in `outputs/outbox/`, and reschedules wait for HR approval. Real calls through Twilio are wired up but switched off and untested; see the Voice calls section of README.md.
