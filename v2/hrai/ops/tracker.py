"""Error, feedback and quality tracking. Everything that goes wrong becomes a ticket.

Sources: unhandled exceptions in requests, hooks, triggers and tools; ERROR log records; negative user
feedback; answers that admit a knowledge gap. Repeats of the same problem (same fingerprint) bump the
occurrence count on the open ticket instead of creating a new one.
"""

import hashlib
import json
import logging
import traceback

from .. import config, db

OPEN = ("new", "triaged", "fixing", "awaiting_approval", "approved", "pr_open", "needs_human")


def _fp(*parts):
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def event(ticket_id, actor, name, detail=""):
    db.x("INSERT INTO ticket_events (ticket_id, ts, actor, event, detail) VALUES (?,?,?,?,?)",
         (ticket_id, db.now(), actor, name, detail if isinstance(detail, str) else json.dumps(detail, default=str)))


def open_ticket(source, title, detail, kind="bug", severity="medium", component="", fingerprint_parts=None):
    fp = _fp(*(fingerprint_parts or (source, title)))
    existing = db.q1(f"SELECT * FROM tickets WHERE fingerprint=? AND status IN ({','.join('?' * len(OPEN))})",
                     (fp, *OPEN))
    if existing:
        db.x("UPDATE tickets SET occurrences=occurrences+1, updated_at=? WHERE id=?", (db.now(), existing["id"]))
        event(existing["id"], "tracker", "repeated", detail[:500])
        return existing["id"]
    from ..hooks import redact
    tid = db.x("INSERT INTO tickets (source, title, detail, fingerprint, kind, severity, component, status, created_at, "
               "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (source, title[:200], redact(detail)[:20000], fp, kind, severity, component, "new", db.now(), db.now()))
    event(tid, "tracker", "created", f"from {source}")
    from .. import triggers
    triggers.emit("ticket.created", {"ticket_id": tid})
    return tid


def capture_exception(exc, context=None):
    tb = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    ours = [f for f in tb if str(config.ROOT) in f.filename and "/var/" not in f.filename]
    where = ours[-1] if ours else (tb[-1] if tb else None)
    location = f"{where.filename.replace(str(config.ROOT) + '/', '')}:{where.lineno} in {where.name}" if where else "unknown"
    detail = (f"{type(exc).__name__}: {exc}\nLocation: {location}\nContext: {json.dumps(context or {}, default=str)[:2000]}"
              f"\n\nTraceback:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:]}")
    return open_ticket("error", f"{type(exc).__name__} in {where.name if where else '?'}: {str(exc)[:80]}", detail,
                       kind="bug", severity="high", component=location.split(":")[0],
                       fingerprint_parts=(type(exc).__name__, location))


def capture_feedback(username, request, answer, rating, comment="", agent=""):
    from .. import hooks
    ctx = {"username": username, "request": request, "answer": answer, "rating": rating, "comment": comment, "agent": agent}
    hooks.run("on_feedback", ctx)
    fid = db.x("INSERT INTO feedback (ts, username, request, answer, rating, comment, ticket_id) VALUES (?,?,?,?,?,?,?)",
               (db.now(), username, request, answer, rating, comment, ctx.get("ticket_id")))
    return {"feedback_id": fid, "ticket_id": ctx.get("ticket_id")}


def get(ticket_id):
    t = db.q1("SELECT * FROM tickets WHERE id=?", (ticket_id,))
    if t:
        t["events"] = db.q("SELECT ts, actor, event, detail FROM ticket_events WHERE ticket_id=? ORDER BY id", (ticket_id,))
    return t


def list_tickets(status=None):
    if status == "open":
        return db.q(f"SELECT * FROM tickets WHERE status IN ({','.join('?' * len(OPEN))}) ORDER BY id DESC", OPEN)
    if status:
        return db.q("SELECT * FROM tickets WHERE status=? ORDER BY id DESC", (status,))
    return db.q("SELECT * FROM tickets ORDER BY id DESC")


def update(ticket_id, actor, event_name, **fields):
    fields["updated_at"] = db.now()
    sets = ", ".join(f"{k}=?" for k in fields)
    db.x(f"UPDATE tickets SET {sets} WHERE id=?", (*fields.values(), ticket_id))
    event(ticket_id, actor, event_name, {k: v for k, v in fields.items() if k not in ("patch", "updated_at", "test_output")})


class TicketLogHandler(logging.Handler):
    """Turns ERROR log records from the app into tickets."""

    def emit(self, record):
        if record.levelno < logging.ERROR or record.name.startswith("hrai.ops"):
            return
        try:
            if record.exc_info and record.exc_info[1]:
                capture_exception(record.exc_info[1], {"logger": record.name, "message": record.getMessage()})
            else:
                open_ticket("log", f"{record.name}: {record.getMessage()[:100]}", record.getMessage(),
                            fingerprint_parts=("log", record.name, record.getMessage()[:100]))
        except Exception:
            pass


def install():
    root = logging.getLogger("hrai")
    if not any(isinstance(h, TicketLogHandler) for h in root.handlers):
        root.addHandler(TicketLogHandler())
