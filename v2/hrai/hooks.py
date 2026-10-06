"""Hooks: code that runs at fixed points of every request, around the agents.

Points: pre_request, post_response, pre_tool, post_tool, on_error, on_feedback.
A hook receives a dict it may change, or raises HookBlocked to stop the request.
Built-in hooks are below; drop extra ones as .py files in hooks.d/ (see hooks.d/example_hook.py).
"""

import importlib.util
import logging
import re
from collections import defaultdict

from . import config

log = logging.getLogger(__name__)
POINTS = ("pre_request", "post_response", "pre_tool", "post_tool", "on_error", "on_feedback")
_registry = defaultdict(list)
_loaded = {"user": False}


class HookBlocked(Exception):
    pass


def hook(point, order=50):
    if point not in POINTS:
        raise ValueError(f"Unknown hook point {point}")

    def wrap(fn):
        _registry[point].append((order, fn))
        _registry[point].sort(key=lambda p: p[0])
        return fn
    return wrap


def run(point, ctx):
    load_user_hooks()
    for _, fn in list(_registry[point]):
        try:
            fn(ctx)
        except HookBlocked:
            raise
        except Exception as exc:  # a broken hook must not break the request
            log.exception("hook %s failed", fn.__name__)
            if point != "on_error":
                from .ops import tracker
                tracker.capture_exception(exc, {"hook": fn.__name__, "point": point})
    return ctx


def load_user_hooks():
    if _loaded["user"]:
        return
    _loaded["user"] = True
    for path in sorted(config.HOOKS_DIR.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"hrai_user_hook_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)


# ---------------------------------------------------------------- built-in hooks

PII = [
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "[AADHAAR]"),
    (re.compile(r"\+?\d[\d -]{8,}\d"), "[PHONE]"),
    (re.compile(r"\b\d{9,18}\b"), "[ACCOUNT]"),
]
INJECTION = re.compile(
    r"ignore (all |any )?((your|the|previous|prior|above|earlier|system) )+(instructions|rules|prompt)"
    r"|reveal (the |your )?(system prompt|password|api key)"
    r"|you are now (an?|the) |disregard (your|the) (rules|instructions)|act as (an? )?admin", re.I)


def redact(text):
    for pattern, label in PII:
        text = pattern.sub(label, text)
    return text


@hook("pre_request", order=10)
def injection_guard(ctx):
    if INJECTION.search(ctx.get("request", "")):
        raise HookBlocked("This request looks like an attempt to override the assistant's rules, so it was not run.")


@hook("pre_request", order=20)
def redact_for_logs(ctx):
    ctx["log_request"] = redact(ctx.get("request", ""))


@hook("pre_request", order=30)
def length_limit(ctx):
    if len(ctx.get("request", "")) > 8000:
        raise HookBlocked("Request is too long (over 8,000 characters).")


@hook("pre_tool", order=10)
def audit_tool_call(ctx):
    from . import db
    db.audit(ctx.get("username", "?"), "tool.call", {"tool": ctx["tool"], "agent": ctx.get("agent"),
                                                     "args": redact(str(ctx.get("args")))[:500]})


GAP = re.compile(r"(couldn't|could not|can't|cannot) find|not (covered|mentioned) in the (handbook|policy)|no policy (on|about)", re.I)


@hook("post_response", order=50)
def knowledge_gap_to_ticket(ctx):
    """An answer that says the handbook does not cover the question becomes a knowledge-gap ticket."""
    if ctx.get("agent") == "policy" and GAP.search(ctx.get("result", "")):
        from .ops import tracker
        tracker.open_ticket("knowledge_gap", f"Handbook gap: {ctx['request'][:80]}",
                            f"Question: {ctx['request']}\nAnswer given: {ctx['result']}", kind="knowledge_gap",
                            severity="low", component="knowledge", fingerprint_parts=("gap", ctx["request"].lower()))


@hook("on_error", order=10)
def error_to_ticket(ctx):
    from .ops import tracker
    ctx["ticket_id"] = tracker.capture_exception(ctx["error"], {k: v for k, v in ctx.items() if k != "error"})


@hook("on_feedback", order=10)
def negative_feedback_to_ticket(ctx):
    if ctx.get("rating", 0) < 0:
        from .ops import tracker
        ctx["ticket_id"] = tracker.open_ticket(
            "feedback", f"User feedback: {(ctx.get('comment') or ctx['request'])[:80]}",
            f"Request: {ctx['request']}\nAnswer: {ctx.get('answer', '')}\nComment: {ctx.get('comment', '')}",
            kind="feedback", severity="medium", component=ctx.get("agent", ""),
            fingerprint_parts=("feedback", ctx["request"].lower(), ctx.get("comment", "")))
