"""MCP (Model Context Protocol) server: exposes the HR tools, the handbook and the skills to any MCP client
(Claude Desktop, Claude Code, IDEs, other agents).

    python app.py mcp                    # stdio (for Claude Desktop / Claude Code)
    python app.py mcp --http --port 8765 # streamable HTTP at http://127.0.0.1:8765/mcp

Authentication: set HRAI_MCP_TOKEN to a service or login token (`python app.py token create <user>`). The
server only lists the tools that user's role may use, and every call runs as that user, with the same
permission checks, hooks and audit log as the web console.
"""

import functools
import inspect

from . import auth, config, db, skills
from . import tools as T
from .gateway.agent_gateway import GatewayError, handle
from .knowledge import vectors


def _as_user(user, fn):
    @functools.wraps(fn)
    def call(**kwargs):
        reset = auth.set_current_user(user)
        try:
            return fn(**kwargs)
        finally:
            auth._current.reset(reset)
    call.__signature__ = inspect.signature(fn)
    return call


def build_server(user):
    from mcp.server.mcpserver import MCPServer
    server = MCPServer("hr-agentic", instructions=(
        "HR tools for an Indian mid-size company: resume screening, onboarding, leave and policy. Emails are only "
        f"drafted, never sent. You are connected as {user.username} ({user.role})."))
    for name, meta in T.REGISTRY.items():
        if user.role in meta["roles"] and name not in ("ask_agent",):
            server.add_tool(_as_user(user, meta["fn"]), name=name, description=meta["description"])

    def ask_hr(question: str) -> dict:
        """Ask the HR assistant anything; it routes to the right agent (policy, leave, onboarding, screening)."""
        try:
            out = handle(question, user=user, channel="mcp")
            return {"agent": out["agent"], "answer": out["result"], "mode": out["mode"]}
        except GatewayError as exc:
            return {"error": str(exc)}
    server.add_tool(ask_hr, name="ask_hr", description=ask_hr.__doc__)

    @server.resource("hr://policy/handbook", name="policy_handbook", mime_type="text/markdown")
    def handbook() -> str:
        return vectors.handbook_text()

    @server.resource("hr://faq/candidates", name="candidate_faq", mime_type="text/markdown")
    def faq() -> str:
        return (config.DATA / "candidate_faq.md").read_text(encoding="utf-8")

    def skill_prompt(body):
        def prompt() -> str:
            return body
        return prompt

    for s in skills.all_skills().values():
        server.prompt(name=s["name"], description=s.get("description", ""))(skill_prompt(s["body"]))
    return server


def main(http=False, port=8765):
    db.init_db()
    user = auth.user_for_token(config.env("HRAI_MCP_TOKEN"))
    if user is None:
        raise SystemExit("Set HRAI_MCP_TOKEN to a valid token (python app.py token create <username>).")
    server = build_server(user)
    if http:
        import anyio
        anyio.run(functools.partial(server.run_streamable_http_async, port=port))
    else:
        server.run("stdio")
