#!/usr/bin/env bash
# One-time setup on WSL (Ubuntu) with Python 3.14 and a virtual environment.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3.14}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.14 not found. Install it with ONE of:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.14 && PYTHON=\$(uv python find 3.14) ./setup_wsl.sh"
  echo "  sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.14 python3.14-venv"
  exit 1
fi
"$PY" -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
python app.py init --demo-users
python -m unittest discover -s tests -t .
cat <<'NEXT'

Done. Save the admin and demo passwords printed above. Next:
  source .venv/bin/activate
  python app.py demo                 # every agent on sample data
  python app.py serve                # http://localhost:8000 (log in as admin)
  python app.py triggers run         # scheduler: reminders, ticket sweep, budget watch, nightly reindex
Add ANTHROPIC_API_KEY to .env for Claude, or install Ollama and `ollama pull llama3.1:8b` for a free local model.
For real PRs from the ticket agent: `gh auth login` once.
NEXT
