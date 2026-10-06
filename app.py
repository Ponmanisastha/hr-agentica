"""HR agentic AI prototype.

  python app.py demo                 run all four agents on sample requests
  python app.py ask "<request>"      send one request to the orchestrator
  python app.py serve [port]         open the web console (default port 8000)
"""

import json
import os
import sys
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hr_agents import tools as T
from hr_agents import voice
from hr_agents.agents import handle

PAGE = Path(__file__).parent / "web" / "index.html"
QUIET_ARGS = {"body", "ranked_candidates", "decision", "notes"}


def demo_requests():
    # Dates are relative to today so the leave rules behave the same whenever the demo runs.
    nxt = lambda days: (T.today() + timedelta(days=days)).isoformat()
    return [
        "Screen all resumes for the Backend Engineer opening and shortlist the best candidates.",
        "Onboard our new hires Priya Raman and Vikram Singh.",
        f"Employee E101 wants annual leave from {nxt(14)} to {nxt(16)}.",
        f"E102 is asking for annual leave from {nxt(3)} to {nxt(9)}.",
        "How many days of maternity leave do we offer?",
        "Make reminder calls for the interviews in the next 3 days.",
        "Make follow-up calls to candidates from yesterday's interviews.",
        "Inbound call from E101: how many sick days do I have left, and do I need a medical certificate?",
    ]


def print_result(req, res):
    print("=" * 78)
    print(f"REQUEST: {req}")
    print(f"ROUTED TO: {res['agent']}  (mode: {res['mode']}, {len(res['trace'])} tool calls)")
    for step in res["trace"]:
        args = ", ".join(f"{k}={v!r}" for k, v in step["input"].items() if k not in QUIET_ARGS)
        print(f"  - {step['tool']}({args[:90]})")
    print("RESULT:")
    print("  " + res["result"].replace("\n", "\n  "))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/samples":
            return self._send(200, json.dumps(demo_requests()), "application/json")
        self._send(200, PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")

    def _voice_webhook(self, form):
        """Twilio webhooks (untested against live Twilio). /voice/inbound greets; /voice/gather answers."""
        path = urlparse(self.path)
        if path.path == "/voice/inbound":
            say = "Hello, this is the HR assistant. How can I help you today?"
            return f'<Response><Gather input="speech" action="/voice/gather" speechTimeout="auto"><Say>{say}</Say></Gather></Response>'
        speech = form.get("SpeechResult", [""])[0]
        caller = form.get("From", ["unknown"])[0]
        ctx = parse_qs(path.query).get("ctx", [""])[0]
        if ctx:  # reply to an outbound reminder or follow-up call: hand it to HR's queue
            voice._append("call_log.json", {"call_id": form.get("CallSid", [""])[0], "to": caller, "phone": caller,
                                            "context": ctx, "status": "answered", "reply": speech})
            say = "Thank you. I've noted that, and HR will confirm by email."
        else:
            say = handle(f"Inbound call from {caller}: {speech}")["result"].removeprefix("Spoken answer: ")
        return f"<Response><Say>{escape(say)}</Say></Response>"

    def do_POST(self):
        if self.path.startswith("/voice/"):
            form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            return self._send(200, self._voice_webhook(form), "text/xml")
        if self.path != "/api/run":
            return self._send(404, "{}", "application/json")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        req = (body.get("request") or "").strip()
        if not req:
            return self._send(400, json.dumps({"error": "empty request"}), "application/json")
        self._send(200, json.dumps(handle(req), default=str), "application/json")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "demo"
    if cmd == "demo":
        voice.reset_state()
        (T.OUT / "leave_decisions.json").unlink(missing_ok=True)
        for req in demo_requests():
            print_result(req, handle(req))
        print("=" * 78)
        print("Outputs written to ./outputs (shortlist, onboarding plans, leave ledger, email drafts, voice call logs).")
    elif cmd == "ask":
        req = " ".join(argv[2:])
        print_result(req, handle(req))
    elif cmd == "serve":
        port = int(argv[2]) if len(argv) > 2 else 8000
        host = os.environ.get("HR_HOST", "127.0.0.1")  # set HR_HOST=0.0.0.0 to expose it, e.g. for Twilio webhooks
        print(f"HR agent console on http://localhost:{port}  (Ctrl+C to stop)")
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
