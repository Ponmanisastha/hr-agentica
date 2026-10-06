"""Screening crew: three roles that hand work to each other, like a small recruiting team.

- Sourcer: finds and scores every candidate for the job (RAG search over resumes + rule scoring).
- Fairness reviewer: checks the evidence uses only skills and experience, and flags borderline calls.
- Hiring coordinator: makes the shortlist, saves it and drafts the invitation and decline emails.

Three engines, picked automatically:
1. CrewAI, when it is installed and a model is available. CrewAI does not support Python 3.14 yet
   (it requires < 3.14), so on 3.14 this path is skipped; install requirements-crewai.txt on Python 3.13 to use it.
2. A LangGraph crew (sourcer -> reviewer -> coordinator nodes) when a model is available.
3. The rules-only crew below when no model is available.
"""

import json
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .. import auth
from ..gateway import llm
from .specialists import SCREENING, Run, _run_llm, Spec

PROTECTED = re.compile(r"\b(gender|female|male|age|old|young|religion|caste|married|pregnan|nationality|muslim|hindu|christian)\b", re.I)


def crewai_available():
    try:
        import crewai  # noqa: F401
        return True
    except Exception:
        return False


def run_crew(request):
    auth.require("agent:screening")
    if llm.any_model_available():
        if crewai_available():
            try:
                return _crewai(request)
            except llm.BudgetExceeded:
                pass
        try:
            return _langgraph_crew(request)
        except (llm.NoModelAvailable, llm.BudgetExceeded):
            pass
    run = Run("screening")
    return {"text": rules_crew(run, request), "mode": "rules"}, run.trace


# ---------------------------------------------------------------- rules-only crew

def _pick_job(run, request):
    jobs = run.call("list_job_openings")["jobs"]
    return next((j for j in jobs if j["id"].lower() in request.lower() or j["title"].lower() in request.lower()), jobs[0])


def rules_crew(run, request):
    job = _pick_job(run, request)
    # Sourcer
    run.call("search_resumes", query=f"{job['title']} " + " ".join(job["must_have"]), k=6)
    cands = run.call("list_candidates", job_id=job["id"])["candidates"]
    scored = sorted((run.call("score_candidate", candidate_id=c["id"], job_id=job["id"]) for c in cands), key=lambda s: -s["score"])
    # Fairness reviewer
    picks = [s for s in scored if s["meets_minimum"]][: job["shortlist_size"]]
    cut = picks[-1]["score"] if picks else 100
    review = []
    for s in scored:
        s["decision"] = "shortlist" if s in picks else "decline"
        s["reason"] = (f"Meets all must-haves, {s['years_experience']} years" if s in picks else
                       f"Missing: {', '.join(s['must_have_missing'])}" if s["must_have_missing"] else
                       f"Below minimum years ({s['years_experience']})" if not s["meets_minimum"] else "Ranked below the shortlist size")
        if PROTECTED.search(s["reason"]):
            s["reason"] = "Decision based on skills and experience only"
        if s not in picks and abs(s["score"] - cut) <= 5:
            review.append(f"{s['candidate']} is within 5 points of the cut; HR may want a second look.")
    run.trace.append({"agent": "fairness_reviewer", "tool": "review", "input": {}, "output": {"flags": review}})
    # Coordinator
    run.call("save_shortlist", job_id=job["id"], notes=" ".join(review) or "No borderline cases.",
             decisions=[{"candidate_id": s["candidate_id"], "decision": s["decision"], "reason": s["reason"]} for s in scored])
    for s in scored:
        if s["decision"] == "shortlist":
            run.call("draft_email", to=s["email"], subject=f"Interview invitation: {job['title']}",
                     body=f"Dear {s['candidate']},\n\nThank you for applying for {job['title']}. We would like to invite you "
                          "to a 45-minute technical interview. Please reply with three slots that suit you this week.\n\n"
                          "Regards,\nTalent Acquisition")
        else:
            run.call("draft_email", to=s["email"], subject=f"Your application for {job['title']}",
                     body=f"Dear {s['candidate']},\n\nThank you for your interest in {job['title']}. After careful review we "
                          "will not be moving forward at this time.\n\nRegards,\nTalent Acquisition")
    lines = [f"Screened {len(scored)} candidates for {job['title']} ({job['id']}). Shortlisted {len(picks)}:"]
    lines += [f"- {s['candidate']} (score {s['score']})" for s in picks]
    lines += [f"Fairness review: {r}" for r in review] or ["Fairness review: no borderline cases."]
    lines.append("Shortlist saved; invitation and decline emails are drafts in the outbox.")
    return "\n".join(lines)


# ---------------------------------------------------------------- LangGraph crew (model available)

class CrewState(TypedDict, total=False):
    request: str
    sourcer: str
    reviewer: str
    coordinator: str
    trace: list


ROLES = {
    "sourcer": ("Sourcer", ["list_job_openings", "list_candidates", "search_resumes", "read_resume", "score_candidate"],
                "You are the sourcer on a recruiting crew. Find the job, score EVERY candidate with score_candidate and "
                "report a ranked table: candidate_id, name, score, meets_minimum, missing must-haves, years."),
    "reviewer": ("Fairness reviewer", ["read_resume", "load_skill"],
                 "You are the fairness reviewer. Check the sourcer's ranking uses only skills and experience, flag "
                 "borderline candidates (within 5 points of the cut or missing one must-have) and say what HR should look "
                 "at. Never use protected traits. Reply with your review only."),
    "coordinator": ("Hiring coordinator", ["save_shortlist", "draft_email", "load_skill"],
                    "You are the hiring coordinator. Using the sourcer's ranking and the reviewer's notes, decide shortlist "
                    "or decline for every candidate per the resume-screening skill, call save_shortlist, then draft one "
                    "email per candidate. Finish with a 3-5 line summary for HR."),
}


def _role_node(role):
    title, tools, system = ROLES[role]
    spec = Spec("screening", title, "", "smart" if role != "reviewer" else "fast", tools, system)

    def node(state: CrewState):
        context = f"Request: {state['request']}"
        if state.get("sourcer"):
            context += f"\n\nSourcer's ranking:\n{state['sourcer']}"
        if state.get("reviewer"):
            context += f"\n\nFairness review:\n{state['reviewer']}"
        run = Run(f"screening:{role}")
        out = _run_llm(spec, context, (), run)
        return {role: out["text"], "trace": state.get("trace", []) + run.trace}
    return node


def _langgraph_crew(request):
    g = StateGraph(CrewState)
    for role in ROLES:
        g.add_node(role, _role_node(role))
    g.add_edge(START, "sourcer")
    g.add_edge("sourcer", "reviewer")
    g.add_edge("reviewer", "coordinator")
    g.add_edge("coordinator", END)
    final = g.compile().invoke({"request": request, "trace": []})
    return {"text": final["coordinator"], "mode": "llm:langgraph-crew"}, final["trace"]


# ---------------------------------------------------------------- CrewAI crew (Python <= 3.13)

def _crewai(request):
    from crewai import LLM, Agent, Crew, Process, Task
    from crewai.tools import tool as crewai_tool
    from .. import config, tools as T

    tier = llm._apply_budget("screening", "smart")  # budget check before CrewAI makes its own LiteLLM calls
    alias = next(a for a in config.CHAINS[tier] if llm.model_available(a))
    model = LLM(model=config.MODELS[alias], max_tokens=4000)
    llm.track_external_calls()
    token = llm.agent_scope("screening")
    trace = []

    def wrap(name):
        def fn(**kwargs) -> str:
            out = T.run(name, **kwargs)
            trace.append({"agent": "screening:crewai", "tool": name, "input": kwargs, "output": out})
            return json.dumps(out, default=str)
        fn.__name__, fn.__doc__ = name, T.REGISTRY[name]["description"]
        fn.__signature__ = T.REGISTRY[name]["fn"].__signature__
        return crewai_tool(name)(fn)

    agents = {}
    for role, (title, names, system) in ROLES.items():
        agents[role] = Agent(role=title, goal=system, backstory="Part of the HR recruiting crew.", llm=model,
                             tools=[wrap(n) for n in names], allow_delegation=False, verbose=False)
    tasks = [Task(description=f"{request}\n\n{ROLES[r][2]}", expected_output="A concise report.", agent=agents[r])
             for r in ROLES]
    try:
        out = Crew(agents=list(agents.values()), tasks=tasks, process=Process.sequential, verbose=False).kickoff()
    finally:
        llm.reset_agent_scope(token)
    return {"text": str(out), "mode": f"llm:crewai:{config.MODELS[alias]}"}, trace
