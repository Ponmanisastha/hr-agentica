"""Self-test: runs every agent offline and checks what each one produced.

    python test_prototype.py

Uses mock mode and a fixed date, so results are the same on every machine.
"""

import json
import os
import sys

os.environ["HR_AGENT_MODE"] = "mock"
os.environ.setdefault("HR_DEMO_TODAY", "2026-10-06")

from hr_agents import tools as T, voice  # noqa: E402
from hr_agents.agents import handle, route  # noqa: E402

results = []


def check(name, condition, detail=""):
    results.append(condition)
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not condition else ""))


def main():
    voice.reset_state()
    (T.OUT / "leave_decisions.json").unlink(missing_ok=True)

    print("\nRouting")
    check("resume request goes to screening", route("Screen resumes for Backend Engineer") == "screening")
    check("new hire request goes to onboarding", route("Onboard Vikram Singh") == "onboarding")
    check("leave request goes to policy", route("E101 wants annual leave from 2026-10-20 to 2026-10-22") == "policy")
    check("call request goes to voice", route("Make reminder calls for interviews") == "voice")

    print("\n1. Resume screening")
    r = handle("Screen all resumes for the Backend Engineer opening")
    shortlist = (T.OUT / "shortlist_JOB-101.md").read_text(encoding="utf-8")
    check("6 resumes scored", r["result"].count("/100") == 6, r["result"])
    check("3 candidates shortlisted", shortlist.count("| shortlist |") == 3)
    check("candidate missing a must-have is declined", "Rahul Verma" in shortlist and "| decline |" in shortlist)
    check("6 candidate emails drafted", sum(1 for s in r["trace"] if s["tool"] == "draft_email") == 6)

    print("\n2. Onboarding")
    r = handle("Onboard our new hires Priya Raman and Vikram Singh")
    check("Priya has all documents", "Priya Raman (NH-201)" in r["result"] and "all documents received" in r["result"])
    check("Vikram has 6 documents missing", "6 documents missing" in r["result"], r["result"])
    plan = (T.OUT / "onboarding_NH-202.md").read_text(encoding="utf-8")
    check("payroll is blocked for Vikram", "blocked: missing documents" in plan)

    print("\n3. Leave and policy")
    r = handle("Employee E101 wants annual leave from 2026-10-20 to 2026-10-22.")
    check("short leave with notice is approved", "is approved" in r["result"], r["result"])
    check("public holiday is not counted (2 working days)", "2 working days" in r["result"], r["result"])
    r = handle("E102 is asking for annual leave from 2026-10-09 to 2026-10-15.")
    check("low balance and short notice go to the manager", "sent to Arun Kumar" in r["result"], r["result"])
    r = handle("How many days of maternity leave do we offer?")
    check("policy answer cites its section", "26 weeks" in r["result"] and "section" in r["result"])
    ledger = json.loads((T.OUT / "leave_decisions.json").read_text())
    check("both leave decisions recorded", len(ledger) == 2)

    print("\n4. Voice calls")
    r = handle("Make reminder calls for the interviews in the next 3 days.")
    check("6 reminder calls handled", "6 calls handled" in r["result"], r["result"])
    check("Anita confirmed", "INT-301 Anita Sharma (candidate): confirmed" in r["result"])
    check("Fatima's no-answer queues a retry", "no answer; retry queued" in r["result"])
    check("Meera's clash becomes a proposal", "INT-302 Meera Iyer (candidate): asked to reschedule; proposed" in r["result"])
    r = handle("Make follow-up calls to candidates from yesterday's interviews.")
    check("Sneha's question answered from the FAQ", "answered from the FAQ" in r["result"])
    proposals = json.loads((T.OUT / "voice" / "reschedule_proposals.json").read_text())
    check("3 proposals, all awaiting HR approval", len(proposals) == 3 and all(p["status"] == "awaiting HR approval" for p in proposals))
    slots = [(p["new_date"], p["new_time"]) for p in proposals]
    check("no interviewer slot is proposed twice", len(slots) == len(set(slots)), str(slots))
    r = handle("Inbound call from E101: how many sick days do I have left, and do I need a medical certificate?")
    check("inbound call gets a spoken answer with the balance", "6 sick leave days left" in r["result"], r["result"])
    transcripts = list((T.OUT / "voice" / "transcripts").glob("*.txt"))
    check("a transcript is saved for every call", len(transcripts) == 9, f"{len(transcripts)} found")

    print("\n5. Safety")
    outbox = list((T.OUT / "outbox").glob("*.txt"))
    check("every email is a draft", outbox and all("DRAFT" in f.read_text(encoding="utf-8") for f in outbox))
    check("calls use the simulator, not a real phone line", voice.telephony().name == "simulated")

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
