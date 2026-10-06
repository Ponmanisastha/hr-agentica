"""Shared test setup: one temporary home for the whole run, offline settings, seeded data and test users."""

import atexit
import os
import shutil
import tempfile
import warnings

warnings.simplefilter("ignore", ResourceWarning)
HOME = tempfile.mkdtemp(prefix="hrai-test-")
os.environ.update(HRAI_HOME=HOME, HRAI_INBOX=os.path.join(HOME, "inbox"), HRAI_POLICY_DIR=os.path.join(HOME, "policies"),
                  HRAI_DOCS_DIR=os.path.join(HOME, "documents"),
                  HRAI_MODE="mock", HRAI_EMBEDDINGS="hash",
                  HRAI_TODAY="2026-10-06")
for key in ("ANTHROPIC_API_KEY", "HRAI_GITHUB_REPO", "HRAI_GATEWAY_URL", "HRAI_A2A_URL"):
    os.environ.pop(key, None)
atexit.register(shutil.rmtree, HOME, True)

from hrai import auth, automations, db  # noqa: E402,F401  automations registers triggers
from hrai.knowledge import kag, vectors  # noqa: E402
from hrai.ops import tracker  # noqa: E402

ADMIN = auth.User(0, "tester", "admin")
_state = {"ready": False}


def bootstrap():
    if _state["ready"]:
        return
    db.init_db()
    tracker.install()
    vectors.index_all()
    kag.build()
    auth.create_user("hr1", "hr-password-1", "hr")
    auth.create_user("deepa", "deepa-password", "employee", "E101")
    _state["ready"] = True


def as_user(user):
    class Ctx:
        def __enter__(self):
            self.t = auth.set_current_user(user)

        def __exit__(self, *a):
            auth._current.reset(self.t)
    return Ctx()
