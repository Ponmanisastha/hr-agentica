# HR agentic AI prototype

AI agents that take over the most tedious HR tasks. **To test everything, follow [TESTING.md](TESTING.md).**

1. **Resume screening**: scores every resume against the job, ranks, shortlists, drafts invites and declines.
2. **Onboarding paperwork**: checks missing documents, builds a dated checklist, drafts welcome, reminder and IT/manager emails.
3. **Leave and policy queries**: answers policy questions with a cited section, and approves or routes leave requests by the handbook's rules.

4. **Voice calls** (added 2026-10-06): reminder calls to candidates and interviewers, follow-up calls after interviews, rescheduling by phone, and inbound HR query calls.

An orchestrator routes each plain-English request to the right agent.

## Voice calls

Calls are simulated by default: `data/voice_simulation.json` holds what each person says back, and no real call is placed. Try:

```
python app.py ask "Make reminder calls for the interviews in the next 3 days"
python app.py ask "Make follow-up calls to candidates from yesterday's interviews"
python app.py ask "Inbound call from E101: how many sick days do I have left?"
```

Reschedules are never applied directly: the agent finds a slot that suits both sides and records a proposal in `outputs/voice/reschedule_proposals.json` for HR to approve. Transcripts, the call log, statuses and the retry queue are in `outputs/voice/`.

Real calls (untested sketch): `hr_agents/voice.py` has a `TwilioTelephony` adapter and `app.py serve` exposes `/voice/inbound` and `/voice/gather` webhooks. It is off unless `HR_VOICE_PROVIDER=twilio` and `HR_VOICE_LIVE=1` are set, plus `pip install twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` and a public `PUBLIC_BASE_URL`. Check consent and calling-hours rules before enabling it.

## Run it

Needs Python 3.9+. No packages are needed for the offline (mock) mode.

```
python app.py demo                       # runs eight sample requests through all four agents
python test_prototype.py                 # self-test: checks every agent's results
python app.py ask "Onboard Vikram Singh"  # one request
python app.py serve                      # web console at http://localhost:8000
```

To run the agents on Claude instead of the scripted mock planner:

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python app.py demo
```

Optional settings: `HR_AGENT_MODEL` (default `claude-opus-5-5`), `HR_AGENT_MODE=mock` to force offline mode, `HR_DEMO_TODAY=2026-10-06` to pin the date used for leave-notice rules.

## Layout

| Path | What it is |
| --- | --- |
| `app.py` | CLI and web server |
| `hr_agents/agents.py` | Orchestrator and the four agents (Claude tool-use loop and mock planner) |
| `hr_agents/voice.py` | Voice tools and telephony adapters (simulated, Twilio) |
| `hr_agents/tools.py` | The tools agents call: scoring, document checks, plans, leave rules, policy search, email drafts |
| `data/` | Sample job, 6 resumes, 2 new hires, 3 employees, holidays, policy handbook, interviews, candidate FAQ, simulated call replies |
| `outputs/` | What the agents produce: shortlist, onboarding plans, leave ledger, `outbox/` email drafts |
| `web/index.html` | Web console |

Nothing is sent or changed in a real system: emails are drafts in `outputs/outbox`, and leave decisions go to `outputs/leave_decisions.json`.
