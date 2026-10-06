"""Voice calls: telephony adapters plus the tools the voice agent uses.

The default SimulatedTelephony plays each call against data/voice_simulation.json, so no
real phone call is ever placed. TwilioTelephony shows where a real provider plugs in; it
is off unless HR_VOICE_PROVIDER=twilio and HR_VOICE_LIVE=1 are both set, and it has not
been tested against a live Twilio account.

Interview changes are never applied directly: reschedules are written as proposals that
wait for HR approval in outputs/voice/reschedule_proposals.json.
"""

import json
import os
import re
from datetime import date, datetime, timedelta

from . import tools as T

VOICE_OUT = "voice"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


# ---------------------------------------------------------------- dates

def business_day(offset):
    """The date `offset` business days from today (negative = past), skipping weekends."""
    d, step, left = T.today(), (1 if offset >= 0 else -1), abs(offset)
    while left:
        d += timedelta(days=step)
        if d.weekday() < 5:
            left -= 1
    return d


def spoken(d, time):
    return f"{d:%A} {d.day} {MONTHS[d.month - 1]} at {time}"


def parse_spoken_slots(text):
    """Pull 'Friday 9 October at 10:00' style slots out of what a caller said."""
    slots = []
    for day, month, hhmm in re.findall(r"(\d{1,2}) (" + "|".join(MONTHS) + r") at (\d{1,2}:\d{2})", text or ""):
        m = MONTHS.index(month) + 1
        y = T.today().year + (1 if m < T.today().month else 0)
        slots.append({"date": date(y, m, int(day)).isoformat(), "time": hhmm})
    return slots


# ---------------------------------------------------------------- telephony adapters

class SimulatedTelephony:
    """Answers calls from data/voice_simulation.json. Nothing leaves the machine."""

    name = "simulated"

    def __init__(self):
        self.script = T._load("voice_simulation.json")

    def call(self, to_phone, message, context_id=""):
        person = self.script.get(to_phone, {})
        reply = person.get(context_id, person.get("*")) if person else None
        if reply:
            reply = re.sub(r"\{slot:(-?\d+),(\d{1,2}:\d{2})\}",
                           lambda m: spoken(business_day(int(m.group(1))), m.group(2)), reply)
        return {"status": "answered" if reply else "no_answer", "reply": reply}


class TwilioTelephony:
    """Real calls through Twilio (untested sketch).

    Outbound: Twilio speaks `message` and gathers the callee's speech; Twilio then posts the
    transcript to PUBLIC_BASE_URL/voice/gather, which app.py hands to the voice agent.
    Needs: pip install twilio, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, PUBLIC_BASE_URL.
    """

    name = "twilio"

    def __init__(self):
        from twilio.rest import Client  # imported here so the prototype runs without twilio
        self.client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        self.from_number = os.environ["TWILIO_FROM_NUMBER"]
        self.base = os.environ["PUBLIC_BASE_URL"].rstrip("/")

    def call(self, to_phone, message, context_id=""):
        from xml.sax.saxutils import escape
        twiml = (f'<Response><Gather input="speech" action="{self.base}/voice/gather?ctx={context_id}" '
                 f'speechTimeout="auto"><Say>{escape(message)}</Say></Gather></Response>')
        call = self.client.calls.create(to=to_phone, from_=self.from_number, twiml=twiml)
        # The reply arrives later on the webhook, so the agent records the call as pending.
        return {"status": "placed", "reply": None, "provider_call_id": call.sid}


def telephony():
    if os.environ.get("HR_VOICE_PROVIDER") == "twilio" and os.environ.get("HR_VOICE_LIVE") == "1":
        return TwilioTelephony()
    return SimulatedTelephony()


# ---------------------------------------------------------------- state files

def _log_path(name):
    path = T.OUT / VOICE_OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append(name, entry):
    path = _log_path(name)
    log = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    log.append({**entry, "recorded_at": datetime.now().isoformat(timespec="seconds")})
    path.write_text(json.dumps(log, indent=2), encoding="utf-8")
    return str(path.relative_to(T.ROOT))


def _interviews():
    data = T._load("interviews.json")
    out = []
    for i in data["interviews"]:
        d = business_day(i["day_offset"])
        out.append({k: v for k, v in i.items() if k != "day_offset"} | {"date": d.isoformat(), "when_spoken": spoken(d, i["time"])})
    return out


# ---------------------------------------------------------------- tools

def list_interviews(which="upcoming", days_ahead=3):
    """Upcoming interviews in the next N business days, or past ones (attended or no-show) to follow up."""
    horizon = business_day(days_ahead).isoformat()
    today = T.today().isoformat()
    rows = _interviews()
    if which == "past":
        rows = [i for i in rows if i["date"] < today]
    else:
        rows = [i for i in rows if today <= i["date"] <= horizon and i["status"] == "scheduled"]
    return {"interviews": rows}


def place_call(to_phone, to_name, message, context_id=""):
    """Call someone, say `message`, and return what they said back (or no_answer)."""
    tel = telephony()
    result = tel.call(to_phone, message, context_id)
    call_id = f"CALL-{datetime.now():%H%M%S}-{abs(hash((to_phone, context_id, message))) % 1000:03d}"
    lines = [f"Call {call_id} to {to_name} ({to_phone}) via {tel.name}, about {context_id or 'general'}",
             f"AGENT: {message}",
             f"{to_name.upper()}: {result['reply']}" if result["reply"] else f"[{result['status']}]"]
    path = _log_path(f"transcripts/{call_id}.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _append("call_log.json", {"call_id": call_id, "to": to_name, "phone": to_phone, "context": context_id,
                              "status": result["status"], "reply": result["reply"]})
    return {"call_id": call_id, "status": result["status"], "reply": result["reply"]}


def end_call(call_id, closing_message):
    """Say a closing line on an answered call (recorded in its transcript)."""
    path = _log_path(f"transcripts/{call_id}.txt")
    if not path.exists():
        return {"error": f"No call {call_id}"}
    with path.open("a", encoding="utf-8") as f:
        f.write(f"AGENT: {closing_message}\n")
    return {"call_id": call_id, "ended": True}


def classify_reply(reply):
    """Rule-based reading of a reply: intent plus any date slots the person mentioned."""
    if not reply:
        return {"intent": "no_answer", "slots_mentioned": []}
    t = reply.lower()
    slots = parse_spoken_slots(reply)
    if re.search(r"can't make|cannot|clash|move it|reschedul|another slot|travelling|not available", t):
        intent = "reschedule"
    elif re.search(r"withdraw|not interested|accepted another|no longer", t):
        intent = "decline"
    elif "?" in reply:
        intent = "question"
    elif re.search(r"\byes\b|confirm|i'll be there|fine|sure", t):
        intent = "confirm"
    else:
        intent = "unclear"
    return {"intent": intent, "slots_mentioned": slots}


def find_common_slot(interviewer, candidate_slots=None, after_date=""):
    """First interviewer free slot that matches the candidate's slots (or any free slot after a date)."""
    free = T._load("interviews.json")["interviewer_free_slots"].get(interviewer)
    if free is None:
        return {"error": f"No calendar for {interviewer}"}
    # Slots already offered in a pending proposal for this interviewer are held, so nobody is double-booked.
    mine = {i["id"] for i in _interviews() if i["interviewer"] == interviewer}
    path = _log_path("reschedule_proposals.json")
    held = {(p["new_date"], p["new_time"]) for p in (json.loads(path.read_text(encoding="utf-8")) if path.exists() else [])
            if p["interview_id"] in mine and p["status"] == "awaiting HR approval"}
    free = [{"date": business_day(o).isoformat(), "time": t} for o, t in free]
    free = [s for s in free if (s["date"], s["time"]) not in held]
    if candidate_slots:
        wanted = {(s["date"], s["time"]) for s in candidate_slots}
        match = [s for s in free if (s["date"], s["time"]) in wanted]
    else:
        match = [s for s in free if s["date"] > after_date]
    if not match:
        return {"found": False, "interviewer_free": free}
    s = match[0]
    return {"found": True, "date": s["date"], "time": s["time"],
            "spoken": spoken(date.fromisoformat(s["date"]), s["time"]), "needs_candidate_confirmation": not candidate_slots}


def propose_reschedule(interview_id, new_date, new_time, requested_by, reason):
    """Record a reschedule proposal for HR approval. The interview itself is not changed."""
    saved = _append("reschedule_proposals.json", {"interview_id": interview_id, "new_date": new_date, "new_time": new_time,
                                                   "requested_by": requested_by, "reason": reason,
                                                   "status": "awaiting HR approval"})
    return {"proposal_saved_to": saved, "status": "awaiting HR approval"}


def update_interview_status(interview_id, party, status, note=""):
    """Log a confirmation, no-answer or withdrawal for one party of an interview."""
    return {"saved_to": _append("interview_status.json", {"interview_id": interview_id, "party": party,
                                                           "status": status, "note": note})}


def schedule_retry_call(interview_id, to_name, to_phone, after_hours=2):
    when = (datetime.now() + timedelta(hours=after_hours)).isoformat(timespec="minutes")
    return {"saved_to": _append("retry_queue.json", {"interview_id": interview_id, "to": to_name,
                                                      "phone": to_phone, "retry_at": when}), "retry_at": when}


def search_faq(query):
    """Search the candidate FAQ (results timeline, format, rescheduling, travel)."""
    text = (T.DATA / "candidate_faq.md").read_text(encoding="utf-8")
    sections = [{"section": p.split("\n", 1)[0].strip(), "text": p.split("\n", 1)[1].strip()}
                for p in re.split(r"^## ", text, flags=re.M)[1:]]
    words = [w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 3]
    scored = sorted(((sum((s["section"] + " " + s["text"]).lower().count(w) for w in words), s) for s in sections),
                    key=lambda x: -x[0])
    return {"results": [s for n, s in scored[:2] if n]}


def log_inbound_call(caller, question, answer):
    """Record an inbound call's question and the spoken answer."""
    call_id = f"IN-{datetime.now():%H%M%S}-{abs(hash((caller, question))) % 1000:03d}"
    _log_path(f"transcripts/{call_id}.txt").write_text(
        f"Inbound call {call_id} from {caller}\n{caller.upper()}: {question}\nAGENT: {answer}\n", encoding="utf-8")
    _append("call_log.json", {"call_id": call_id, "to": "inbound", "phone": caller, "context": "query",
                              "status": "answered", "reply": question})
    return {"call_id": call_id}


S = {"type": "string"}
SLOT = {"type": "object", "properties": {"date": S, "time": S}, "required": ["date", "time"]}
VOICE_TOOLS = {
    "list_interviews": (list_interviews, "List upcoming interviews (which='upcoming') in the next days_ahead business days, or past ones (which='past') to follow up.",
                        T._schema({"which": {"type": "string", "enum": ["upcoming", "past"]}, "days_ahead": {"type": "integer"}}, ["which"])),
    "place_call": (place_call, "Phone a person, speak the message, and get their spoken reply (status answered or no_answer). context_id is the interview id or a short topic.",
                   T._schema({"to_phone": S, "to_name": S, "message": S, "context_id": S}, ["to_phone", "to_name", "message", "context_id"])),
    "end_call": (end_call, "Speak a closing line on an answered call.", T._schema({"call_id": S, "closing_message": S}, ["call_id", "closing_message"])),
    "classify_reply": (classify_reply, "Rule-based helper: intent of a caller's reply and any date slots they mentioned.", T._schema({"reply": S}, ["reply"])),
    "find_common_slot": (find_common_slot, "Find an interviewer free slot that matches the candidate's offered slots, or the first free slot after a date.",
                         T._schema({"interviewer": S, "candidate_slots": {"type": "array", "items": SLOT}, "after_date": S}, ["interviewer"])),
    "propose_reschedule": (propose_reschedule, "Record a reschedule proposal for HR approval (does not change the interview).",
                           T._schema({"interview_id": S, "new_date": S, "new_time": S, "requested_by": S, "reason": S},
                                     ["interview_id", "new_date", "new_time", "requested_by", "reason"])),
    "update_interview_status": (update_interview_status, "Log a party's status for an interview: confirmed, no_answer, wants_reschedule, withdrawn, attended_followed_up.",
                                T._schema({"interview_id": S, "party": S, "status": S, "note": S}, ["interview_id", "party", "status"])),
    "schedule_retry_call": (schedule_retry_call, "Queue another call attempt after N hours.",
                            T._schema({"interview_id": S, "to_name": S, "to_phone": S, "after_hours": {"type": "integer"}}, ["interview_id", "to_name", "to_phone"])),
    "log_inbound_call": (log_inbound_call, "Record an inbound call: who called, what they asked, and the answer spoken back.",
                         T._schema({"caller": S, "question": S, "answer": S}, ["caller", "question", "answer"])),
    "search_faq": (search_faq, "Search the candidate FAQ to answer a candidate's question.", T._schema({"query": S}, ["query"])),
}
T.TOOLS.update(VOICE_TOOLS)


def reset_state():
    """Clear the voice logs and proposals (used by the demo so each run starts fresh)."""
    for name in ("call_log.json", "interview_status.json", "reschedule_proposals.json", "retry_queue.json"):
        _log_path(name).unlink(missing_ok=True)
    for f in (T.OUT / VOICE_OUT / "transcripts").glob("*.txt"):
        f.unlink()
