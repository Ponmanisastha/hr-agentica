"""The orchestrator and the three HR agents.

Each agent runs in one of two modes:
- claude: a tool-use loop on the Claude API (needs ANTHROPIC_API_KEY and the anthropic package).
- mock: a scripted plan that calls the same tools in a fixed order, so the prototype runs offline.
"""

import json
import os
import re

from . import tools as T
from . import voice  # noqa: F401  registers the voice tools in T.TOOLS

MODEL = os.environ.get("HR_AGENT_MODEL", "claude-opus-5-5")
MAX_TURNS = 20


def _client():
    if os.environ.get("HR_AGENT_MODE") == "mock" or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic.Anthropic()


def _create(client, effort, **kwargs):
    """One Messages API call, with server-side refusal fallback where the SDK supports it."""
    try:
        return client.beta.messages.create(
            model=MODEL, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": effort}, **kwargs)
    except TypeError:  # older SDK without the fallbacks parameter
        return client.messages.create(model=MODEL, output_config={"effort": effort}, **kwargs)


class Agent:
    name = ""
    tools: list = []
    system = ""

    def __init__(self):
        self.trace = []

    def call(self, tool, **args):
        result = T.run_tool(tool, args)
        self.trace.append({"tool": tool, "input": args, "output": result})
        return result

    def run(self, request):
        client = _client()
        if client is not None:
            try:
                return {"agent": self.name, "mode": "claude", "result": self._run_claude(client, request), "trace": self.trace}
            except Exception as exc:  # network or auth problem: still finish the job offline
                self.trace.append({"tool": "claude_api_error", "input": {}, "output": {"error": str(exc)}})
        return {"agent": self.name, "mode": "mock", "result": self.plan(request), "trace": self.trace}

    def _run_claude(self, client, request):
        system = self.system + f"\n\nToday's date is {T.today().isoformat()}."
        messages = [{"role": "user", "content": request}]
        for _ in range(MAX_TURNS):
            resp = _create(client, "medium", max_tokens=16000, system=system,
                           tools=T.tool_specs(self.tools), messages=messages)
            if resp.stop_reason == "refusal":
                return "The model declined this request; please handle it manually."
            messages.append({"role": "assistant", "content": resp.content})
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                return "\n".join(b.text for b in resp.content if b.type == "text").strip()
            results = []
            for c in calls:
                out = self.call(c.name, **c.input) if c.name in self.tools else {"error": f"Unknown tool {c.name}"}
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(out)})
            messages.append({"role": "user", "content": results})
        return "Stopped after too many steps; see the trace."

    def plan(self, request):
        raise NotImplementedError


# ---------------------------------------------------------------- 1. resume screening

class ScreeningAgent(Agent):
    name = "Resume screening agent"
    tools = ["list_job_openings", "list_resumes", "read_resume", "score_resume", "save_shortlist", "draft_email"]
    system = (
        "You are an HR resume-screening agent. For the job the user names (or the only open job), score every resume "
        "with score_resume, read borderline ones, then rank them. Shortlist the top candidates up to the job's "
        "shortlist_size, but only those who meet the minimum (all must-haves and minimum years). Mark the rest decline. "
        "Save the shortlist with save_shortlist, including a short note on any judgement calls. Draft an interview "
        "invitation for each shortlisted candidate and a polite decline for the others with draft_email. "
        "Never judge on name, gender, age, religion, or other protected traits. Finish with a 3-5 line summary for HR."
    )

    def plan(self, request):
        jobs = self.call("list_job_openings")["jobs"]
        job = next((j for j in jobs if j["id"].lower() in request.lower() or j["title"].lower() in request.lower()), jobs[0])
        files = self.call("list_resumes")["resumes"]
        scored = sorted((self.call("score_resume", file_name=f, job_id=job["id"]) for f in files),
                        key=lambda c: -c["score"])
        picks = [c for c in scored if c["meets_minimum"]][: job["shortlist_size"]]
        for c in scored:
            c["decision"] = "shortlist" if c in picks else "decline"
        self.call("save_shortlist", job_id=job["id"], ranked_candidates=scored,
                  notes="Scored by rules: 60 points must-haves, 24 nice-to-haves, 16 experience.")
        for c in scored:
            if c["decision"] == "shortlist":
                self.call("draft_email", to=c["email"], subject=f"Interview invitation: {job['title']}",
                          body=f"Dear {c['candidate']},\n\nThank you for applying for {job['title']}. We would like to "
                               "invite you to a 45-minute technical interview. Please reply with three slots that suit you "
                               "this week.\n\nRegards,\nTalent Acquisition")
            else:
                self.call("draft_email", to=c["email"], subject=f"Your application for {job['title']}",
                          body=f"Dear {c['candidate']},\n\nThank you for your interest in {job['title']}. After careful "
                               "review we will not be moving forward at this time. We will keep your profile for future "
                               "openings.\n\nRegards,\nTalent Acquisition")
        lines = [f"Screened {len(scored)} resumes for {job['title']} ({job['id']})."]
        lines += [f"{i}. {c['candidate']}: {c['score']}/100, {c['decision']}"
                  + (f" (missing {', '.join(c['must_have_missing'])})" if c["must_have_missing"] else "")
                  for i, c in enumerate(scored, 1)]
        lines.append(f"Shortlist saved to outputs/shortlist_{job['id']}.md; {len(scored)} email drafts are in outputs/outbox for review.")
        return "\n".join(lines)


# ---------------------------------------------------------------- 2. onboarding

class OnboardingAgent(Agent):
    name = "Onboarding agent"
    tools = ["get_new_hire", "check_documents", "create_onboarding_plan", "draft_email"]
    system = (
        "You are an HR onboarding agent. For each new hire the user names (by name or id), look them up, check their "
        "documents, and create the onboarding plan. Then draft: a welcome email to the hire; a reminder listing any "
        "missing documents; and one email to IT and the manager listing their tasks and due dates from the plan. "
        "If the user names no one, onboard every new hire. Finish with a short summary that calls out blocked tasks."
    )

    def plan(self, request):
        hires = T._load("new_hires.json")
        text = request.lower()
        named = [h for h in hires if h["id"].lower() in text or any(p.lower() in text for p in h["name"].split())]
        out = []
        for h in named or hires:
            hire = self.call("get_new_hire", hire_id_or_name=h["id"])
            docs = self.call("check_documents", hire_id=hire["id"])
            plan = self.call("create_onboarding_plan", hire_id=hire["id"])
            first = hire["name"].split()[0]
            self.call("draft_email", to=hire["email"], subject=f"Welcome to the team, {first}!",
                      body=f"Hi {first},\n\nWe are excited to have you join as {hire['role']} on {hire['start_date']}. "
                           f"Your manager will be {hire['manager']}. Day 1 starts at 9:30 with orientation.\n\nWarm regards,\nHR")
            if docs["missing"]:
                self.call("draft_email", to=hire["email"], subject="Pending documents before your joining date",
                          body=f"Hi {first},\n\nTo complete your onboarding, please upload: {', '.join(docs['missing'])}."
                               + (" Payroll cannot be set up until bank details and PAN are received." if docs["payroll_blocked"] else "")
                               + "\n\nThanks,\nHR")
            tasks = "\n".join(f"- {t['due']} {t['owner']}: {t['task']} ({t['status']})" for t in plan["tasks"])
            self.call("draft_email", to=f"it-helpdesk@example.com, {hire['manager']}",
                      subject=f"Onboarding tasks for {hire['name']} ({hire['start_date']})",
                      body=f"Please action the following for {hire['name']}:\n\n{tasks}\n\nHR")
            blocked = [t for t in plan["tasks"] if t["status"].startswith("blocked")]
            out.append(f"{hire['name']} ({hire['id']}), starts {hire['start_date']}: {len(plan['tasks'])} tasks planned, "
                       + (f"{len(docs['missing'])} documents missing ({', '.join(docs['missing'])}), {len(blocked)} blocked task(s)."
                          if docs["missing"] else "all documents received, nothing blocked."))
        out.append("Plans saved in outputs/onboarding_*.md; email drafts in outputs/outbox.")
        return "\n".join(out)


# ---------------------------------------------------------------- 3. leave and policy queries

class LeavePolicyAgent(Agent):
    name = "Leave and policy agent"
    tools = ["search_policy", "get_employee", "count_leave_days", "evaluate_leave_request", "record_leave_decision", "draft_email"]
    system = (
        "You are an HR helpdesk agent for leave requests and policy questions. For a policy question, search the "
        "handbook and answer in 2-4 sentences, citing the section title; if the handbook does not cover it, say so and "
        "suggest contacting HR. For a leave request, you need the employee id, leave type and dates; call "
        "evaluate_leave_request, record the result with record_leave_decision, and draft an email to the employee with "
        "the outcome (and to the manager when it is routed to them). Never approve anything the rules did not approve."
    )

    def plan(self, request):
        emp = re.search(r"\bE\d{3}\b", request, re.I)
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", request)
        if not (emp and dates):
            hits = self.call("search_policy", query=request)["results"]
            if not hits:
                return "The handbook does not cover this. Please contact HR at hr@example.com."
            top = hits[0]
            return f"{top['text']}\n\nSource: Policy handbook, section \"{top['section']}\"."
        lt = next((t for t in ("sick", "casual", "annual") if t in request.lower()), "annual")
        start, end = dates[0], dates[-1]
        self.call("search_policy", query=f"{lt} leave")
        decision = self.call("evaluate_leave_request", employee_id=emp.group(0).upper(), leave_type=lt,
                             start_date=start, end_date=end)
        if "error" in decision:
            return decision["error"]
        self.call("record_leave_decision", decision=decision)
        first = decision["employee"].split()[0]
        if decision["decision"] == "approve":
            msg = (f"Your {lt} leave from {start} to {end} ({decision['working_days']} working days) is approved. "
                   f"Remaining {lt} balance: {decision['balance_after']} days. {decision['note']}").strip()
        else:
            msg = (f"Your {lt} leave request from {start} to {end} has been sent to {decision['manager']} for approval "
                   f"because: {'; '.join(decision['reasons'])}.")
            self.call("draft_email", to=decision["manager_email"], subject=f"Leave approval needed: {decision['employee']}",
                      body=f"{decision['employee']} requested {lt} leave {start} to {end} ({decision['working_days']} working days). "
                           f"Auto-approval was not possible: {'; '.join(decision['reasons'])}. Please approve or decline in the HRMS.")
        self.call("draft_email", to=decision["employee_email"], subject=f"Your {lt} leave request",
                  body=f"Hi {first},\n\n{msg}\n\nHR Helpdesk")
        return msg


# ---------------------------------------------------------------- 4. voice calls

class VoiceAgent(Agent):
    name = "Voice call agent"
    tools = ["list_interviews", "place_call", "end_call", "classify_reply", "find_common_slot", "propose_reschedule",
             "update_interview_status", "schedule_retry_call", "search_faq", "log_inbound_call",
             "search_policy", "get_employee", "draft_email"]
    system = (
        "You are the HR voice assistant. You speak to people by phone through place_call, so keep every spoken "
        "message under 45 words, warm and plain. Three jobs:\n"
        "1. Reminder calls: list upcoming interviews and call the candidate and then the interviewer. Ask them to "
        "confirm. Confirmed: log it and close politely. No answer: log it, queue a retry, and draft a reminder email. "
        "Wants to reschedule: find a common slot with find_common_slot (use the dates they offered), record it with "
        "propose_reschedule, and tell them HR will confirm. Never promise a new time as final. Questions: answer from "
        "search_faq. Withdrawal: log it and draft an email to HR.\n"
        "2. Follow-up calls: list past interviews. Thank attendees, answer their questions from search_faq and log the "
        "follow-up. For no-shows, check they are fine and offer another slot, handled like a reschedule.\n"
        "3. Inbound query calls: answer the caller's HR question from search_policy (and get_employee for balances), "
        "then record it with log_inbound_call.\n"
        "End with a short summary for HR listing every proposal awaiting their approval."
    )

    def plan(self, request):
        t = request.lower()
        if re.search(r"inbound|call from|caller|calling in", t):
            return self._inbound(request)
        if re.search(r"follow", t):
            return self._follow_up()
        return self._reminders()

    def _handle_reply(self, iv, party, call, out):
        name, phone = (iv["candidate"], iv["candidate_phone"]) if party == "candidate" else (iv["interviewer"], iv["interviewer_phone"])
        first = name.split()[0]
        c = self.call("classify_reply", reply=call["reply"] or "")
        if c["intent"] == "no_answer":
            self.call("update_interview_status", interview_id=iv["id"], party=party, status="no_answer")
            retry = self.call("schedule_retry_call", interview_id=iv["id"], to_name=name, to_phone=phone)
            if party == "candidate":
                self.call("draft_email", to=iv["candidate_email"], subject=f"Reminder: your interview on {iv['when_spoken']}",
                          body=f"Hi {first},\n\nWe tried calling to remind you about your interview on {iv['when_spoken']} "
                               f"({iv['mode']}). Please reply to confirm, or let us know if you need another time.\n\nTalent Acquisition")
            out.append(f"{iv['id']} {name} ({party}): no answer; retry queued for {retry['retry_at']} and reminder email drafted.")
        elif c["intent"] == "confirm":
            self.call("update_interview_status", interview_id=iv["id"], party=party, status="confirmed")
            self.call("end_call", call_id=call["call_id"], closing_message=f"Thank you, {first}. See you then.")
            out.append(f"{iv['id']} {name} ({party}): confirmed.")
        elif c["intent"] == "reschedule":
            if party == "candidate":
                slot = self.call("find_common_slot", interviewer=iv["interviewer"], candidate_slots=c["slots_mentioned"])
            else:
                slot = self.call("find_common_slot", interviewer=iv["interviewer"], after_date=iv["date"])
            self.call("update_interview_status", interview_id=iv["id"], party=party, status="wants_reschedule", note=call["reply"])
            if slot.get("found"):
                self.call("propose_reschedule", interview_id=iv["id"], new_date=slot["date"], new_time=slot["time"],
                          requested_by=f"{name} ({party})", reason=call["reply"])
                if party == "candidate":
                    close = (f"Thanks, {first}. {iv['interviewer']} is free on {slot['spoken']}. I've passed that to HR "
                             "to approve, and you'll get a confirmation by email.")
                else:
                    close = (f"Thanks, {first}. Your next free slot is {slot['spoken']}. I've passed that to HR to "
                             "approve, and we'll check it with the candidate.")
                extra = "; the candidate still needs to agree once HR approves" if slot["needs_candidate_confirmation"] else ""
                out.append(f"{iv['id']} {name} ({party}): asked to reschedule; proposed {slot['spoken']}, awaiting HR approval{extra}.")
            else:
                close = f"Thanks, {first}. I'll ask HR to find another time and we'll get back to you."
                out.append(f"{iv['id']} {name} ({party}): asked to reschedule; no common slot found, HR to arrange.")
            self.call("end_call", call_id=call["call_id"], closing_message=close)
        elif c["intent"] == "question":
            faq = self.call("search_faq", query=call["reply"])["results"]
            answer = faq[0]["text"] if faq else "I'll check with the recruiter and they'll call you back."
            self.call("end_call", call_id=call["call_id"], closing_message=answer)
            self.call("update_interview_status", interview_id=iv["id"], party=party, status="question_answered", note=call["reply"])
            out.append(f"{iv['id']} {name} ({party}): asked \"{call['reply']}\"; answered from the FAQ.")
        elif c["intent"] == "decline":
            self.call("update_interview_status", interview_id=iv["id"], party=party, status="withdrawn", note=call["reply"])
            self.call("draft_email", to="talent@example.com", subject=f"{name} withdrew from {iv['id']}", body=call["reply"])
            out.append(f"{iv['id']} {name} ({party}): withdrew; HR notified.")
        else:
            self.call("update_interview_status", interview_id=iv["id"], party=party, status="needs_human", note=call["reply"])
            out.append(f"{iv['id']} {name} ({party}): unclear reply, flagged for HR.")

    def _reminders(self):
        out = []
        for iv in self.call("list_interviews", which="upcoming", days_ahead=3)["interviews"]:
            for party in ("candidate", "interviewer"):
                name = iv[party]
                other = iv["interviewer"] if party == "candidate" else iv["candidate"]
                msg = (f"Hello {name.split()[0]}, this is the HR assistant calling about your interview "
                       f"{'with' if party == 'candidate' else 'of'} {other} on {iv['when_spoken']}, {iv['mode'].lower()}. "
                       "Can you confirm you'll be there?")
                call = self.call("place_call", to_phone=iv[f"{party}_phone"], to_name=name, message=msg, context_id=iv["id"])
                self._handle_reply(iv, party, call, out)
        return self._wrap("Reminder calls", out)

    def _follow_up(self):
        out = []
        for iv in self.call("list_interviews", which="past")["interviews"]:
            first = iv["candidate"].split()[0]
            if iv["status"] == "attended":
                msg = (f"Hello {first}, this is the HR assistant. Thank you for attending your interview on "
                       f"{iv['when_spoken']}. How did it go, and do you have any questions about next steps?")
            else:
                msg = (f"Hello {first}, this is the HR assistant. We missed you at your interview on {iv['when_spoken']}. "
                       "I hope everything is okay. Would you like another slot?")
            call = self.call("place_call", to_phone=iv["candidate_phone"], to_name=iv["candidate"], message=msg, context_id=iv["id"])
            self._handle_reply(iv, "candidate", call, out)
        return self._wrap("Follow-up calls", out)

    def _inbound(self, request):
        emp = re.search(r"\bE\d{3}\b", request, re.I)
        question = request.split(":", 1)[-1].strip()
        parts = []
        if emp:
            e = self.call("get_employee", employee_id=emp.group(0).upper())
            lt = next((x for x in ("sick", "casual", "annual") if x in question.lower()), None)
            if lt and "error" not in e:
                parts.append(f"You have {e['leave_balance'][lt]} {lt} leave days left.")
        hits = self.call("search_policy", query=question)["results"]
        parts.append(hits[0]["text"] if hits else "I don't have that in the handbook, so I'll ask HR to call you back.")
        answer = " ".join(parts)
        self.call("log_inbound_call", caller=emp.group(0).upper() if emp else "unknown caller", question=question, answer=answer)
        return f"Spoken answer: {answer}"

    def _wrap(self, title, out):
        pending = [o for o in out if "awaiting HR approval" in o]
        lines = [f"{title}: {len(out)} calls handled."] + [f"- {o}" for o in out]
        if pending:
            lines.append(f"{len(pending)} reschedule proposal(s) await HR approval in outputs/voice/reschedule_proposals.json.")
        lines.append("Transcripts are in outputs/voice/transcripts.")
        return "\n".join(lines)


# ---------------------------------------------------------------- orchestrator

AGENTS = {"screening": ScreeningAgent, "onboarding": OnboardingAgent, "policy": LeavePolicyAgent, "voice": VoiceAgent}


def route(request):
    """Pick the agent for a request: Claude classifies when available, keywords otherwise."""
    client = _client()
    if client is not None:
        try:
            resp = _create(client, "low", max_tokens=2000,
                           system="Classify the HR request. Reply with exactly one word: screening (resumes, candidates, "
                                  "hiring shortlist), onboarding (new hires, joining, documents), voice (phone calls: "
                                  "interview reminder or follow-up calls, rescheduling by phone, inbound calls), or "
                                  "policy (leave requests, HR policy questions, anything else).",
                           messages=[{"role": "user", "content": request}])
            word = "".join(b.text for b in resp.content if b.type == "text").strip().lower()
            if word in AGENTS:
                return word
        except Exception:
            pass
    t = request.lower()
    if re.search(r"\bcalls?\b|phone|voice|remind|follow[- ]?up|caller", t):
        return "voice"
    if re.search(r"resume|cv\b|candidate|screen|shortlist|applicant", t):
        return "screening"
    if re.search(r"onboard|new hire|joining|joiner|NH-\d", t, re.I):
        return "onboarding"
    return "policy"


def handle(request):
    key = route(request)
    result = AGENTS[key]().run(request)
    result["route"] = key
    return result
