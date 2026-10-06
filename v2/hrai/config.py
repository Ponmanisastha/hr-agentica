"""Settings, read from environment variables and an optional .env file in the project root."""

import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path=ROOT / ".env"):
    """Minimal .env loader (KEY=VALUE lines) so python-dotenv is not needed. Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def env(key, default=""):
    return os.environ.get(key, default)


def home() -> Path:
    """Where runtime state lives (database, vectors, outbox, worktrees). Tests point it at a temp folder."""
    path = Path(env("HRAI_HOME", str(ROOT / "var")))
    path.mkdir(parents=True, exist_ok=True)
    return path


DATA = ROOT / "data"
SKILLS_DIR = ROOT / "skills"
HOOKS_DIR = ROOT / "hooks.d"

# Model tiers. Every call goes through the AI gateway (hrai/gateway/llm.py), which picks the first
# available model in the tier's chain and falls back down it.
MODELS = {
    "smart": env("HRAI_SMART_MODEL", "anthropic/claude-sonnet-5-5"),
    "fast": env("HRAI_FAST_MODEL", "anthropic/claude-haiku-4-5-20251001"),
    "local": env("HRAI_LOCAL_MODEL", "ollama_chat/llama3.1:8b"),
}
CHAINS = {"smart": ["smart", "fast", "local"], "fast": ["fast", "local"], "local": ["local"]}
OLLAMA_BASE = env("OLLAMA_API_BASE", "http://localhost:11434")

# Monthly budgets in USD per agent. Override with HRAI_BUDGET_<AGENT>=amount or `app.py budget set`.
DEFAULT_BUDGETS = {
    "router": 1.0, "policy": 5.0, "leave": 5.0, "onboarding": 5.0,
    "screening": 10.0, "recruitment": 5.0, "insights": 3.0, "payroll": 5.0, "projects": 5.0, "culture": 3.0, "memory": 1.0, "ticket": 15.0,
}
TOTAL_BUDGET = float(env("HRAI_TOTAL_BUDGET", "40"))


def mode():
    """auto: Claude if a key is set, else Ollama if running, else the offline scripted planner.
    claude / local / mock force one path."""
    return env("HRAI_MODE", "auto").lower()


def today() -> date:
    pinned = env("HRAI_TODAY")
    return date.fromisoformat(pinned) if pinned else date.today()
