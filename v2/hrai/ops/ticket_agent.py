"""The engineering harness: an agent that works tickets from the tracker through to a reviewed, approved fix.

LangGraph workflow, one durable thread per ticket (checkpoints in var/ticket_graph.db, so a ticket can wait for
approval across restarts):

    triage -> propose_fix -> test -> ai_review --(changes needed, max 2 rounds)--> propose_fix
                                       |
                                       v
                                  raise_pr  -> await_approval (human: interrupt) -> finish -> END

- triage: kind, severity and component (fast model, or rules).
- propose_fix: knowledge gaps become a "pending HR confirmation" entry in data/kb_additions.md (never invented
  policy); code bugs get a patch drafted by the smart model from the traceback's files. With no model, a code
  bug goes to `needs_human` with the triage notes.
- test: the patch is applied on branch ticket/<id> in its own git worktree and the test suite runs there.
- ai_review: a separate reviewer prompt (or rule checks offline) approves or requests changes.
- raise_pr: with GitHub configured (HRAI_GITHUB_REPO and `gh auth login`), pushes the branch, opens a PR and
  posts the AI review on it. Without GitHub, the branch stays local.
- await_approval: stops until a human approves or rejects (`app.py tickets approve|reject <id>` or the web
  console, admin only). Nothing is merged before that.
- finish: approve -> merge (gh pr merge, or a local --no-ff merge) and close the ticket with the resolution;
  reject -> close the PR / delete the branch and close the ticket as won't fix.
`app.py tickets sync` also closes tickets whose PR a human merged or closed directly on GitHub.
"""

import difflib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .. import auth, config, db, skills
from ..gateway import llm
from . import tracker

ACTOR = "ticket-agent"
MAX_ROUNDS = 2
FORBIDDEN = re.compile(r"(^|/)(\.env|var/|\.git/)|secrets?", re.I)
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{20,}|(password|api_key)\s*=\s*['\"][^'\"]{6,}", re.I)


class TState(TypedDict, total=False):
    ticket_id: int
    kind: str
    files: list  # [{"path", "content"}] proposed new file contents
    summary: str
    patch: str
    tests_ok: bool
    test_output: str
    review: dict
    rounds: int
    pr_url: str
    decision: dict
    outcome: str


# ---------------------------------------------------------------- git helpers

def git(*args, cwd=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd or config.ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def repo_top():
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=config.ROOT, capture_output=True, text=True)
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 else None


def prefix():
    """Where the app lives inside the git repository ('' at the root, 'v2' when it sits in a subfolder)."""
    top = repo_top()
    return config.ROOT.resolve().relative_to(top) if top else Path()


def ensure_repo():
    """Make the project a git repository (needed for branches and worktrees) unless it already sits in one."""
    if repo_top():
        return False
    git("init", "-q", "-b", config.env("HRAI_BASE_BRANCH", "main"))
    git("add", "-A")
    git("-c", "user.name=HR Ticket Agent", "-c", "user.email=ticket-agent@localhost", "commit", "-q", "-m",
        "Initial commit of hr-agentic v2")
    return True


def _ident():
    return ["-c", f"user.name={config.env('HRAI_GIT_NAME', 'HR Ticket Agent')}",
            "-c", f"user.email={config.env('HRAI_GIT_EMAIL', 'ticket-agent@localhost')}"]


def worktree(tid):
    return config.home() / "worktrees" / f"ticket-{tid}"


def github_repo():
    return config.env("HRAI_GITHUB_REPO")  # e.g. Ponmanisastha/hr-agentica


def gh(*args):
    r = subprocess.run(["gh", *args], cwd=config.ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {r.stderr.strip()}")
    return r.stdout.strip()


# ---------------------------------------------------------------- nodes

def triage(state: TState):
    t = tracker.get(state["ticket_id"])
    kind, severity, component = t["kind"], t["severity"], t["component"]
    if t["source"] == "feedback":
        kind = "knowledge_gap" if re.search(r"policy|handbook|wrong|incorrect|didn.?t know|missing", t["detail"], re.I) else "quality"
    if llm.any_model_available():
        try:
            res = llm.complete("ticket", [{"role": "user", "content":
                "Triage this ticket for an HR agent app. Reply with JSON only: {\"kind\": \"bug|knowledge_gap|quality|"
                "feature\", \"severity\": \"low|medium|high\", \"component\": \"file or area\", \"notes\": \"one line\"}"
                f"\n\nTitle: {t['title']}\n\n{t['detail'][:6000]}"}], tier="fast", max_tokens=300)
            data = json.loads(res.text[res.text.find("{"): res.text.rfind("}") + 1])
            kind, severity = data.get("kind", kind), data.get("severity", severity)
            component = data.get("component", component)
            tracker.event(t["id"], ACTOR, "triage_notes", data.get("notes", ""))
        except Exception:
            pass
    tracker.update(t["id"], ACTOR, "triaged", status="triaged", kind=kind, severity=severity, component=component)
    return {"kind": kind, "rounds": 0}


def _knowledge_fix(t):
    question = re.search(r"Question: (.+)", t["detail"]) or re.search(r"Request: (.+)", t["detail"])
    question = (question.group(1) if question else t["title"]).strip()
    path = config.DATA / "kb_additions.md"
    current = path.read_text(encoding="utf-8") if path.exists() else "# Handbook additions\n\nApproved by HR through the ticket workflow.\n"
    if question.lower() in current.lower():
        return None
    draft = ("HR has not published a policy on this yet. Until HR confirms one, the assistant should say so and "
             "point the employee to hr@example.com.")
    if llm.any_model_available():
        try:
            draft = llm.complete("ticket", [{"role": "user", "content":
                "Write a 1-3 sentence holding answer for an HR handbook gap. Do NOT invent policy; say HR will confirm "
                f"and give hr@example.com. Question: {question}"}], tier="fast", max_tokens=200).text.strip() or draft
        except Exception:
            pass
    section = f"\n## Pending HR confirmation: {question.rstrip('?')}\nQuestion asked: {question}\n{draft}\n"
    return {"summary": f"Add a pending-HR-confirmation entry for: {question}",
            "files": [{"path": "data/kb_additions.md", "content": current.rstrip("\n") + "\n" + section}]}


def _code_fix(t, feedback=""):
    paths = sorted(set(re.findall(r"((?:hrai|tests|app)[\w/]*\.py)", t["detail"] + " " + (t["component"] or ""))))[:3]
    if not paths:
        paths = ["hrai/tools.py"]
    sources = "\n\n".join(f"--- {p} ---\n{(config.ROOT / p).read_text(encoding='utf-8')}" for p in paths if (config.ROOT / p).exists())
    skill = (skills.get("ticket-fix-review") or {}).get("body", "")
    retry_note = ("Reviewer / test feedback on your last attempt:\n" + feedback) if feedback else ""
    res = llm.complete("ticket", [{"role": "user", "content":
        f"{skill}\n\nFix this ticket. Reply with JSON only: {{\"summary\": \"one line\", \"files\": [{{\"path\": "
        "\"relative/path.py\", \"content\": \"the COMPLETE new file content\"}]}}. Change as little as possible; "
        f"include a test in tests/ if the bug is in code.\n\nTicket #{t['id']}: {t['title']}\n{t['detail'][:8000]}\n\n"
        f"{retry_note}\n\nFiles:\n{sources}"}],
        tier="smart", max_tokens=16000)
    text = res.text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def _diff(files, base=None):
    base = base or config.ROOT
    out = []
    for f in files:
        old_path = base / f["path"]
        old = old_path.read_text(encoding="utf-8").splitlines(keepends=True) if old_path.exists() else []
        new = f["content"].splitlines(keepends=True)
        if new and not new[-1].endswith("\n"):
            new[-1] += "\n"
        shown = (prefix() / f["path"]).as_posix()
        out += difflib.unified_diff(old, new, f"a/{shown}" if old else "/dev/null", f"b/{shown}")
    return "".join(out)


def propose_fix(state: TState):
    t = tracker.get(state["ticket_id"])
    tracker.update(t["id"], ACTOR, "fixing", status="fixing")
    feedback = ""
    if state.get("review"):
        feedback = json.dumps(state["review"]) + "\n" + (state.get("test_output") or "")[-3000:]
    try:
        if state["kind"] == "knowledge_gap":
            fix = _knowledge_fix(t)
            if fix is None:
                return {"outcome": "already_covered"}
        elif llm.any_model_available():
            fix = _code_fix(t, feedback)
        else:
            return {"outcome": "needs_human"}
    except (llm.NoModelAvailable, llm.BudgetExceeded, json.JSONDecodeError, ValueError) as exc:
        tracker.event(t["id"], ACTOR, "fix_failed", str(exc)[:500])
        return {"outcome": "needs_human"}
    bad = [f["path"] for f in fix["files"] if FORBIDDEN.search(f["path"]) or ".." in f["path"]]
    if bad:
        tracker.event(t["id"], ACTOR, "fix_rejected", f"touches forbidden paths: {bad}")
        return {"outcome": "needs_human"}
    patch = _diff(fix["files"])
    tracker.update(t["id"], ACTOR, "patch_proposed", patch=patch)
    return {"files": fix["files"], "summary": fix.get("summary", ""), "patch": patch, "rounds": state.get("rounds", 0) + 1,
            "outcome": ""}


def test(state: TState):
    tid = state["ticket_id"]
    wt, branch = worktree(tid), f"ticket/{tid}"
    # A stale worktree may still hold this branch (an earlier run, or another HRAI_HOME): free it first.
    listing = git("worktree", "list", "--porcelain", check=False).split("\n\n")
    for block in listing:
        if f"branch refs/heads/{branch}" in block:
            git("worktree", "remove", "--force", block.split("\n")[0].removeprefix("worktree "), check=False)
    if wt.exists():
        git("worktree", "remove", "--force", str(wt), check=False)
    git("branch", "-D", branch, check=False)
    git("worktree", "prune", check=False)
    wt.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-q", "-b", branch, str(wt), "HEAD")
    app_dir = wt / prefix()
    for f in state["files"]:
        target = app_dir / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"] if f["content"].endswith("\n") else f["content"] + "\n", encoding="utf-8")
    cmd = config.env("HRAI_TICKET_TEST_CMD") or f"{shlex.quote(sys.executable)} -m unittest discover -s tests -t ."
    test_home = config.home() / "worktrees" / f"ticket-{tid}-home"
    env = {**os.environ, "HRAI_HOME": str(test_home), "HRAI_MODE": "mock", "HRAI_EMBEDDINGS": "hash",
           "HRAI_TICKET_TEST_CMD": "true"}
    r = subprocess.run(cmd, shell=True, cwd=app_dir, capture_output=True, text=True, env=env, timeout=900)
    shutil.rmtree(test_home, ignore_errors=True)
    ok = r.returncode == 0
    output = (r.stdout + r.stderr)[-6000:]
    tracker.update(tid, ACTOR, "tests_passed" if ok else "tests_failed", branch=branch, test_output=output)
    return {"tests_ok": ok, "test_output": output}


def _rules_review(state):
    issues = []
    if not state["tests_ok"]:
        issues.append("Tests fail with this patch.")
    if len(state["patch"].splitlines()) > 400:
        issues.append("Patch is over 400 lines; split it.")
    if SECRET.search(state["patch"]):
        issues.append("Patch appears to contain a secret.")
    for f in state["files"]:
        if FORBIDDEN.search(f["path"]):
            issues.append(f"Touches a forbidden path: {f['path']}")
        if f["path"].endswith(".py"):
            try:
                compile(f["content"], f["path"], "exec")
            except SyntaxError as exc:
                issues.append(f"Syntax error in {f['path']}: {exc}")
    return {"verdict": "approve" if not issues else "request_changes", "reviewer": "rules",
            "summary": "Rule checks passed: tests green, small diff, no secrets, allowed paths." if not issues else "Rule checks failed.",
            "issues": issues}


def ai_review(state: TState):
    review = _rules_review(state)
    if review["verdict"] == "approve" and llm.any_model_available():
        t = tracker.get(state["ticket_id"])
        skill = (skills.get("ticket-fix-review") or {}).get("body", "")
        try:
            res = llm.complete("ticket", [{"role": "user", "content":
                f"{skill}\n\nYou are the code reviewer (not the author). Review this patch for ticket #{t['id']} "
                f"({t['title']}).\nTests: {'passed' if state['tests_ok'] else 'FAILED'}\n\n{state['patch'][:20000]}\n\n"
                "Reply with JSON only: {\"verdict\": \"approve|request_changes\", \"summary\": \"...\", \"issues\": []}"}],
                tier="smart", max_tokens=3000)
            ai = json.loads(res.text[res.text.find("{"): res.text.rfind("}") + 1])
            review = {**ai, "reviewer": f"ai:{res.model}", "rule_checks": "passed"}
        except Exception as exc:
            review["note"] = f"AI review unavailable ({str(exc)[:120]}); rule checks only."
    tracker.update(state["ticket_id"], ACTOR, f"review_{review['verdict']}", review=json.dumps(review))
    return {"review": review}


def after_review(state: TState):
    if state["review"]["verdict"] == "approve":
        return "raise_pr"
    return "propose_fix" if state.get("rounds", 0) < MAX_ROUNDS and state["kind"] != "knowledge_gap" else "needs_human"


def raise_pr(state: TState):
    tid, wt = state["ticket_id"], worktree(state["ticket_id"])
    t = tracker.get(tid)
    git("add", "-A", cwd=wt)
    git(*_ident(), "commit", "-q", "-m", f"Ticket #{tid}: {state['summary'] or t['title']}\n\nAutomated fix by the HR ticket agent; "
        "awaiting human approval.", cwd=wt)
    pr_url = ""
    if github_repo():
        try:
            git("push", "-u", "origin", f"ticket/{tid}", cwd=wt)
            body = (f"Fixes ticket #{tid}: {t['title']}\n\n**Summary:** {state['summary']}\n\n**Tests:** passed\n\n"
                    f"**AI review ({state['review'].get('reviewer')}):** {state['review'].get('summary')}\n\n"
                    "Opened by the HR ticket agent. Needs a human approval before merge.")
            pr_url = gh("pr", "create", "--repo", github_repo(), "--base", config.env("HRAI_BASE_BRANCH", "main"),
                        "--head", f"ticket/{tid}", "--title", f"Ticket #{tid}: {(state['summary'] or t['title'])[:80]}",
                        "--body", body).splitlines()[-1]
            issues = "\n".join(f"- {i}" for i in state["review"].get("issues") or []) or "- none"
            gh("pr", "comment", pr_url, "--body", f"### Automated code review\n\nVerdict: **{state['review']['verdict']}**\n\n"
               f"{state['review'].get('summary')}\n\nIssues:\n{issues}")
        except Exception as exc:
            tracker.event(tid, ACTOR, "pr_failed", str(exc)[:500])
    tracker.update(tid, ACTOR, "awaiting_approval", status="awaiting_approval", pr_url=pr_url)
    return {"pr_url": pr_url}


def await_approval(state: TState):
    decision = interrupt({"ticket_id": state["ticket_id"], "pr_url": state.get("pr_url"), "review": state["review"],
                          "summary": state["summary"]})
    return {"decision": decision}


def finish(state: TState):
    tid, d = state["ticket_id"], state["decision"]
    branch, wt = f"ticket/{tid}", worktree(tid)
    who, note = d.get("by", "?"), d.get("note", "")
    if d.get("approved"):
        if state.get("pr_url"):
            gh("pr", "merge", state["pr_url"], "--squash", "--delete-branch")
            resolution = f"Approved by {who}; PR merged: {state['pr_url']}. {state['summary']}"
            git("pull", "--ff-only", check=False)
        else:
            git(*_ident(), "merge", "--no-ff", "-q", "-m", f"Merge ticket #{tid} (approved by {who})", branch)
            resolution = f"Approved by {who}; merged locally ({git('rev-parse', '--short', 'HEAD')}). {state['summary']}"
        _post_merge(state)
        status = "closed"
    else:
        if state.get("pr_url"):
            gh("pr", "close", state["pr_url"], "--comment", f"Rejected by {who}: {note}", "--delete-branch")
        resolution, status = f"Rejected by {who}: {note or 'no reason given'}", "rejected"
    git("worktree", "remove", "--force", str(wt), check=False)
    git("branch", "-D", branch, check=False)
    tracker.update(tid, who, "closed", status=status, resolution=resolution)
    return {"outcome": status}


def needs_human(state: TState):
    t = tracker.get(state["ticket_id"])
    reason = {"already_covered": "The handbook additions already cover this; closing.",
              }.get(state.get("outcome"), "No safe automatic fix: needs an engineer. Triage notes and context are on the ticket.")
    status = "closed" if state.get("outcome") == "already_covered" else "needs_human"
    tracker.update(t["id"], ACTOR, status, status=status, resolution=reason if status == "closed" else None)
    return {"outcome": status}


def _post_merge(state):
    """Refresh derived knowledge when the fix changed it (CAG cache keys on the handbook hash, so it refreshes itself)."""
    if any(f["path"].startswith("data/") for f in state["files"]):
        from ..knowledge import kag, vectors
        vectors.index_all()
        kag.build()


def build(checkpointer):
    g = StateGraph(TState)
    for name, fn in [("triage", triage), ("propose_fix", propose_fix), ("test", test), ("ai_review", ai_review),
                     ("raise_pr", raise_pr), ("await_approval", await_approval), ("finish", finish),
                     ("needs_human", needs_human)]:
        g.add_node(name, fn)
    g.add_edge(START, "triage")
    g.add_edge("triage", "propose_fix")
    g.add_conditional_edges("propose_fix", lambda s: "needs_human" if s.get("outcome") else "test",
                            {"needs_human": "needs_human", "test": "test"})
    g.add_edge("test", "ai_review")
    g.add_conditional_edges("ai_review", after_review, {"raise_pr": "raise_pr", "propose_fix": "propose_fix",
                                                        "needs_human": "needs_human"})
    g.add_edge("raise_pr", "await_approval")
    g.add_edge("await_approval", "finish")
    g.add_edge("finish", END)
    g.add_edge("needs_human", END)
    return g.compile(checkpointer=checkpointer)


class _Graph:
    """`with _Graph() as g:` the workflow with its durable checkpoint store, closed afterwards."""

    def __enter__(self):
        self.conn = sqlite3.connect(str(config.home() / "ticket_graph.db"), check_same_thread=False)
        return build(SqliteSaver(self.conn))

    def __exit__(self, *exc):
        self.conn.close()


def _cfg(tid):
    return {"configurable": {"thread_id": f"ticket-{tid}"}}


# ---------------------------------------------------------------- entry points

def work(ticket_id):
    """Run the workflow for one ticket until it needs a human (approval or manual fix) or finishes."""
    ensure_repo()
    t = tracker.get(ticket_id)
    if not t or t["status"] not in ("new", "triaged"):
        return {"ticket_id": ticket_id, "status": t["status"] if t else "missing"}
    reset = auth.set_current_user(auth.User(0, ACTOR, "service"))
    try:
        try:
            with _Graph() as g:
                g.invoke({"ticket_id": ticket_id}, config=_cfg(ticket_id))
        except Exception as exc:  # e.g. git not installed or the repo is in a bad state: hand it to a person
            tracker.update(ticket_id, ACTOR, "needs_human", status="needs_human",
                           resolution=f"Ticket agent could not continue: {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        auth._current.reset(reset)
    return {"ticket_id": ticket_id, "status": tracker.get(ticket_id)["status"]}


def work_new_tickets(limit=5):
    return [work(t["id"]) for t in tracker.list_tickets("new")[:limit]]


def decide(ticket_id, approved, note=""):
    """Human approval step. Admin only."""
    user = auth.require("tickets:approve")
    t = tracker.get(ticket_id)
    if not t or t["status"] != "awaiting_approval":
        raise ValueError(f"Ticket {ticket_id} is not awaiting approval")
    tracker.event(ticket_id, user.username, "approved" if approved else "rejected", note)
    with _Graph() as g:
        g.invoke(Command(resume={"approved": approved, "note": note, "by": user.username}), config=_cfg(ticket_id))
    return tracker.get(ticket_id)


def sync_github():
    """Close tickets whose PR a human merged or closed on GitHub directly."""
    out = []
    for t in tracker.list_tickets("awaiting_approval"):
        if not t["pr_url"]:
            continue
        state = json.loads(gh("pr", "view", t["pr_url"], "--json", "state"))["state"]
        if state == "MERGED":
            tracker.update(t["id"], "github", "closed", status="closed", resolution=f"PR merged on GitHub: {t['pr_url']}")
        elif state == "CLOSED":
            tracker.update(t["id"], "github", "closed", status="rejected", resolution=f"PR closed on GitHub: {t['pr_url']}")
        out.append({"ticket_id": t["id"], "pr_state": state})
    return out
