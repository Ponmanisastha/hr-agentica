"""Agent gateway: the single front door to the agents, for every channel (CLI, web, A2A, MCP, triggers).

Per request it: authenticates the caller, rate-limits them, runs the pre_request hooks (prompt-injection
guard, PII redaction for logs, size limit), checks the caller's role may use the chosen agent, runs the
LangGraph orchestrator, runs the post_response hooks, writes the audit log, and turns any crash into a
ticket while giving the caller a clean error message.
"""

import threading
import time
from collections import defaultdict, deque

from .. import auth, db, hooks
from ..ops import tracker

RATE_LIMIT = 30  # requests per user per minute
_hits = defaultdict(deque)
_lock = threading.Lock()


class GatewayError(Exception):
    def __init__(self, message, status=400, ticket_id=None):
        super().__init__(message)
        self.status, self.ticket_id = status, ticket_id


def _rate_limit(username):
    now = time.time()
    with _lock:
        q = _hits[username]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            raise GatewayError("Too many requests; please wait a minute.", 429)
        q.append(now)


def authenticate(token=None, user=None):
    if user is not None:
        return user
    u = auth.user_for_token(token)
    if not u:
        raise GatewayError("Please log in (missing or expired token).", 401)
    return u


def handle(request, *, token=None, user=None, channel="cli", conversation_id="default", route=None):
    from ..agents import graph
    user = authenticate(token, user)
    reset = auth.set_current_user(user)
    started = time.time()
    ctx = {"request": (request or "").strip(), "username": user.username, "role": user.role, "channel": channel}
    try:
        if not ctx["request"]:
            raise GatewayError("Empty request.")
        _rate_limit(user.username)
        hooks.run("pre_request", ctx)
        if route and not user.can(f"agent:{route}"):
            raise GatewayError(f"Your role ({user.role}) cannot use the {route} agent.", 403)
        out = graph.invoke(ctx["request"], conversation_id=conversation_id, route=route)
        ctx.update(agent=out["route"], result=out["result"], mode=out["mode"])
        hooks.run("post_response", ctx)
        db.audit(user.username, "request", {"channel": channel, "agent": ctx["agent"], "mode": ctx["mode"],
                                            "request": ctx.get("log_request"), "ms": int(1000 * (time.time() - started))})
        return {"agent": ctx["agent"], "result": ctx["result"], "mode": ctx["mode"], "trace": out.get("trace", [])}
    except hooks.HookBlocked as exc:
        db.audit(user.username, "request.blocked", {"channel": channel, "reason": str(exc)})
        raise GatewayError(str(exc), 400)
    except PermissionError as exc:
        db.audit(user.username, "request.denied", {"channel": channel, "reason": str(exc)})
        raise GatewayError(str(exc), 403)
    except GatewayError:
        raise
    except Exception as exc:
        ctx["error"] = exc
        hooks.run("on_error", ctx)
        raise GatewayError(f"Something went wrong; it has been logged as ticket #{ctx.get('ticket_id')}.", 500,
                           ctx.get("ticket_id"))
    finally:
        auth._current.reset(reset)


def feedback(user, request, answer, rating, comment="", agent=""):
    return tracker.capture_feedback(user.username, request, answer, rating, comment, agent)
