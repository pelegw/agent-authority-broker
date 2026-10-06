"""Owner-minted tokens: `aab_admin_<48 hex>` for the CLI, scripts and deploys,
and `aab_monitor_<48 hex>` for an uptime monitor.

Minted from an authenticated admin session or token, stored only as sha256,
and shown in plaintext exactly once (the create response). Optional expiry,
revocable, and listed with a throttled last-used time. There is no static
admin token in the environment: every admin credential is one of these rows or
a login session, so each one can be revoked individually.

The scope is fixed at creation and spelled in the prefix, so a glance at a
token says what it opens: an `admin` token is an owner credential for the
whole management plane; a `monitor` token is accepted by /health and
/v1/health alone (routers/health.py), where it turns the liveness probe
into the full health summary, and by nothing else. authenticate() takes the
scope the caller expects, so neither kind can stand in for the other.
"""

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

from .. import db

PREFIX = "aab_admin_"
MONITOR_PREFIX = "aab_monitor_"
PREFIXES = {"admin": PREFIX, "monitor": MONITOR_PREFIX}
SCOPES = tuple(PREFIXES)
# Refresh last_used_at at most this often (as WA_GW's _touch_last_used), so
# token auth is a read in the common case rather than a write per request.
_LAST_USED_THROTTLE = 60


def _now() -> int:
    return int(time.time())


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8", "surrogateescape")).hexdigest()


@dataclass(frozen=True)
class TokenAuth:
    id: str
    principal_id: str
    username: str
    expires_at: int | None
    name: str = ""
    scope: str = "admin"


def create(principal_id: str, name: str, expires_in_hours: int | None = None,
           scope: str = "admin") -> dict:
    """Mint a token. The returned dict carries `token` (plaintext) exactly once."""
    if scope not in PREFIXES:
        raise ValueError(f"unknown token scope {scope!r}")
    plaintext = PREFIXES[scope] + secrets.token_hex(24)
    now = _now()
    expires_at = now + expires_in_hours * 3600 if expires_in_hours else None
    tid = str(uuid.uuid4())
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO admin_tokens (id, principal_id, name, token_hash, created_at,"
            " expires_at, scope) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tid, principal_id, name, hash_token(plaintext), now, expires_at, scope))
    return {"id": tid, "name": name, "created_at": now, "expires_at": expires_at,
            "scope": scope, "token": plaintext}


def authenticate(plaintext: str, scope: str = "admin") -> TokenAuth | None:
    """Resolve a presented token of the expected scope to its owner, or
    None (= 401).

    Revoked, expired, disabled-principal and other-scope tokens all resolve
    to None: the prefix is checked first (no database work for a token of
    the wrong kind) and the stored scope again, so a forged prefix on a real
    token of the other scope still fails.
    """
    if scope not in PREFIXES or not plaintext.startswith(PREFIXES[scope]):
        return None
    now = _now()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT t.*, p.username FROM admin_tokens t"
            " JOIN principals p ON p.id = t.principal_id"
            " WHERE t.token_hash = ? AND t.scope = ? AND t.revoked = 0 AND p.disabled = 0"
            " AND (t.expires_at IS NULL OR t.expires_at > ?)",
            (hash_token(plaintext), scope, now)).fetchone()
        if row is None:
            return None
        if now - (row["last_used_at"] or 0) >= _LAST_USED_THROTTLE:
            conn.execute("UPDATE admin_tokens SET last_used_at = ? WHERE id = ?",
                         (now, row["id"]))
    return TokenAuth(row["id"], row["principal_id"], row["username"], row["expires_at"],
                     row["name"], row["scope"])


def list_for(principal_id: str) -> list[dict]:
    """Token metadata, newest first. Never the hash, never the plaintext."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, scope, created_at, expires_at, last_used_at, revoked"
            " FROM admin_tokens WHERE principal_id = ? ORDER BY created_at DESC, name",
            (principal_id,)).fetchall()
    now = _now()
    return [{**dict(r), "revoked": bool(r["revoked"]),
             "expired": r["expires_at"] is not None and r["expires_at"] <= now}
            for r in rows]


def revoke(principal_id: str, token_id: str) -> bool:
    with db.connect() as conn:
        return conn.execute(
            "UPDATE admin_tokens SET revoked = 1 WHERE id = ? AND principal_id = ?",
            (token_id, principal_id)).rowcount > 0
