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

SPECS = {s.key: s for s in (POLICY, LEAVE, ONBOARDING, SCREENING, RECRUITMENT)}


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


PLANS = {"policy": plan_policy, "leave": plan_leave, "onboarding": plan_onboarding, "screening": plan_screening,
         "recruitment": plan_recruitment}
