"""Owner console sessions: an opaque random cookie backed by a `sessions` row.

The cookie carries 32 random bytes; the database stores only its sha256, so a
leaked database (or the session list the console shows) contains nothing that
can be replayed as a cookie. A session ends at the EARLIER of:

  * idle:     `session_idle_seconds` after `last_seen_at`
  * absolute: `session_absolute_seconds` after `created_at` (stored as expires_at)

`last_seen_at` is written at most once a minute so authenticated reads stay
reads in the common case.
"""

import hashlib
import secrets
import time
from dataclasses import dataclass, replace

from .. import db
from ..config import get_settings
from ..runtime_settings import runtime_settings

COOKIE_NAME = "aab_session"
_TOUCH_THROTTLE = 60


def _now() -> int:
    return int(time.time())


def hash_cookie(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


@dataclass(frozen=True)
class Session:
    id: str                 # sha256 of the cookie value
    principal_id: str
    username: str
    created_at: int
    last_seen_at: int
    absolute_expires_at: int

    def expires_at(self) -> int:
        """When this session dies if no further requests arrive."""
        idle = self.last_seen_at + runtime_settings().session_idle_seconds
        return min(idle, self.absolute_expires_at)


def create(principal_id: str, ip: str = "", user_agent: str = "") -> tuple[str, int]:
    """Start a session. Returns (cookie_value, absolute_expires_at).

    The cookie value exists only in this return value and the Set-Cookie header.
    """
    value = secrets.token_urlsafe(32)
    now = _now()
    rs = runtime_settings()
    expires = now + rs.session_absolute_seconds
    idle = rs.session_idle_seconds
    with db.connect() as conn:
        # Housekeeping: drop sessions that can no longer authenticate, so the
        # table does not grow with every login.
        conn.execute("DELETE FROM sessions WHERE expires_at <= ? OR last_seen_at + ? <= ?",
                     (now, idle, now))
        conn.execute(
            "INSERT INTO sessions (id, principal_id, created_at, expires_at, last_seen_at,"
            " ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (hash_cookie(value), principal_id, now, expires, now,
             ip[:64], user_agent[:256]))
    return value, expires


def lookup(cookie_value: str | None) -> Session | None:
    """The live session for this cookie, else None. No side effects.

    Expired sessions and sessions of a disabled principal resolve to None.
    """
    if not cookie_value:
        return None
    now = _now()
    idle = runtime_settings().session_idle_seconds
    with db.connect() as conn:
        row = conn.execute(
            "SELECT s.*, p.username FROM sessions s JOIN principals p ON p.id = s.principal_id"
            " WHERE s.id = ? AND p.disabled = 0", (hash_cookie(cookie_value),)).fetchone()
    if row is None:
        return None
    if now >= row["expires_at"] or now >= row["last_seen_at"] + idle:
        return None
    return Session(row["id"], row["principal_id"], row["username"], row["created_at"],
                   row["last_seen_at"], row["expires_at"])


def touch(session: Session) -> Session:
    """Record activity for the idle clock, at most once per minute.

    Returns the session as it now stands (so its expiry reflects the touch).
    """
    now = _now()
    if now - session.last_seen_at < _TOUCH_THROTTLE:
        return session
    with db.connect() as conn:
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, session.id))
    return replace(session, last_seen_at=now)


def delete(session_id: str) -> bool:
    with db.connect() as conn:
        return conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount > 0


def revoke(principal_id: str, session_id: str) -> bool:
    """Delete one of this principal's sessions by its stored id."""
    with db.connect() as conn:
        return conn.execute("DELETE FROM sessions WHERE id = ? AND principal_id = ?",
                            (session_id, principal_id)).rowcount > 0


def revoke_all_except(principal_id: str, keep_id: str | None) -> int:
    """Kill every session of this principal except `keep_id` (None = all)."""
    with db.connect() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE principal_id = ? AND id IS NOT ?",
            (principal_id, keep_id)).rowcount


def list_for(principal_id: str, current_id: str | None = None) -> list[dict]:
    """Live sessions, newest first. `id` is the stored hash, never a cookie."""
    now = _now()
    idle = runtime_settings().session_idle_seconds
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE principal_id = ? ORDER BY created_at DESC",
            (principal_id,)).fetchall()
    out = []
    for r in rows:
        expires = min(r["expires_at"], r["last_seen_at"] + idle)
        if now >= expires:
            continue   # dead sessions are not worth showing (or revoking)
        out.append({"id": r["id"], "created_at": r["created_at"],
                    "last_seen_at": r["last_seen_at"], "expires_at": expires,
                    "ip": r["ip"], "user_agent": r["user_agent"],
                    "current": r["id"] == current_id})
    return out


def set_cookie(response, value: str) -> None:
    """Attach the session cookie with the only flags it may ever carry.

    HttpOnly: page scripts cannot read it. SameSite=Strict: browsers never send
    it on cross-site requests (half of the CSRF defence). Secure in public mode,
    where the broker is only reachable over HTTPS through Cloudflare; left off
    locally so http://127.0.0.1 logins work.
    """
    response.set_cookie(
        COOKIE_NAME, value, max_age=runtime_settings().session_absolute_seconds,
        path="/", httponly=True, samesite="strict", secure=get_settings().public_mode())


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict",
                           secure=get_settings().public_mode())
