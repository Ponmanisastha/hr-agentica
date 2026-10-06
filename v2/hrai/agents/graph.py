"""The orchestrator, built with LangGraph.

    START -> recall_memory -> route -> {policy | leave | onboarding | screening} -> remember -> END

- recall_memory: long-term memory for this user (MAG) plus the last turns of this conversation (short-term,
  kept by the LangGraph checkpointer per conversation id). Offline, follow-ups such as "and sick leave?" are
  rewritten with the subject of the previous question.
- route: the fast model classifies the request (keywords when no model is available), choosing only among
  the agents this user's role may use.
- specialists: tool-using agents (hrai/agents/specialists.py); screening is a three-role crew (crew.py).
  Agents can also call each other through A2A with the ask_agent tool.
- remember: saves the turn to long-term memory.
"""

import operator
import re
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .. import auth
from ..gateway import llm
from ..knowledge import cag, mag
from . import crew
from .specialists import SPECS, run_agent


class State(TypedDict, total=False):
    request: str
    route: str
    memories: list
    history: Annotated[list, operator.add]
    result: str
    mode: str
    trace: list


FOLLOW_UP = re.compile(r"^\s*(and|what about|how about|also|same for|ok(ay)?,? (and|what about))\b[\s,]*", re.I)
ABOUT_THE_CHAT = re.compile(r"what (did|was) i (just )?ask|my (last|previous|earlier) question|what were we (talking|discussing)", re.I)
POLICY_TOPIC = re.compile(r"\b(leave|polic(y|ies)|holiday|notice period|maternity|paternity|benefits?|reimburs\w*|"
                          r"work from home|wfh|allowance|entitle\w*|eligib\w*|insurance|probation|onboarding documents?|"
                          r"documents? (do )?i need|code of conduct|posh|harass\w*|gifts?|travel|hotel|expenses?|"
                          r"working hours|attendance|resign\w*|gratuity)\b", re.I)
TOPICS = ["annual", "sick", "casual", "maternity", "paternity", "notice period", "work from home", "reimbursement",
          "public holiday", "onboarding"]


def previous_questions(state):
    return [h.split(" -> A:", 1)[0][3:] for h in (state.get("history") or []) if h.startswith("Q: ")]


def resolve_follow_up(request, history):
    """Short-term context: "and sick leave?" after "How many casual leave days do we get?" becomes "How many sick
    leave days do we get?". A model reads the earlier turns itself; this is for the offline planner and the
    keyword router, so a follow-up reaches the same agent with the subject filled in."""
    if not history or not FOLLOW_UP.match(request):
        return request
    prev, rest = history[-1], FOLLOW_UP.sub("", request).strip(" ?.!")
    old = next((t for t in TOPICS if t in prev.lower()), None)
    new = next((t for t in TOPICS if t in rest.lower()), None)
    if old and new:
        return re.sub(re.escape(old), new, prev, count=1, flags=re.I)
    return f"{rest} ({prev})"


def recall_node(state: State):
    user = auth.current_user()
    asked = previous_questions(state)
    request = state["request"] if llm.any_model_available() else resolve_follow_up(state["request"], asked)
    mems = mag.recall(user.username, request) if user.username != "system" else []
    recent = [f"Earlier in this conversation: {h}" for h in (state.get("history") or [])[-3:]]
    return {"memories": recent + mems, "request": request}


def keyword_route(text):
    t = text.lower()
    if (re.search(r"\bbalance\b|\b(leaves?|days? off)\b.*\b(left|remaining|taken)\b|\b(left|remaining)\b.*\bleave|accru|"
                  r"how many .*\bleaves?\b.*\b(do i have|have i|i have|can i take)\b", t)
            and not re.search(r"payslip|salary|budget|project", t)):
        return "leave"
    if re.search(r"\bevent|celebrat|festival|diwali|town ?hall|offsite|party|rsvp|kudos|appreciat|award|nominat|"
                 r"birthday|anniversar|pulse|survey|engagement|culture|lunch|what is coming up|whats coming up|"
                 r"coming up this (month|week)|\bplan a\b|\borganis\w+ a\b|\borganiz\w+ a\b", t):
        return "culture"
    if re.search(r"project|allocat|staff(ing|ed)?\b|bench|capacity|utilisation|utilization|timesheet|milestone|"
                 r"\blog(ged)? \d+(\.\d+)? ?h(ou)?rs?\b|"
                 r"who is free|roll(ing)? off|\bfte\b|what is slipping|slipping|\btasks?\b|\bput\b .* \bon\b", t):
        return "projects"
    if re.search(r"payslip|pay ?slip|payroll|salary|\bctc\b|\bpf\b|\besi\b|professional tax|\btds\b|take[- ]home|"
                 r"tax regime|hike|increment|appraisal|bonus|in-?hand|gratuity", t):
        return "payroll"
    if re.search(r"insight|analytic|dashboard|metric|kpi|trend|funnel|time to (hire|offer|join)|acceptance rate|"
                 r"pass rate|headcount|needs? (my )?attention|how is hiring|statistics|\bstats\b", t):
        return "insights"
    if re.search(r"inbox|folder|pipeline|interview|\bround\b|\bl\d+\b|\bhr round|final round|(?<!we )\boffers?\b|joining date|joined|"
                 r"follow[- ]?ups?|hiring status|candidate status|cleared|ingest", t):
        return "recruitment"
    if re.search(r"resume|\bcv\b|candidate|screen|shortlist|applicant", t):
        return "screening"
    if re.search(r"onboard|new hire|joining|joiner|nh-\d", t):
        return "onboarding"
    if re.search(r"\d{4}-\d{2}-\d{2}", t) and re.search(r"leave|off|vacation|holiday", t):
        return "leave"
    if re.search(r"\b(apply|request|take|book)\b.*\bleave\b|\bleave (from|on|request)\b", t):
        return "leave"
    return "policy"


def route_node(state: State):
    """Pick the specialist. The model is only offered agents this user may use. Offline, a policy question that
    trips a keyword for an agent the role cannot use ("how much maternity leave do we offer?") goes to the policy
    agent instead of being refused."""
    request, user = state["request"], auth.current_user()
    allowed = {k: s for k, s in SPECS.items() if user.can(f"agent:{k}")}
    if llm.any_model_available():
        options = "\n".join(f"- {k}: {s.description}" for k, s in allowed.items())
        try:
            res = llm.complete("router", [{"role": "user", "content": request}], tier="fast", max_tokens=20,
                               system=f"Pick the agent for this HR request. Reply with one word from:\n{options}")
            word = res.text.strip().lower().split()[0].strip(".:") if res.text.strip() else ""
            if word in allowed:
                return {"route": word}
        except (llm.NoModelAvailable, llm.BudgetExceeded):
            pass
    word = keyword_route(request)
    if word not in allowed and POLICY_TOPIC.search(request):
        word = "policy"  # e.g. an employee's leave or benefits question that happened to contain a hiring word
    return {"route": word}  # anything else outside the role is refused by the agent with a clear 403


def specialist_node(state: State):
    key, request = state["route"], state["request"]
    asked = previous_questions(state)
    if ABOUT_THE_CHAT.search(request):
        text = (f"You asked: \"{asked[-1]}\"" + (f", and before that: \"{asked[-2]}\"" if len(asked) > 1 else "") + "."
                if asked else "This is the first thing you have asked me in this conversation.")
        return {"result": text, "mode": "memory", "trace": [{"agent": key, "tool": "conversation_history", "input": {},
                                                               "output": {"turns": len(asked)}}]}
    if key == "policy":
        hit = cag.cached_answer(request)
        if hit:
            return {"result": hit, "mode": "cache", "trace": [{"agent": "policy", "tool": "answer_cache", "input": {}, "output": "hit"}]}
    if key == "screening":
        out, trace = crew.run_crew(request)
    else:
        out, trace = run_agent(SPECS[key], request, state.get("memories") or [])
    if key == "policy":
        cag.store_answer(request, out["text"])
    return {"result": out["text"], "mode": out["mode"], "trace": trace}


def remember_node(state: State):
    user = auth.current_user()
    if user.username != "system":
        mag.learn_from_turn(user.username, state["request"], state["result"], state["route"])
    return {"history": [f"Q: {state['request'][:200]} -> A: {state['result'][:200]}"]}


def build(checkpointer=None):
    g = StateGraph(State)
    g.add_node("recall_memory", recall_node)
    g.add_node("route", route_node)
    for key in SPECS:
        g.add_node(key, specialist_node)
    g.add_node("remember", remember_node)
    g.add_edge(START, "recall_memory")
    g.add_edge("recall_memory", "route")
    g.add_conditional_edges("route", lambda s: s["route"], {k: k for k in SPECS})
    for key in SPECS:
        g.add_edge(key, "remember")
    g.add_edge("remember", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


_graph = {"app": None}


def app():
    if _graph["app"] is None:
        _graph["app"] = build()
    return _graph["app"]


def invoke(request, conversation_id="default", route=None):
    """Run one request. `route` skips the router (used by A2A calls addressed to a specific agent)."""
    state = {"request": request}
    if route:  # direct call to one agent: no routing, no memory write
        out = specialist_node({"request": request, "route": route, "memories": []})
        return {"route": route, **out}
    final = app().invoke(state, config={"configurable": {"thread_id": f"{auth.current_user().username}:{conversation_id}"}})
    return {"route": final["route"], "result": final["result"], "mode": final["mode"], "trace": final.get("trace", [])}
