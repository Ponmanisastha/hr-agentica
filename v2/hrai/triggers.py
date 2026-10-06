"""Triggers: things that start agents without anyone typing a request.

- Event triggers react to something happening (`emit("new_hire.created", {...})`).
- Scheduled triggers run on an interval or at a time of day. `python app.py triggers run` keeps a
  scheduler loop going; `python app.py triggers fire <name>` runs one now. Every run is logged in trigger_runs.
The built-in triggers live in hrai/automations.py.
"""

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime

from . import db

log = logging.getLogger(__name__)
_subscribers = defaultdict(list)
SCHEDULES = {}  # name -> {"every": seconds | None, "daily": "HH:MM" | None, "fn": callable, "doc": str}


def on(event_name):
    def wrap(fn):
        _subscribers[event_name].append(fn)
        return fn
    return wrap


def emit(event_name, payload=None, background=False):
    for fn in list(_subscribers[event_name]):
        if background:
            threading.Thread(target=_safe, args=(fn, event_name, payload or {}), daemon=True).start()
        else:
            _safe(fn, event_name, payload or {})


def _safe(fn, name, payload):
    try:
        out = fn(payload)
        db.x("INSERT INTO trigger_runs (name, ts, status, detail) VALUES (?,?,?,?)",
             (f"event:{name}:{fn.__name__}", db.now(), "ok", str(out)[:1000]))
    except Exception as exc:
        db.x("INSERT INTO trigger_runs (name, ts, status, detail) VALUES (?,?,?,?)",
             (f"event:{name}:{fn.__name__}", db.now(), "error", str(exc)[:1000]))
        if name != "ticket.created":  # avoid loops: a failing ticket handler must not create tickets forever
            from .ops import tracker
            tracker.capture_exception(exc, {"trigger": name})


def schedule(name, every=None, daily=None):
    def wrap(fn):
        SCHEDULES[name] = {"every": every, "daily": daily, "fn": fn, "doc": (fn.__doc__ or "").strip().split("\n")[0]}
        return fn
    return wrap


def _last_run(name):
    row = db.q1("SELECT ts FROM trigger_runs WHERE name=? ORDER BY id DESC LIMIT 1", (name,))
    return datetime.fromisoformat(row["ts"]) if row else None


def is_due(name, now=None):
    now = now or datetime.now()
    spec, last = SCHEDULES[name], _last_run(name)
    if spec["every"]:
        return last is None or (now - last).total_seconds() >= spec["every"]
    hh, mm = map(int, spec["daily"].split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now >= target and (last is None or last < target)


def fire(name):
    spec = SCHEDULES[name]
    try:
        out = spec["fn"]()
        status = "ok"
    except Exception as exc:
        out, status = str(exc), "error"
        from .ops import tracker
        tracker.capture_exception(exc, {"trigger": name})
    db.x("INSERT INTO trigger_runs (name, ts, status, detail) VALUES (?,?,?,?)", (name, db.now(), status, str(out)[:2000]))
    return {"trigger": name, "status": status, "result": out}


def run_due(now=None):
    return [fire(n) for n in list(SCHEDULES) if is_due(n, now)]


def run_forever(poll_seconds=30):
    log.info("scheduler started with %s", ", ".join(SCHEDULES))
    while True:
        for r in run_due():
            print(f"[{db.now()}] trigger {r['trigger']}: {r['status']}")
        time.sleep(poll_seconds)
