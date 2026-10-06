"""The specialist agents. Each one is a spec (role, model tier, tools, skills) plus a rules-only plan.

How an agent runs (run_agent):
1. With a model available, a tool-use loop through the AI gateway: the model picks LangChain tools until done.
2. With no model (offline, mock mode, or a blocked budget), the agent's `plan` calls the same tools in a fixed
   order, so the system still works and tests stay deterministic.
"""

import json
import re
from dataclasses import dataclass, field

from .. import auth, config, db, skills
from .. import tools as T
from ..gateway import llm
from ..knowledge import cag, vectors

MAX_TURNS = 12


@dataclass
class Spec:
    key: str
    title: str
    description: str
    tier: str
    tools: list
    system: str
    cache_system: bool = False
    examples: list = field(default_factory=list)


class Run:
    """Collects the tool trace for one agent run."""

    def __init__(self, agent):
        self.agent, self.trace = agent, []

    def call(self, tool, **args):
        out = T.run(tool, **args)
        self.trace.append({"agent": self.agent, "tool": tool, "input": args, "output": out})
        return out


def system_prompt(spec, memories):
    user = auth.current_user()
    who = f"The user is {user.username} (role: {user.role}" + (f", employee id {user.employee_id})" if user.employee_id else ")")
    mem = ("\n\nWhat you remember about this user (use only if relevant):\n" + "\n".join(f"- {m}" for m in memories)) if memories else ""
    text = f"{spec.system}\n\nToday's date is {config.today().isoformat()}. {who}.{skills.prompt_block(spec.key)}{mem}"
    if spec.key == "policy":  # CAG: the whole handbook rides in the (cached) system prompt
        ctx = cag.policy_context("")
        if ctx["strategy"] == "cag":
            text += f"\n\n<handbook>\n{ctx['context']}\n</handbook>"
    return text


def run_agent(spec, request, memories=()):
    auth.require(f"agent:{spec.key}")
    run = Run(spec.key)
    try:
        return _run_llm(spec, request, memories, run), run.trace
    except (llm.NoModelAvailable, llm.BudgetExceeded) as exc:
        run.trace.append({"agent": spec.key, "tool": "no_model_using_rules", "input": {}, "output": {"reason": str(exc)[:300]}})
        result = PLANS[spec.key](run, request)
        return {"text": result, "mode": "rules"}, run.trace


def _run_llm(spec, request, memories, run):
    system = system_prompt(spec, memories)
    messages = [{"role": "user", "content": request}]
    specs = T.openai_specs(spec.tools)
    model = ""
    for _ in range(MAX_TURNS):
        res = llm.complete(spec.key, messages, system=system, tools=specs, tier=spec.tier, cache_system=spec.cache_system)
        model = res.model
        messages.append(res.message)
        if not res.tool_calls:
            return {"text": res.text.strip(), "mode": f"llm:{model}"}
        for call in res.tool_calls:
            out = run.call(call["name"], **call["args"]) if call["name"] in spec.tools else {"error": f"Unknown tool {call['name']}"}
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(out, default=str)[:20000]})
    return {"text": "Stopped after too many steps; see the trace.", "mode": f"llm:{model}"}


# ---------------------------------------------------------------- specs

POLICY = Spec(
    "policy", "Policy agent", "Answers HR policy questions from the handbook with section citations.", "fast",
    ["policy_context", "search_policy", "kg_facts", "search_faq", "recall_memory", "load_skill", "report_issue"],
    "You are the HR policy helpdesk. The full handbook is in <handbook> below (cache-augmented). Answer in 2-4 "
    "sentences and cite the section number. Use kg_facts for exact numbers, approvers and reporting lines. If the "
    "handbook does not cover the question, say \"I couldn't find this in the handbook\" and suggest contacting "
    "hr@example.com. Never invent policy.", cache_system=True,
    examples=["How many days of maternity leave do we offer?", "Can I work from home three days a week?"])

LEAVE = Spec(
    "leave", "Leave agent", "Evaluates and records leave requests using the handbook rules; routes exceptions to managers.",
    "fast",
    ["get_employee", "evaluate_leave_request", "record_leave_decision", "kg_facts", "draft_email", "recall_memory",
     "load_skill", "ask_agent", "report_issue"],
    "You are the HR leave agent. For a leave request you need the employee id, leave type and dates. Call "
    "evaluate_leave_request, then record_leave_decision with the same arguments, then draft an email to the employee "
    "with the outcome (and to the manager when it needs their approval). Never approve anything the rules did not "
    "approve. Answer in 2-4 sentences.",
    examples=["Employee E101 wants annual leave from 2026-10-28 to 2026-10-30."])

ONBOARDING = Spec(
    "onboarding", "Onboarding agent", "Checks documents, builds the dated onboarding plan and drafts the emails.", "smart",
    ["get_new_hire", "check_documents", "create_onboarding_plan", "draft_email", "ask_agent", "load_skill",
     "recall_memory", "report_issue"],
    "You are the HR onboarding agent. Follow the onboarding skill (load it first). If the user names no one, onboard "
    "every new hire starting in the next 30 days.",
    examples=["Onboard our new hires Priya Raman and Vikram Singh."])

SCREENING = Spec(
    "screening", "Screening crew", "A three-role crew (sourcer, fairness reviewer, coordinator) that shortlists candidates.",
    "smart",
    ["list_job_openings", "list_candidates", "search_resumes", "read_resume", "score_candidate", "save_shortlist",
     "draft_email", "load_skill", "report_issue"],
    "You are the HR screening coordinator. Follow the resume-screening skill (load it first).",
    examples=["Screen all resumes for the Backend Engineer opening and shortlist the best candidates."])

RECRUITMENT = Spec(
    "recruitment", "Recruitment pipeline agent",
    "Runs the hiring pipeline: resume inbox, interview rounds (L1..Ln, HR, Final), offers, joining and follow-ups.", "fast",
    ["ingest_resumes", "pipeline_summary", "candidate_timeline", "move_candidate", "schedule_interview",
     "record_interview_result", "make_offer", "record_offer_response", "mark_joined", "list_followups",
     "complete_followup", "set_interview_rounds", "draft_email", "load_skill", "report_issue"],
    "You are the recruitment pipeline agent. Use the tools to read new resumes from the inbox, track each candidate "
    "through the job's interview rounds, record results, request offers (they need human approval), record offer "
    "responses and joining, and manage follow-ups. Follow the hiring-pipeline skill. Refer to candidates by name and "
    "id. Never invent interview results; ask if a result or date is missing. Keep answers short.",
    examples=["Read the new resumes in the inbox", "Anita cleared L1 with rating 4, schedule L2 on 2026-10-12 at 11:00",
              "What follow-ups are due this week?"])

INSIGHTS = Spec(
    "insights", "HR insights agent",
    "Answers questions about HR numbers and trends (hiring funnel, round pass rates, time to hire, headcount, leave, "
    "onboarding, AI spend) and says what needs attention today.", "fast",
    ["hr_insights", "needs_attention", "pipeline_summary", "load_skill"],
    "You are the HR insights agent. Answer with numbers from the tools only; never estimate or invent a figure. Lead "
    "with the answer in one or two sentences, then at most five bullet points. When something needs action, say what "
    "and where (Hiring board, Approvals, Tickets, Budget). If the data is too thin to show a trend, say so.",
    examples=["How is hiring going?", "What is our offer acceptance rate?", "What needs my attention today?"])

PAYROLL = Spec(
    "payroll", "Salary and payroll agent",
    "Salary structures, CTC breakups, tax regime comparison, revisions, monthly payroll (PF, ESI, professional tax, "
    "TDS) and payslips.", "fast",
    ["salary_breakup", "compare_tax_regimes", "set_salary", "update_salary_details", "propose_salary_revision",
     "add_pay_adjustment", "run_payroll", "payroll_summary", "submit_payroll", "mark_payroll_paid", "my_payslip",
     "salary_structures", "draft_email", "load_skill", "report_issue"],
    "You are the salary and payroll agent for an Indian employer. Use the tools for every figure; never compute pay or "
    "tax yourself. Say amounts in rupees. A salary revision and a payroll run both need human approval, and payroll is "
    "approved by someone other than whoever submitted it, so say what is waiting rather than claiming it is done. "
    "Nothing is ever paid from here: HR uploads the bank file and then records the payment. Tell employees their own "
    "pay only. Follow the payroll-india skill.",
    examples=["What is the breakup for a 12 lakh CTC?", "Run payroll for 2026-10", "Which tax regime is better for me?",
              "Give Deepa a 10% hike from next month"])

PROJECTS = Spec(
    "projects", "Project and staffing agent",
    "Projects, who is allocated to them, capacity and the bench, tasks and milestones, timesheets and project risks.",
    "fast",
    ["project_board", "project_details", "create_project", "update_project", "allocate_person", "release_person",
     "team_capacity", "project_risks", "add_project_task", "update_project_task", "project_tasks", "my_projects",
     "log_project_hours", "project_timesheet", "draft_email", "load_skill", "report_issue"],
    "You are the project and staffing agent. Use the tools for every number; never guess who is free. Allocations are "
    "a percentage of someone's time, and nobody goes past 100%. When asked to staff something, suggest people the "
    "capacity tool says are free and say what they are already on. Keep answers short and name people and projects "
    "plainly. Follow the project-staffing skill.",
    examples=["Who is free next month?", "Put Deepa on the portal project at 40%", "What is slipping?",
              "Show the portal project"])

CULTURE = Spec(
    "culture", "Culture and engagement agent",
    "Cultural events and celebrations, RSVPs, kudos and awards, pulse surveys, birthdays and work anniversaries.",
    "fast",
    ["events_calendar", "event_details", "create_event", "update_event", "announce_event", "rsvp_event", "give_kudos",
     "kudos_wall", "nominate_for_award", "decide_award", "list_awards", "start_pulse_survey", "answer_pulse_survey",
     "pulse_results", "close_pulse_survey", "engagement_report", "draft_email", "load_skill", "report_issue"],
    "You are the culture and engagement agent. Use the tools for every date, number and name. An event invitation is "
    "drafted into the outbox for a person to send, never sent by you, and a budget past the limit waits for approval, "
    "so say what is waiting. Pulse answers are anonymous: never guess who said what, and do not report results until "
    "the tool returns them. Keep answers short and warm without being gushing. Follow the culture-events skill.",
    examples=["What is coming up this month?", "Plan a Diwali lunch on 2026-10-20 with a 20000 budget",
              "Kudos to Deepa for the payroll migration", "Whose birthday is coming up?"])

SPECS = {s.key: s for s in (POLICY, LEAVE, ONBOARDING, SCREENING, RECRUITMENT, INSIGHTS, PAYROLL, PROJECTS, CULTURE)}


# ---------------------------------------------------------------- rules-only plans (no model)

def plan_policy(run, request):
    hits = run.call("search_policy", query=request, k=1)["results"]
    facts = run.call("kg_facts", question=request)["facts"]
    if re.match(r"\s*(who|whom)\b", request, re.I) and facts:
        return "From the HR knowledge graph: " + "; ".join(
            f"{f['subject']} {f['predicate'].replace('_', ' ')} {f['object']}" for f in facts[:6]) + "."
    generic = {"policy", "leave", "day", "employee", "company", "offer", "rule", "allowed", "get", "have", "there"}
    asked = vectors._keywords(request) - generic
    if not hits or (asked and not asked & vectors._keywords(hits[0]["text"])):
        return "I couldn't find this in the handbook. Please contact HR at hr@example.com."
    top = hits[0]
    body = top["text"].split("\n", 1)[1] if "\n" in top["text"] else top["text"]
    answer = f"{body}\n\nSource: Policy handbook, section {top['section']}."
    if facts:
        answer += "\nRelated facts: " + "; ".join(f"{f['subject']} {f['predicate'].replace('_', ' ')} {f['object']}" for f in facts[:4])
    return answer


def plan_leave(run, request):
    user = auth.current_user()
    emp = re.search(r"\bE\d{3}\b", request, re.I)
    emp_id = emp.group(0).upper() if emp else user.employee_id
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", request)
    if not (emp_id and dates):
        return "I need the employee id and the leave dates (YYYY-MM-DD) to process a leave request."
    lt = next((t for t in ("sick", "casual", "annual") if t in request.lower()), "annual")
    res = run.call("record_leave_decision", employee_id=emp_id, leave_type=lt, start_date=dates[0], end_date=dates[-1])
    if "error" in res:
        return res["error"]
    e = db.q1("SELECT * FROM employees WHERE id=?", (res["employee_id"],))
    first = res["employee"].split()[0]
    if res["status"] == "approved":
        msg = (f"{first}'s {lt} leave from {dates[0]} to {dates[-1]} ({res['working_days']} working days) is approved. "
               f"Remaining {lt} balance: {res['balance_after']:g} days. {res['note']}").strip()
    else:
        msg = (f"{first}'s {lt} leave from {dates[0]} to {dates[-1]} needs {res['manager']}'s approval "
               f"(decision: route_to_manager) because: {'; '.join(res['reasons'])}.")
        run.call("draft_email", to=e["manager_email"], subject=f"Leave approval needed: {res['employee']}",
                 body=f"{res['employee']} requested {lt} leave {dates[0]} to {dates[-1]}. {'; '.join(res['reasons'])}. "
                      f"Approve or reject it in the HR console (approval #{res['approval_id']}).")
    run.call("draft_email", to=e["email"], subject=f"Your {lt} leave request", body=f"Hi {first},\n\n{msg}\n\nHR Helpdesk")
    return msg


def plan_onboarding(run, request):
    hires = db.q("SELECT * FROM new_hires")
    text = request.lower()
    named = [h for h in hires if h["id"].lower() in text or any(p.lower() in text for p in h["name"].split())]
    out = []
    for h in named or hires:
        docs = run.call("check_documents", hire_id=h["id"])
        plan = run.call("create_onboarding_plan", hire_id=h["id"])
        first = h["name"].split()[0]
        run.call("draft_email", to=h["email"], subject=f"Welcome to the team, {first}!",
                 body=f"Hi {first},\n\nWe are excited to have you join as {h['role']} on {h['start_date']}. Your manager "
                      f"will be {h['manager']}. Day 1 starts at 9:30 with orientation.\n\nWarm regards,\nHR")
        if docs["missing"]:
            cite = run.call("ask_agent", agent="policy", question="Which onboarding documents must every new hire submit?")
            run.call("draft_email", to=h["email"], subject="Pending documents before your joining date",
                     body=f"Hi {first},\n\nTo complete your onboarding, please upload: {', '.join(docs['missing'])}."
                          + (" Payroll cannot be set up until bank details and PAN card are received." if docs["payroll_blocked"] else "")
                          + f"\n\n(Policy: {cite.get('answer', '')[:200]})\n\nThanks,\nHR")
        tasks = "\n".join(f"- {t['due']} {t['owner']}: {t['task']} ({t['status']})" for t in plan["tasks"])
        run.call("draft_email", to=f"it-helpdesk@example.com, {h['manager']}",
                 subject=f"Onboarding tasks for {h['name']} ({h['start_date']})", body=f"Please action:\n\n{tasks}\n\nHR")
        blocked = [t for t in plan["tasks"] if t["status"].startswith("blocked")]
        out.append(f"{h['name']} ({h['id']}), starts {h['start_date']}: {len(plan['tasks'])} tasks planned, "
                   + (f"{len(docs['missing'])} documents missing, {len(blocked)} blocked task(s)."
                      if docs["missing"] else "all documents received, nothing blocked."))
    return "\n".join(out) + "\nEmail drafts are in the outbox for review."


def plan_screening(run, request):
    from .crew import rules_crew
    return rules_crew(run, request)


ROUND = r"(L\d+|HR|Final)"


def _find_candidate(text):
    for c in db.q("SELECT id, name FROM candidates"):
        if re.search(rf"\b{re.escape(c['id'])}\b", text, re.I) or c["name"].lower() in text.lower() \
                or re.search(rf"\b{re.escape(c['name'].split()[0])}\b", text, re.I):
            return c
    return None


def plan_recruitment(run, request):
    """Rules-only understanding of common pipeline requests; anything else gets the pipeline summary."""
    t = request.lower()
    if re.search(r"inbox|folder|new resumes|ingest|upload", t):
        r = run.call("ingest_resumes")
        if "error" in r:
            return r["error"]
        lines = [f"Read {len(r['added'])} new resume(s)."] + [f"- {a['name']} ({a['candidate_id']}): {a['stage']}, score {a['score']}"
                                                             for a in r["added"]]
        lines += [f"Duplicate: {d}" for d in r["duplicates"]] + [f"Unreadable: {u}" for u in r["unreadable"]]
        lines += [f"No job folder for: {n} (put it under inbox/<JOB-ID>/)" for n in r["no_job"]]
        return "\n".join(lines)
    if "follow" in t:
        fs = run.call("list_followups", days_ahead=7)["followups"]
        return "\n".join(f"#{f['id']} {f['due']} {f['name']}: {f['note']}" for f in fs) or "No follow-ups due in the next 7 days."
    c = _find_candidate(request)
    rnd = re.search(ROUND, request, re.I)
    rnd = rnd.group(1).upper() if rnd and rnd.group(1).lower() != "final" else ("Final" if rnd else None)
    if c and rnd and re.search(r"\b(pass|passed|cleared|cleared|selected in|fail|failed|rejected in|hold)\b", t):
        result = "fail" if re.search(r"\bfail|rejected", t) else "hold" if "hold" in t else "pass"
        rating = re.search(r"rating\s*(\d)", t)
        out = run.call("record_interview_result", candidate=c["id"], round_name=rnd, result=result,
                       rating=int(rating.group(1)) if rating else 0, feedback=request)
        if "error" in out:
            return out["error"]
        msg = f"Recorded {rnd} {result} for {c['name']}. Stage: {out['stage']}" + (f", next round {out['next_round']}" if out.get("next_round") else "") + "."
        when = re.search(r"(\d{4}-\d{2}-\d{2})(?:[ T]at\s*|[ T])?(\d{1,2}:\d{2})?", request)
        if out.get("next_round") and "schedule" in t and when:
            s = run.call("schedule_interview", candidate=c["id"], round_name=out["next_round"],
                         when=f"{when.group(1)}T{(when.group(2) or '11:00').zfill(5)}")
            msg += f" {out['next_round']} scheduled for {s.get('scheduled_at', '?')}." if "error" not in s else f" {s['error']}"
        return msg
    if c and "schedule" in t:
        when = re.search(r"(\d{4}-\d{2}-\d{2})(?:[ T]at\s*|[ T])?(\d{1,2}:\d{2})?", request)
        s = run.call("schedule_interview", candidate=c["id"], round_name=rnd or "",
                     when=f"{when.group(1)}T{(when.group(2) or '11:00').zfill(5)}" if when else "")
        return s.get("error") or f"{s['round']} for {c['name']} scheduled at {s['scheduled_at']}; invite drafted."
    if c and re.search(r"accept", t):
        out = run.call("record_offer_response", candidate=c["id"], accepted=True)
        return out.get("error") or f"{c['name']} accepted; new hire {out['new_hire_id']} joining {out['joining_date']}. Onboarding started."
    if c and re.search(r"joined", t):
        out = run.call("mark_joined", candidate=c["id"])
        return out.get("error") or f"{c['name']} marked as joined."
    if c:
        tl = run.call("candidate_timeline", candidate=c["id"])
        cand = tl["candidate"]
        return (f"{cand['name']} ({cand['id']}): {cand['stage']}. {cand.get('status_note') or ''} Next round: "
                f"{tl['next_round'] or 'none'}. Last update {cand['updated_at']}.")
    return run.call("pipeline_summary")["summary"]


def plan_insights(run, request):
    t = request.lower()
    if re.search(r"attention|to ?do|today|pending|overdue|what should|next action", t):
        items = run.call("needs_attention")["items"]
        return ("Needs attention:\n" + "\n".join(f"{i + 1}. {a['text']}" for i, a in enumerate(items))) if items \
            else "Nothing needs attention right now."
    for section, pattern in (("payroll", r"payroll|salary cost|wage bill"), ("projects", r"project|utilisation|bench"), ("culture", r"event|culture|kudos|engagement"), ("ai", r"\bai\b|budget|spend|token"), ("leave", r"leave"),
                             ("onboarding", r"onboarding"), ("workforce", r"headcount|department|workforce|joiners")):
        if re.search(pattern, t):
            data = run.call("hr_insights", section=section)
            return f"{section.title()} insights:\n" + json.dumps(data.get(section, data), indent=1, default=str)[:3000]
    return run.call("hr_insights")["summary"]


MONTH = r"(20\d\d-\d\d)|\b(january|february|march|april|may|june|july|august|september|october|november|december)\b"


def _month_in(text):
    m = re.search(MONTH, text, re.I)
    if not m:
        return ""
    if m.group(1):
        return m.group(1)
    names = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
             "november", "december"]
    mo = names.index(m.group(2).lower()) + 1
    return f"{config.today().year}-{mo:02d}"


def plan_payroll(run, request):
    t = request.lower()
    money = re.search(r"(\d+(?:\.\d+)?)\s*(lakh|lpa|l\b|crore|cr\b)?", t)
    month = _month_in(request)
    if re.search(r"payslip|pay ?slip|my pay|salary slip", t):
        out = run.call("my_payslip", month=month)
        return out.get("error") or out["text"]
    if re.search(r"regime|old vs new|tax saving", t) and money:
        amount = _amount(money)
        out = run.call("compare_tax_regimes", ctc_annual=amount)
        if "error" in out:
            return out["error"]
        return (f"On ₹{amount:,.0f} CTC the {out['better']} regime costs less. New regime: tax ₹{out['new']['tax']:,}, "
                f"take-home ₹{out['new']['take_home_monthly']:,} a month. Old regime (with the declarations given): tax "
                f"₹{out['old']['tax']:,}, take-home ₹{out['old']['take_home_monthly']:,} a month.")
    if re.search(r"breakup|break-up|structure|components", t) and money:
        amount = _amount(money)
        b = run.call("salary_breakup", ctc_annual=amount)
        if "error" in b:
            return b["error"]
        m, a = b["monthly"], b["annual"]
        return (f"₹{amount:,.0f} CTC: monthly basic ₹{m['basic']:,}, HRA ₹{m['hra']:,}, special allowance "
                f"₹{m['special_allowance']:,}, gross ₹{m['gross']:,}. Employee PF ₹{b['employee_deductions_monthly']['pf']:,} "
                f"a month. In the CTC: employer PF ₹{a['employer_pf']:,}, gratuity ₹{a['gratuity']:,} a year"
                + (f", employer ESI ₹{a['employer_esi']:,}" if b["esi_applicable"] else "") + ".")
    if re.search(r"\brun\b.*payroll|process (the )?payroll|calculate payroll", t):
        out = run.call("run_payroll", month=month)
        return out.get("error") or (f"Draft payroll for {out['month']}: {out['employees']} employees, gross "
                                    f"₹{out['gross']:,}, net ₹{out['net']:,}, employer cost ₹{out['employer_cost']:,}."
                                    + (" Warnings: " + "; ".join(out["warnings"]) + "." if out["warnings"] else "")
                                    + " Submit it for approval when it looks right.")
    if re.search(r"submit", t) and "payroll" in t:
        out = run.call("submit_payroll", month=month)
        return out.get("error") or f"Payroll for {out['month']} is waiting for approval (#{out['approval_id']})."
    if re.search(r"hike|raise|revision|increment|increase", t):
        name = re.sub(r".*?(?:give|for)\s+", "", request, flags=re.I).split()[0] if re.search(r"give|for", t, re.I) else ""
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
        if name and (pct or money):
            out = run.call("propose_salary_revision", employee=name, pct=float(pct.group(1)) if pct else 0,
                           new_ctc_annual=0 if pct else _amount(money), reason=request)
            return out.get("error") or (f"Proposed ₹{out['old_ctc']:,} to ₹{out['new_ctc']:,} ({out['pct']:+}%) from "
                                        f"{out['effective_from']}. Waiting for approval #{out['approval_id']}.")
    if re.search(r"salary|ctc|structures", t) and re.search(r"everyone|all|list|structures", t):
        rows = run.call("salary_structures")["employees"]
        return "\n".join(f"{r['name']} ({r['id']}): " + (f"₹{r['ctc_annual']:,} CTC, ₹{r['monthly_gross']:,} gross a month, "
                          f"{r['regime']} regime" + (f", missing {', '.join(r['missing'])}" if r["missing"] else "")
                          if r["ctc_annual"] else "no salary set up")
                         for r in rows)
    out = run.call("payroll_summary", month=month)
    if "error" in out:
        return out["error"]
    if out["status"] == "not_started":
        return f"No payroll has been run for {out['month']} yet."
    return (f"Payroll {out['month']} ({out['status']}): {out['employees']} employees, gross ₹{out['gross']:,}, "
            f"deductions ₹{out['deductions']:,}, net ₹{out['net']:,}, employer cost ₹{out['employer_cost']:,}. "
            f"TDS ₹{out['statutory']['tds']:,}, PF ₹{out['statutory']['pf_employee'] + out['statutory']['pf_employer']:,}.")


def _amount(match):
    """'12 lakh', '12 LPA', '1200000' -> rupees a year."""
    n = float(match.group(1))
    unit = (match.group(2) or "").strip()
    if unit in ("lakh", "lpa", "l"):
        return n * 100000
    if unit in ("crore", "cr"):
        return n * 10000000
    return n * 100000 if n < 200 else n


def plan_projects(run, request):
    t = request.lower()
    if re.search(r"\bfree\b|bench|capacity|availab|utilisation|utilization|who can", t):
        out = run.call("team_capacity", free_only=bool(re.search(r"free|bench|availab|who can", t)))
        u = out["utilisation"]
        lines = [f"Average utilisation {u['average_pct']}% across {u['people']} people; {u['bench']} on the bench, "
                 f"{u['over_allocated']} over-allocated."]
        for c in out["people"][:10]:
            on = ", ".join(f"{p['project']} {p['percent']:g}%" for p in c["projects"]) or "nothing"
            lines.append(f"- {c['name']}: {c['allocated_pct']:g}% booked ({on}), {c['free_pct']:g}% free"
                         + (f", {c['leave_days']:g} leave day(s)" if c["leave_days"] else ""))
        return "\n".join(lines)
    if re.search(r"risk|slip|late|overdue|attention|blocked", t):
        risks = run.call("project_risks")["risks"]
        return "\n".join(f"- {x['text']}" for x in risks) or "Nothing is slipping right now."
    if re.search(r"\b(put|allocate|assign|staff)\b", t) and re.search(r"\bon\b|\bto\b", t):
        pct = re.search(r"(\d{1,3})\s*%", t)
        who = re.search(r"(?:put|allocate|assign|staff)\s+(E\d+|[A-Za-z][\w.]*(?:\s+[A-Z][\w.]*)??)\s+(?:on|to)\b",
                        request, re.I)
        what = re.search(r"\b(?:on|to)\s+(?:the\s+)?([\w \-]+?)(?:\s+project)?(?:\s+at\b|\s+for\b|[.,]|$)", request, re.I)
        if who and what:
            out = run.call("allocate_person", employee=who.group(1).strip(), project=what.group(1).strip(),
                           percent=float(pct.group(1)) if pct else 100)
            return out.get("error") or (f"{out['name']} is on {out['project']} at {out['percent']:g}% from "
                                        f"{out['start_date']}; now {out['now_allocated_pct']:g}% booked in total.")
    if re.search(r"timesheet|hours", t):
        out = run.call("project_timesheet", days=7)
        if "error" in out:
            return out["error"]
        return (f"{out['total_hours']:g} hours logged since {out['from']}: "
                + ", ".join(f"{b['project']} {b['hours']:g}h" for b in out["by_project"])) if out["entries"] \
            else "No hours logged in the last week."
    if re.search(r"\bmy\b", t):
        out = run.call("my_projects")
        if "error" in out:
            return out["error"]
        return (f"{out['name']} is {out['allocated_pct']:g}% allocated: "
                + "; ".join(f"{c['project']} {c['percent']:g}% as {c['role'] or 'team member'}" for c in out["current"])) \
            if out["current"] else f"{out['name']} is not on any project right now."
    if re.search(r"task|milestone", t):
        rows = run.call("project_tasks")["tasks"]
        return "\n".join(f"- [{r['project']}] {r['title']} ({r['status']}"
                         + (f", due {r['due']}{' OVERDUE' if r['overdue'] else ''}" if r["due"] else "") + ")"
                         for r in rows[:15]) or "No open tasks."
    name = re.search(r"(?:show|about|status of|how is)\s+(?:the\s+)?([\w \-]+?)(?:\s+project)?[?.]?$", request, re.I)
    if name:
        out = run.call("project_details", project=name.group(1).strip())
        if "error" not in out:
            p, team = out["project"], [t for t in out["team"] if t["current"]]
            open_tasks = [t for t in out["tasks"] if t["status"] != "done"]
            return (f"{p['name']} ({p['id']}): {p['status']}, {p['health'].replace('_', ' ')}, manager "
                    f"{p['manager'] or 'none'}, ends {p['end_date'] or 'open'}. Team: "
                    + (", ".join(f"{m['name']} {m['percent']:g}%" for m in team) or "nobody")
                    + f". {len(open_tasks)} open task(s)."
                    + (f" Missing skills: {', '.join(out['staffing']['gaps'])}." if out["staffing"]["gaps"] else ""))
    return run.call("project_board") and _project_summary(run)


def _project_summary(run):
    rows = run.call("project_board")["projects"]
    if not rows:
        return "No projects yet."
    cap = run.call("team_capacity")["utilisation"]
    lines = [f"{len(rows)} project(s):"]
    for p in rows[:10]:
        lines.append(f"- {p['name']} ({p['status']}): {p['team_size']} people ({p['fte']} FTE), {p['open_tasks']} open "
                     f"task(s)" + (f", {p['overdue_tasks']} overdue" if p["overdue_tasks"] else "")
                     + (f", ends in {p['days_left']} day(s)" if p["days_left"] is not None else ""))
    lines.append(f"Average utilisation {cap['average_pct']}%, {cap['bench']} on the bench.")
    return "\n".join(lines)


def plan_culture(run, request):
    t = request.lower()
    if re.search(r"kudos|thank|appreciat|shout ?out|well done", t):
        m = re.search(r"(?:kudos|thanks|thank you|shout ?out)\s+(?:to\s+)?"
                      r"([A-Za-z][\w.]*(?:\s+[A-Z][\w.]*)??)\s+for\s+(.*)", request, re.I)
        if m and m.group(2).strip():
            out = run.call("give_kudos", to=m.group(1).strip(), message=m.group(2).strip())
            return out.get("error") or f"Kudos to {out['to']} recorded: \u201c{out['message']}\u201d."
        wall = run.call("kudos_wall")
        return "\n".join(f"- {k['to_name']} from {k['from_name'] or 'the team'}: {k['message']}" for k in wall["kudos"][:10]) \
            or "No kudos yet. Be the first."
    if re.search(r"birthday|anniversar", t):
        occ = [c for c in run.call("events_calendar", days_ahead=45)["calendar"] if c["type"] == "occasion"]
        return "\n".join(f"- {o['day']}: {o['title']}" for o in occ) or "No birthdays or anniversaries in the next 45 days."
    if re.search(r"\bplan\b|organis|organiz|\bschedule\b.*event|create.*event", t):
        when = re.search(r"(\d{4}-\d{2}-\d{2})", request)
        budget = re.search(r"(?:budget\s*(?:of\s*)?|₹|rs\.?\s*)([\d,]+)", request, re.I)
        title = re.sub(r"^(?:can you\s+)?(?:plan|organise|organize|schedule|create)\s+(?:an?\s+)?", "", request, flags=re.I)
        title = re.split(r"\s+on\s+\d{4}-|\s+with\s+", title)[0].strip(" .?")
        if when and title:
            kind = next((k for k in ("festival", "town_hall", "offsite", "training", "volunteering", "sports")
                         if k.replace("_", " ") in t), "celebration")
            out = run.call("create_event", title=title, day=when.group(1), kind=kind,
                           budget=float(budget.group(1).replace(",", "")) if budget else 0)
            if "error" in out:
                return out["error"]
            msg = f"{out['title']} is planned for {out['day']}."
            if out.get("approval_id"):
                msg += f" The ₹{out['budget']:,.0f} budget needs approval (#{out['approval_id']}) before it is announced."
            else:
                msg += " Announce it when you are ready and the invitation will be drafted in the outbox."
            return msg
    if re.search(r"rsvp|coming|attend", t) and re.search(r"\byes\b|\bno\b|maybe", t):
        answer = "yes" if re.search(r"\byes\b|count me in|i(?:'| a)m coming", t) else "no" if re.search(r"\bno\b", t) else "maybe"
        name = re.search(r"(?:to|for)\s+(?:the\s+)?([\w \-]+?)(?:[.?]|$)", request, re.I)
        if name:
            out = run.call("rsvp_event", event=name.group(1).strip(), answer=answer)
            return out.get("error") or f"Noted: {answer} to {out['title']} ({out['attending']['headcount']} attending)."
    if re.search(r"survey|pulse|how is everyone|mood", t):
        out = run.call("pulse_results")
        if "surveys" in out:
            return "\n".join(f"#{s['id']} {s['title']}: {s['answers']} answer(s), closes {s['closes']}"
                             for s in out["surveys"]) or "No pulse survey is open."
        return str(out)
    cal = run.call("events_calendar", days_ahead=45)["calendar"]
    if not cal:
        return "Nothing is planned in the next 45 days."
    lines = []
    for c in cal[:12]:
        if c["type"] == "event":
            lines.append(f"- {c['day']}: {c['title']} ({c['kind'].replace('_', ' ')}, {c['status']}, "
                         f"{c['attending']} attending)")
        else:
            lines.append(f"- {c['day']}: {c['title']}")
    return "\n".join(lines)


PLANS = {"policy": plan_policy, "leave": plan_leave, "onboarding": plan_onboarding, "screening": plan_screening,
         "recruitment": plan_recruitment, "insights": plan_insights, "payroll": plan_payroll,
         "projects": plan_projects, "culture": plan_culture}
