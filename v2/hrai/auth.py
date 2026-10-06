"""Authentication and role-based access.

- Passwords are hashed with bcrypt (salted, cost 12). Plain passwords are never stored or logged.
- Login returns a random session token; only its SHA-256 is stored, with an 8-hour expiry.
- Five wrong passwords lock the account for 15 minutes.
- Service tokens (for MCP clients, A2A peers and scripts) are long-lived tokens tied to a user and role.
- Roles: admin > hr > manager > employee. `service` is a machine user with HR-level tool access.
"""

import contextvars
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import bcrypt

from . import db

ROLES = ("admin", "hr", "manager", "employee", "service")
SESSION_HOURS = 8
MAX_FAILED = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 10

# What each role may use. Agents check this before running; tools check it again before acting.
PERMISSIONS = {
    "agent:policy": {"admin", "hr", "manager", "employee", "service"},
    "agent:leave": {"admin", "hr", "manager", "employee", "service"},
    "agent:onboarding": {"admin", "hr", "service"},
    "agent:screening": {"admin", "hr", "service"},
    "agent:recruitment": {"admin", "hr", "service"},
    "agent:insights": {"admin", "hr", "service"},
    "leave:any_employee": {"admin", "hr", "manager", "service"},
    "approvals:decide": {"admin", "hr", "manager"},
    "tickets:view": {"admin", "hr"},
    "tickets:approve": {"admin"},
    "budget:view": {"admin", "hr"},
    "insights:view": {"admin", "hr"},
    "budget:set": {"admin"},
    "users:manage": {"admin"},
}


class AuthError(Exception):
    pass


@dataclass
class User:
    id: int
    username: str
    role: str
    employee_id: str | None = None

    def can(self, permission):
        return self.role in PERMISSIONS.get(permission, set())


SYSTEM = User(0, "system", "admin")  # used by triggers and the CLI when no login is given

_current = contextvars.ContextVar("hrai_user", default=None)


def current_user() -> User:
    return _current.get() or SYSTEM


def set_current_user(user):
    return _current.set(user)


def require(permission):
    user = current_user()
    if not user.can(permission):
        raise PermissionError(f"{user.username} ({user.role}) is not allowed to do {permission}")
    return user


def hash_password(password):
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Password must be at least {MIN_PASSWORD} characters")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def create_user(username, password, role, employee_id=None):
    if role not in ROLES:
        raise AuthError(f"Unknown role {role}; use one of {', '.join(ROLES)}")
    db.x("INSERT INTO users (username, password_hash, role, employee_id, created_at) VALUES (?,?,?,?,?)",
         (username.lower(), hash_password(password), role, employee_id, db.now()))
    db.audit("system", "user.create", {"username": username, "role": role})
    return get_user(username)


def get_user(username):
    row = db.q1("SELECT * FROM users WHERE username=?", (username.lower(),))
    return User(row["id"], row["username"], row["role"], row["employee_id"]) if row else None


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def login(username, password):
    row = db.q1("SELECT * FROM users WHERE username=?", ((username or "").lower(),))
    # Same work whether or not the user exists, so timing does not reveal valid usernames.
    stored = row["password_hash"].encode() if row else bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=12))
    if row and row["locked_until"] and row["locked_until"] > db.now():
        db.audit(username, "login.locked")
        raise AuthError("Account locked after too many failed attempts; try again later")
    ok = bcrypt.checkpw((password or "").encode(), stored) and row is not None
    if not ok:
        if row:
            fails = row["failed_attempts"] + 1
            locked = (datetime.now() + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds") if fails >= MAX_FAILED else None
            db.x("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?", (0 if locked else fails, locked, row["id"]))
        db.audit(username, "login.failed")
        raise AuthError("Wrong username or password")
    db.x("UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=?", (row["id"],))
    db.audit(username, "login.ok")
    return issue_token(row["id"], hours=SESSION_HOURS)


def issue_token(user_id, hours=SESSION_HOURS, kind="login", label=None):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    db.x("INSERT INTO sessions (token_hash, user_id, kind, label, expires_at, created_at) VALUES (?,?,?,?,?,?)",
         (_token_hash(token), user_id, kind, label, expires, db.now()))
    return token


def create_service_token(username, label, days=90):
    user = get_user(username)
    if not user:
        raise AuthError(f"No user {username}")
    return issue_token(user.id, hours=24 * days, kind="service", label=label)


def user_for_token(token):
    if not token:
        return None
    row = db.q1("SELECT u.* , s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
                (_token_hash(token),))
    if not row or row["expires_at"] < db.now():
        return None
    return User(row["id"], row["username"], row["role"], row["employee_id"])


def logout(token):
    db.x("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def bootstrap_admin(password=None):
    """Create the first admin if none exists. Returns the generated password (shown once) or None."""
    if db.q1("SELECT 1 AS y FROM users WHERE role='admin'"):
        return None
    generated = password or secrets.token_urlsafe(12)
    create_user("admin", generated, "admin")
    return None if password else generated
