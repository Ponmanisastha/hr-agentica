"""A2A (Agent2Agent protocol): each HR agent is published as an A2A agent so other agents, inside this app or
anywhere else, can discover it and send it tasks.

- Discovery: GET /.well-known/agent-card.json (the HR assistant, which routes) and
  GET /a2a/<agent>/.well-known/agent-card.json for policy, leave, onboarding and screening.
- Calls: JSON-RPC 2.0 POST to /a2a/<agent> with method "message/send" (A2A 0.3; "SendMessage" from A2A 1.0 is
  accepted too). The answer comes back as a completed Task with a text artifact. "tasks/get" fetches it again.
- Security: bearer token (a login or service token, see `app.py token`), and the caller's role decides which
  agents it may use, exactly as in the web console.
- Inside the app, agents use the `ask_agent` tool, which speaks the same protocol: in-process by default, or
  over HTTP to a remote agent when HRAI_A2A_URL (or HRAI_A2A_URL_<AGENT>) is set. Delegation depth is capped
  at 2 so agents cannot call each other in a loop.
"""

import contextvars
import json
import urllib.request
import uuid
from datetime import datetime, timezone

from . import auth, config

PROTOCOL_VERSION = "0.3.0"
MAX_DEPTH = 2
_depth = contextvars.ContextVar("a2a_depth", default=0)
_tasks = {}  # task id -> task (kept in memory; tasks are short-lived request/response here)


def _specs():
    from .agents.specialists import SPECS
    return SPECS


def agent_card(key, base_url):
    if key == "hr":
        name, desc = "HR assistant", "Routes any HR request to the right specialist agent."
        skills = [{"id": k, "name": s.title, "description": s.description, "tags": ["hr", k], "examples": s.examples}
                  for k, s in _specs().items()]
    else:
        s = _specs()[key]
        name, desc = s.title, s.description
        skills = [{"id": key, "name": s.title, "description": s.description, "tags": ["hr", key], "examples": s.examples}]
    return {"protocolVersion": PROTOCOL_VERSION, "name": name, "description": desc, "url": f"{base_url}/a2a/{key}",
            "preferredTransport": "JSONRPC", "version": "2.0.0",
            "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
            "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"], "skills": skills,
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}, "security": [{"bearer": []}]}


def _text_of(message):
    parts = (message or {}).get("parts") or []
    return "\n".join(p.get("text", "") for p in parts if p.get("kind", "text") == "text" or "text" in p).strip()


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle_jsonrpc(agent_key, body, user):
    """Serve one A2A JSON-RPC request for `agent_key` ('hr' routes to any agent)."""
    from .gateway.agent_gateway import GatewayError, handle
    rid, method, params = body.get("id"), body.get("method"), body.get("params") or {}
    if body.get("jsonrpc") != "2.0":
        return _error(rid, -32600, "Invalid Request")
    if agent_key != "hr" and agent_key not in _specs():
        return _error(rid, -32001, f"No agent {agent_key}")
    if method in ("tasks/get", "GetTask"):
        task = _tasks.get(params.get("id"))
        return {"jsonrpc": "2.0", "id": rid, "result": task} if task else _error(rid, -32001, "Task not found")
    if method not in ("message/send", "SendMessage"):
        return _error(rid, -32601, f"Method not found: {method}")
    message = params.get("message") or {}
    text = _text_of(message)
    if not text:
        return _error(rid, -32602, "message.parts must contain text")
    context_id = message.get("contextId") or str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    try:
        out = handle(text, user=user, channel="a2a", conversation_id=context_id,
                     route=None if agent_key == "hr" else agent_key)
        state, answer, meta = "completed", out["result"], {"agent": out["agent"], "mode": out["mode"]}
    except GatewayError as exc:
        state, answer, meta = ("rejected" if exc.status in (401, 403) else "failed"), str(exc), {"ticket_id": exc.ticket_id}
    reply = {"kind": "message", "role": "agent", "messageId": str(uuid.uuid4()), "parts": [{"kind": "text", "text": answer}],
             "contextId": context_id, "taskId": task_id}
    task = {"kind": "task", "id": task_id, "contextId": context_id,
            "status": {"state": state, "message": reply, "timestamp": datetime.now(timezone.utc).isoformat()},
            "artifacts": [{"artifactId": str(uuid.uuid4()), "name": "answer", "parts": [{"kind": "text", "text": answer}]}]
            if state == "completed" else [],
            "history": [{**message, "contextId": context_id, "taskId": task_id}], "metadata": meta}
    _tasks[task_id] = task
    return {"jsonrpc": "2.0", "id": rid, "result": task}


def send_message(text, context_id=None):
    return {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
            "params": {"message": {"kind": "message", "role": "user", "messageId": str(uuid.uuid4()),
                                   "parts": [{"kind": "text", "text": text}], **({"contextId": context_id} if context_id else {})}}}


class A2AClient:
    """Minimal A2A client over HTTP (standard library only)."""

    def __init__(self, token):
        self.token = token

    def _req(self, url, data=None):
        req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())

    def card(self, base_url, agent=None):
        path = f"/a2a/{agent}/.well-known/agent-card.json" if agent else "/.well-known/agent-card.json"
        return self._req(base_url.rstrip("/") + path)

    def send(self, agent_url, text, context_id=None):
        return self._req(agent_url, send_message(text, context_id))


def answer_of(response):
    if "error" in response:
        return None, response["error"]["message"]
    task = response["result"]
    if task["status"]["state"] != "completed":
        return task, _text_of(task["status"].get("message"))
    return task, "\n".join(_text_of(a) for a in task["artifacts"])


def delegate(agent, question):
    """Used by the ask_agent tool: one agent asks another over A2A."""
    agent = agent.lower().strip()
    if agent not in _specs():
        return {"error": f"Unknown agent {agent}; use one of {', '.join(_specs())}"}
    if _depth.get() >= MAX_DEPTH:
        return {"error": "Delegation depth limit reached; answer with what you have."}
    token = _depth.set(_depth.get() + 1)
    try:
        remote = config.env(f"HRAI_A2A_URL_{agent.upper()}") or (
            config.env("HRAI_A2A_URL") and f"{config.env('HRAI_A2A_URL').rstrip('/')}/a2a/{agent}")
        if remote:
            response, transport = A2AClient(config.env("HRAI_A2A_TOKEN")).send(remote, question), "a2a-http"
        else:
            response, transport = handle_jsonrpc(agent, send_message(question), auth.current_user()), "a2a-local"
    finally:
        _depth.reset(token)
    task, answer = answer_of(response)
    if task is None or task["status"]["state"] != "completed":
        return {"error": answer, "agent": agent, "transport": transport}
    return {"agent": agent, "answer": answer, "task_id": task["id"], "transport": transport}
