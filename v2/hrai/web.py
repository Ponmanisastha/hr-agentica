"""Web console and HTTP API (standard library server), plus the A2A endpoints.

Login sets an HttpOnly, SameSite=Strict session cookie; API clients and A2A peers send `Authorization: Bearer
<token>` instead. POSTs must be JSON, which together with SameSite=Strict blocks cross-site form posts.
"""

import json
import re
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import a2a, auth, config, db, triggers
from . import tools as T
from .gateway import agent_gateway as G
from .gateway import llm
from .ops import ticket_agent, tracker

PAGE = config.ROOT / "web" / "index.html"
MAX_BODY = 1_000_000
MAX_UPLOAD_BODY = 30_000_000  # resume uploads (base64 JSON)
MAX_FILE = 5_000_000
COOKIE = "hrai_session"


class Handler(BaseHTTPRequestHandler):
    server_version = "hrai/2"

    def log_message(self, fmt, *args):  # keep request logs quiet (and free of tokens)
        pass

    # ---------------------------------------------------------------- plumbing

    def _send(self, code, body, ctype="application/json", headers=None):
        data = (body if isinstance(body, str) else json.dumps(body, default=str)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                    "script-src 'self' 'unsafe-inline'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _token(self):
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            return h[7:].strip()
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c[COOKIE].value if COOKIE in c else None

    def _user(self):
        return auth.user_for_token(self._token())

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > (MAX_UPLOAD_BODY if self.path.startswith("/api/hiring/upload") else MAX_BODY):
            raise ValueError("Request body too large")
        if n and "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("POST bodies must be application/json")
        return json.loads(self.rfile.read(n) or b"{}")

    def _base(self):
        return f"http://{self.headers.get('Host', 'localhost')}"

    # ---------------------------------------------------------------- routes

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if path == "/.well-known/agent-card.json":
            return self._send(200, a2a.agent_card("hr", self._base()))
        m = re.fullmatch(r"/a2a/(\w+)/\.well-known/agent-card\.json", path)
        if m:
            try:
                return self._send(200, a2a.agent_card(m.group(1), self._base()))
            except KeyError:
                return self._send(404, {"error": "no such agent"})
        user = self._user()
        if not user:
            return self._send(401, {"error": "login required"})
        reset = auth.set_current_user(user)
        try:
            return self._get_api(path, user)
        except PermissionError as exc:
            return self._send(403, {"error": str(exc)})
        finally:
            auth._current.reset(reset)

    def _get_api(self, path, user):
        if path.startswith("/api/hiring/"):
            return self._get_hiring(path)
        if path == "/api/me":
            return self._send(200, {"username": user.username, "role": user.role, "employee_id": user.employee_id})
        if path == "/api/approvals":
            auth.require("approvals:decide")
            return self._send(200, db.q("SELECT id, kind, summary, status, requested_by, created_at FROM approvals "
                                        "WHERE status='pending' ORDER BY id DESC"))
        if path == "/api/tickets":
            auth.require("tickets:view")
            return self._send(200, [{k: t[k] for k in ("id", "source", "title", "kind", "severity", "status", "occurrences",
                                                       "pr_url", "updated_at")} for t in tracker.list_tickets()])
        m = re.fullmatch(r"/api/tickets/(\d+)", path)
        if m:
            auth.require("tickets:view")
            t = tracker.get(int(m.group(1)))
            return self._send(200 if t else 404, t or {"error": "not found"})
        if path == "/api/budget":
            auth.require("budget:view")
            return self._send(200, llm.budget_report())
        if path == "/api/outbox":
            auth.require("tickets:view")
            return self._send(200, db.q("SELECT * FROM outbox ORDER BY id DESC LIMIT 50"))
        if path == "/api/triggers":
            auth.require("tickets:view")
            return self._send(200, {"schedules": [{"name": n, "every_s": s["every"], "daily": s["daily"], "what": s["doc"]}
                                                  for n, s in triggers.SCHEDULES.items()],
                                    "recent_runs": db.q("SELECT * FROM trigger_runs ORDER BY id DESC LIMIT 20")})
        return self._send(404, {"error": "not found"})

    def _ok_or_error(self, out):
        return self._send(400 if isinstance(out, dict) and "error" in out else 200, out)

    def _get_hiring(self, path):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        if path == "/api/hiring/board":
            out = T.run("pipeline_summary", job_id=qs.get("job", [""])[0])
            return self._ok_or_error(out)
        if path == "/api/hiring/followups":
            return self._ok_or_error(T.run("list_followups", days_ahead=int(qs.get("days", ["14"])[0])))
        m = re.fullmatch(r"/api/hiring/candidates/([\w-]+)", path)
        if m:
            return self._ok_or_error(T.run("candidate_timeline", candidate=m.group(1)))
        return self._send(404, {"error": "not found"})

    def _post_hiring(self, path, body):
        import base64
        from . import hiring
        if path == "/api/hiring/upload":
            auth.require("agent:recruitment")
            job = db.q1("SELECT id FROM jobs WHERE id=?", (body.get("job_id", ""),))
            if not job:
                raise ValueError("Pick a job for these resumes")
            folder = hiring.inbox_root() / job["id"]
            folder.mkdir(parents=True, exist_ok=True)
            saved = []
            for f in body.get("files", [])[:50]:
                name = re.sub(r"[^\w.\- ]", "_", f.get("name", ""))[:120].strip()
                if not name or "." not in name or name.rsplit(".", 1)[1].lower() not in ("txt", "md", "pdf", "docx"):
                    raise ValueError(f"Unsupported file {f.get('name')!r}: use .pdf, .docx, .txt or .md")
                data = base64.b64decode(f.get("content_b64", ""), validate=True)
                if len(data) > MAX_FILE:
                    raise ValueError(f"{name} is larger than 5 MB")
                (folder / name).write_bytes(data)
                saved.append(name)
            return self._ok_or_error({"saved": saved, **T.run("ingest_resumes", job_id=job["id"])})
        m = re.fullmatch(r"/api/hiring/candidates/([\w-]+)/(\w+)", path)
        if m:
            cid, action = m.groups()
            calls = {
                "move": lambda: T.run("move_candidate", candidate=cid, stage=body.get("stage", ""), note=body.get("note", "")),
                "schedule": lambda: T.run("schedule_interview", candidate=cid, round_name=body.get("round", ""),
                                          when=body.get("when", ""), interviewer=body.get("interviewer", ""),
                                          mode=body.get("mode", "Video call")),
                "result": lambda: T.run("record_interview_result", candidate=cid, round_name=body.get("round", ""),
                                        result=body.get("result", ""), rating=int(body.get("rating") or 0),
                                        feedback=body.get("feedback", "")),
                "offer": lambda: T.run("make_offer", candidate=cid, ctc_lpa=float(body.get("ctc_lpa") or 0),
                                       joining_date=body.get("joining_date", "")),
                "offer_response": lambda: T.run("record_offer_response", candidate=cid, accepted=bool(body.get("accepted")),
                                                joining_date=body.get("joining_date", "")),
                "joined": lambda: T.run("mark_joined", candidate=cid),
            }
            if action not in calls:
                return self._send(404, {"error": "unknown action"})
            return self._ok_or_error(calls[action]())
        m = re.fullmatch(r"/api/hiring/followups/(\d+)/done", path)
        if m:
            return self._ok_or_error(T.run("complete_followup", followup_id=int(m.group(1)), note=body.get("note", "")))
        m = re.fullmatch(r"/api/hiring/jobs/([\w-]+)/rounds", path)
        if m:
            return self._ok_or_error(T.run("set_interview_rounds", job_id=m.group(1), rounds=body.get("rounds", [])))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send(400, {"error": str(exc)})
        if path == "/api/login":
            try:
                token = auth.login(body.get("username"), body.get("password"))
            except auth.AuthError as exc:
                return self._send(401, {"error": str(exc)})
            user = auth.user_for_token(token)
            return self._send(200, {"username": user.username, "role": user.role},
                              headers={"Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"})
        user = self._user()
        m = re.fullmatch(r"/a2a/(\w+)", path)
        if m:
            if not user:
                return self._send(401, {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32004, "message": "Unauthorized"}})
            return self._send(200, a2a.handle_jsonrpc(m.group(1), body, user))
        if not user:
            return self._send(401, {"error": "login required"})
        reset = auth.set_current_user(user)
        try:
            return self._post_api(path, body, user)
        except G.GatewayError as exc:
            return self._send(exc.status, {"error": str(exc), "ticket_id": exc.ticket_id})
        except PermissionError as exc:
            return self._send(403, {"error": str(exc)})
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        finally:
            auth._current.reset(reset)

    def _post_api(self, path, body, user):
        if path.startswith("/api/hiring/"):
            return self._post_hiring(path, body)
        if path == "/api/logout":
            auth.logout(self._token())
            return self._send(200, {"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/"})
        if path == "/api/ask":
            out = G.handle(body.get("request", ""), user=user, channel="web", conversation_id=str(body.get("conversation_id", "web")))
            return self._send(200, out)
        if path == "/api/feedback":
            return self._send(200, G.feedback(user, body.get("request", ""), body.get("answer", ""), int(body.get("rating", 0)),
                                              body.get("comment", ""), body.get("agent", "")))
        m = re.fullmatch(r"/api/approvals/(\d+)", path)
        if m:
            return self._send(200, T.decide_approval(int(m.group(1)), bool(body.get("approve")), body.get("note", "")))
        m = re.fullmatch(r"/api/tickets/(\d+)/(work|decide)", path)
        if m:
            tid = int(m.group(1))
            if m.group(2) == "work":
                auth.require("tickets:approve")
                return self._send(200, ticket_agent.work(tid))
            t = ticket_agent.decide(tid, bool(body.get("approve")), body.get("note", ""))
            return self._send(200, {"id": t["id"], "status": t["status"], "resolution": t["resolution"]})
        if path == "/api/budget":
            auth.require("budget:set")
            llm.set_budget(body["agent"], float(body["monthly_usd"]), body.get("on_exceed", "downgrade"))
            return self._send(200, llm.budget_report())
        m = re.fullmatch(r"/api/triggers/(\w+)/fire", path)
        if m:
            auth.require("tickets:approve")
            if m.group(1) not in triggers.SCHEDULES:
                return self._send(404, {"error": "no such trigger"})
            return self._send(200, triggers.fire(m.group(1)))
        return self._send(404, {"error": "not found"})


def serve(port=8000, host=None):
    from . import automations  # noqa: F401  registers the built-in triggers
    host = host or config.env("HRAI_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"HR console on http://localhost:{port}   A2A card: http://localhost:{port}/.well-known/agent-card.json")
    server.serve_forever()
