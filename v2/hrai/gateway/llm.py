"""AI gateway: the one path every model call takes.

Built on LiteLLM, so Claude, Ollama and 100+ other providers share one API. On top of LiteLLM it adds:
- model tiers (smart / fast / local) with a fallback chain, so a failing or unavailable model drops to the next;
- per-agent and total monthly budgets in USD, enforced before each call (downgrade to the free local
  model, or block, when a budget is spent);
- usage and cost logging for every call (tokens, cached tokens, cost, latency, errors) in SQLite;
- Anthropic prompt caching for long, stable system prompts (used by CAG);
- an optional LiteLLM proxy server: set HRAI_GATEWAY_URL and every call goes through that proxy instead
  (see gateway/litellm_proxy.yaml), so virtual keys, team budgets and rate limits can live there too.
"""

import contextvars
import json
import time
import urllib.request
from dataclasses import dataclass, field

from .. import config, db

_agent_ctx = contextvars.ContextVar("hrai_agent", default="unknown")
_ollama_seen = {"at": 0.0, "up": False}


class NoModelAvailable(Exception):
    """No model in the chain could be reached (no API key, Ollama not running, or mock mode)."""


class BudgetExceeded(Exception):
    pass


@dataclass
class LLMResult:
    text: str
    tool_calls: list = field(default_factory=list)  # [{"id", "name", "args"}]
    message: dict = field(default_factory=dict)  # assistant message to append to the history
    model: str = ""
    cost: float = 0.0


def _litellm():
    import litellm
    litellm.suppress_debug_info = True
    litellm.drop_params = True  # e.g. temperature on models that do not take it
    return litellm


def _proxy():
    return config.env("HRAI_GATEWAY_URL")


def model_available(alias):
    if config.mode() == "mock":
        return False
    if _proxy():
        return True
    model = config.MODELS[alias]
    if model.startswith("ollama"):
        if config.mode() == "claude":
            return False
        if time.time() - _ollama_seen["at"] > 60:
            try:
                urllib.request.urlopen(config.OLLAMA_BASE + "/api/tags", timeout=1.5)
                _ollama_seen["up"] = True
            except Exception:
                _ollama_seen["up"] = False
            _ollama_seen["at"] = time.time()
        return _ollama_seen["up"]
    if config.mode() == "local":
        return False
    if model.startswith("anthropic/"):
        return bool(config.env("ANTHROPIC_API_KEY"))
    return True  # other providers: let LiteLLM try with its own env vars


def any_model_available():
    return any(model_available(a) for a in ("smart", "fast", "local"))


# ---------------------------------------------------------------- budgets

def month_start():
    return config.today().replace(day=1).isoformat()


def spent(agent=None):
    sql = "SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_usage WHERE ts >= ?"
    args = [month_start()]
    if agent:
        sql += " AND agent=?"
        args.append(agent)
    return db.q1(sql, args)["s"]


def budget_report():
    rows = []
    for b in db.q("SELECT * FROM budgets ORDER BY agent"):
        s = spent(b["agent"])
        rows.append({"agent": b["agent"], "budget_usd": b["monthly_usd"], "spent_usd": round(s, 4),
                     "used_pct": round(100 * s / b["monthly_usd"], 1) if b["monthly_usd"] else 0,
                     "on_exceed": b["on_exceed"],
                     "calls": db.q1("SELECT COUNT(*) AS n FROM llm_usage WHERE agent=? AND ts>=?",
                                    (b["agent"], month_start()))["n"]})
    total = spent()
    return {"month_from": month_start(), "agents": rows,
            "total": {"budget_usd": config.TOTAL_BUDGET, "spent_usd": round(total, 4)}}


def set_budget(agent, monthly_usd, on_exceed="downgrade"):
    if on_exceed not in ("downgrade", "block"):
        raise ValueError("on_exceed must be downgrade or block")
    db.x("INSERT INTO budgets (agent, monthly_usd, on_exceed) VALUES (?,?,?) "
         "ON CONFLICT(agent) DO UPDATE SET monthly_usd=excluded.monthly_usd, on_exceed=excluded.on_exceed",
         (agent, monthly_usd, on_exceed))


def _apply_budget(agent, tier):
    b = db.q1("SELECT * FROM budgets WHERE agent=?", (agent,))
    over_agent = b is not None and spent(agent) >= b["monthly_usd"]
    over_total = spent() >= config.TOTAL_BUDGET
    if not (over_agent or over_total):
        return tier
    if b and b["on_exceed"] == "block":
        raise BudgetExceeded(f"The {agent} agent has used its monthly budget of ${b['monthly_usd']:.2f}")
    db.audit("gateway", "budget.downgrade", {"agent": agent, "from": tier})
    return "local"  # free Ollama model; raises NoModelAvailable later if Ollama is not running


# ---------------------------------------------------------------- the call

def _record(agent, model, tier, usage, cost, ms, ok=True, error=None):
    from ..auth import current_user
    db.x("INSERT INTO llm_usage (ts, agent, username, model, tier, prompt_tokens, completion_tokens, cached_tokens, "
         "cost_usd, latency_ms, ok, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
         (db.now(), agent, current_user().username, model, tier, usage.get("prompt", 0), usage.get("completion", 0),
          usage.get("cached", 0), cost, ms, int(ok), (error or "")[:500]))


def _call(model, **kwargs):
    """The raw LiteLLM call. Tests replace this."""
    return _litellm().completion(model=model, **kwargs)


def complete(agent, messages, *, system=None, tools=None, tier="smart", cache_system=False, max_tokens=None):
    """Run one chat completion for `agent` through the gateway. Raises NoModelAvailable or BudgetExceeded."""
    if config.mode() == "mock":
        raise NoModelAvailable("mock mode")
    tier = _apply_budget(agent, tier)
    errors = []
    for alias in config.CHAINS[tier]:
        if not model_available(alias):
            continue
        model = config.MODELS[alias]
        kwargs = {"max_tokens": max_tokens or (8000 if alias == "smart" else 4000), "timeout": 120,
                  "metadata": {"hrai_internal": True, "agent": agent}}
        if _proxy():
            model = f"litellm_proxy/hr-{alias}"
            kwargs.update(api_base=_proxy(), api_key=config.env("HRAI_GATEWAY_KEY"))
        elif model.startswith("ollama"):
            kwargs["api_base"] = config.OLLAMA_BASE
        msgs = list(messages)
        if system:
            if cache_system and model.startswith("anthropic/"):
                content = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            else:
                content = system
            msgs = [{"role": "system", "content": content}] + msgs
        if tools:
            kwargs["tools"] = tools
        started = time.time()
        token = _agent_ctx.set(agent)
        try:
            resp = _call(model, messages=msgs, **kwargs)
        except Exception as exc:  # try the next model in the chain
            _record(agent, model, alias, {}, 0.0, int(1000 * (time.time() - started)), ok=False, error=str(exc))
            errors.append(f"{model}: {exc}")
            continue
        finally:
            _agent_ctx.reset(token)
        return _normalise(resp, agent, model, alias, started)
    raise NoModelAvailable("; ".join(errors) or "no model configured (set ANTHROPIC_API_KEY or start Ollama)")


def _normalise(resp, agent, model, alias, started):
    msg = resp.choices[0].message
    calls = []
    for tc in getattr(msg, "tool_calls", None) or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": tc.id, "name": tc.function.name, "args": args})
    u = getattr(resp, "usage", None)
    details = getattr(u, "prompt_tokens_details", None)
    usage = {"prompt": getattr(u, "prompt_tokens", 0) or 0, "completion": getattr(u, "completion_tokens", 0) or 0,
             "cached": (getattr(details, "cached_tokens", 0) or 0) if details else 0}
    try:
        cost = float(_litellm().completion_cost(completion_response=resp)) if not model.startswith("ollama") else 0.0
    except Exception:
        cost = 0.0
    _record(agent, model, alias, usage, cost, int(1000 * (time.time() - started)))
    assistant = {"role": "assistant", "content": msg.content or ""}
    if calls:
        assistant["tool_calls"] = [{"id": c["id"], "type": "function",
                                    "function": {"name": c["name"], "arguments": json.dumps(c["args"])}} for c in calls]
    return LLMResult(text=msg.content or "", tool_calls=calls, message=assistant, model=model, cost=cost)


def track_external_calls():
    """Record cost for LiteLLM calls made by other frameworks (CrewAI) under the current agent name."""
    lt = _litellm()

    def on_success(kwargs, response, start, end):
        meta = (kwargs.get("litellm_params") or {}).get("metadata") or kwargs.get("metadata") or {}
        if meta.get("hrai_internal"):
            return  # our own calls are already recorded by complete()
        try:
            cost = float(lt.completion_cost(completion_response=response))
        except Exception:
            cost = 0.0
        u = getattr(response, "usage", None)
        _record(_agent_ctx.get(), kwargs.get("model", ""), "external",
                {"prompt": getattr(u, "prompt_tokens", 0), "completion": getattr(u, "completion_tokens", 0)},
                cost, int((end - start).total_seconds() * 1000))

    if on_success not in lt.success_callback:
        lt.success_callback.append(on_success)
    return on_success


def agent_scope(agent):
    """`token = agent_scope('screening')` ... `reset_agent_scope(token)`: attribute external calls to an agent."""
    return _agent_ctx.set(agent)


def reset_agent_scope(token):
    _agent_ctx.reset(token)
