"""The orchestrator, built with LangGraph.

    START -> recall_memory -> route -> {policy | leave | onboarding | screening} -> remember -> END

- recall_memory: long-term memory for this user (MAG) plus the last turns of this conversation (short-term,
  kept by the LangGraph checkpointer per conversation id).
- route: the fast model classifies the request (keywords when no model is available).
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


def recall_node(state: State):
    user = auth.current_user()
    mems = mag.recall(user.username, state["request"]) if user.username != "system" else []
    recent = [f"Earlier in this conversation: {h}" for h in (state.get("history") or [])[-3:]]
    return {"memories": recent + mems}


def keyword_route(text):
    t = text.lower()
    if re.search(r"payslip|pay ?slip|payroll|salary|\bctc\b|\bpf\b|\besi\b|professional tax|\btds\b|take[- ]home|"
                 r"tax regime|hike|increment|appraisal|bonus|in-?hand|gratuity", t):
        return "payroll"
    if re.search(r"insight|analytic|dashboard|metric|kpi|trend|funnel|time to (hire|offer|join)|acceptance rate|"
                 r"pass rate|headcount|needs? (my )?attention|how is hiring|statistics|\bstats\b", t):
        return "insights"
    if re.search(r"inbox|folder|pipeline|interview|\bround\b|\bl\d+\b|\bhr round|final round|offer|joining date|joined|"
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
    request = state["request"]
    if llm.any_model_available():
        options = "\n".join(f"- {k}: {s.description}" for k, s in SPECS.items())
        try:
            res = llm.complete("router", [{"role": "user", "content": request}], tier="fast", max_tokens=20,
                               system=f"Pick the agent for this HR request. Reply with one word from:\n{options}")
            word = res.text.strip().lower().split()[0].strip(".:") if res.text.strip() else ""
            if word in SPECS:
                return {"route": word}
        except (llm.NoModelAvailable, llm.BudgetExceeded):
            pass
    return {"route": keyword_route(request)}


def specialist_node(state: State):
    key, request = state["route"], state["request"]
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
