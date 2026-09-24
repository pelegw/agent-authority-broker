"""Agent keys (`aab_...`): creation, rotation, disabling, and bearer auth.

Only the sha256 of a key lands in the database; the plaintext exists once, in
the value `create_key`/`rotate_key` return. A key holds no authority by
itself (that comes from its grants, see authority/); this module answers
"which live key is this, and who is it acting for".

Delegated keys hang off a parent via parent_key_id. Authentication walks the
whole chain on every request: if ANY ancestor is disabled, expired, missing,
belongs to another principal, or the chain is deeper than
max_delegation_depth, the key does not authenticate. That is what makes
revoking a parent kill every descendant instantly with no cascade writes.

Ported from WA_GW gateway/app/auth.py (key format, sha256 lookup, rotation
grace window, throttled last-used), extended with principals and chains.
"""

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field

from . import db
from .authority.denies import Denies, merged_denies, parse_denies
from .authority.roles import ROLE_RANK, ROLES, check_role
from .config import get_settings

KEY_PREFIX = "aab_"
ADMIN_TOKEN_PREFIX = "aab_admin_"   # owner tokens; never valid as agent keys
CREATED_BY = ("owner", "delegation")
# Hard stop for chain walks, independent of max_delegation_depth, so a
# corrupted parent_key_id loop can never spin.
_WALK_LIMIT = 64
# Refresh last_used_at at most this often, so per-request auth stays a read
# in the common case rather than a write on every call.
_LAST_USED_THROTTLE = 60


@dataclass(frozen=True)
class AuthContext:
    """Who is calling. Travels through every policy/engine call."""
    key_id: int
    principal_id: str
    name: str
    role: str
    rate_per_min: int
    # Earliest expiry along the key chain: the key is dead then either way.
    expires_at: int | None
    # expires_at, or the rotation grace end when the previous secret was used.
    credential_expires_at: int | None
    parent_key_id: int | None
    depth: int                                # 0 for a root key
    denies: Denies = field(default_factory=dict)   # merged along the chain
    # Root -> self. effective() uses these to bound the key by every
    # ancestor's role and to check grant chains belong to this key lineage.
    chain_key_ids: tuple[int, ...] = ()
    chain_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewKey:
    """A freshly created key. The plaintext is excluded from repr so a stray
    log line or traceback can't leak it."""
    key_id: int
    plaintext: str = field(repr=False)


def generate_key() -> tuple[str, str]:
    """Return (plaintext_key, sha256_hash). Hex can't contain "admin_", so an
    agent key can never look like an admin token."""
    plaintext = KEY_PREFIX + secrets.token_hex(24)
    return plaintext, hash_key(plaintext)


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


# ---- chain walking ------------------------------------------------------------

def key_chain(key_id: int, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Rows from the root key down to `key_id`. Returns [] if the key or any
    ancestor is missing, or the parent links loop (fail closed)."""
    own = conn is None
    conn = conn or db.connect()
    try:
        chain: list[dict] = []
        seen: set[int] = set()
        current: int | None = key_id
        while current is not None:
            if current in seen or len(chain) >= _WALK_LIMIT:
                return []
            seen.add(current)
            row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (current,)).fetchone()
            if row is None:
                return []
            chain.append(dict(row))
            current = row["parent_key_id"]
        chain.reverse()
        return chain
    finally:
        if own:
            conn.close()


def _chain_problem(chain: list[dict], now: int) -> str | None:
    """Why a chain cannot act right now, or None if every link is live."""
    if not chain:
        return "broken chain"
    if len(chain) - 1 > get_settings().max_delegation_depth:
        return "delegation too deep"
    principal = chain[0]["principal_id"]
    for link in chain:
        if link["disabled"]:
            return "disabled"
        if link["expires_at"] is not None and link["expires_at"] <= now:
            return "expired"
        if link["principal_id"] != principal:
            return "principal mismatch"
    return None


def _principal_ok(conn, principal_id: str) -> bool:
    row = conn.execute("SELECT disabled FROM principals WHERE id = ?", (principal_id,)).fetchone()
    return row is not None and not row["disabled"]


# ---- bearer auth ----------------------------------------------------------------

def authenticate_bearer(authorization: str | None, client_ip: str = "") -> AuthContext | None:
    """Resolve an Authorization header to an AuthContext, or None (= 401).

    Honors key expiry, the rotation grace window (a rotated key's previous
    secret works until prev_expires_at), the whole parent chain, and the
    principal. Records throttled last-used metadata.
    """
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    if not token.startswith(KEY_PREFIX) or token.startswith(ADMIN_TOKEN_PREFIX):
        return None
    if not token.isascii():
        return None
    now = int(time.time())
    token_hash = hash_key(token)
    conn = db.connect()
    try:
        with conn:
            # Match the current secret, or the previous one inside its grace
            # window. disabled kills both immediately.
            row = conn.execute(
                "SELECT * FROM api_keys WHERE disabled = 0 AND ("
                "  key_hash = ?"
                "  OR (prev_key_hash = ? AND prev_expires_at IS NOT NULL AND prev_expires_at > ?)"
                ")",
                (token_hash, token_hash, now),
            ).fetchone()
            if row is None:
                return None
            chain = key_chain(row["id"], conn)
            if _chain_problem(chain, now) is not None or chain[-1]["id"] != row["id"]:
                return None
            if not _principal_ok(conn, row["principal_id"]):
                return None
            try:
                denies = merged_denies(chain)
            except ValueError:
                return None      # unreadable denies must not parse as "no denies"
            _touch_last_used(conn, row, now, client_ip)
    finally:
        conn.close()
    expiries = [link["expires_at"] for link in chain if link["expires_at"] is not None]
    expires_at = min(expiries, default=None)
    cred = expires_at
    if token_hash != row["key_hash"]:    # authenticated with the previous secret
        cred = row["prev_expires_at"] if cred is None else min(cred, row["prev_expires_at"])
    return AuthContext(
        key_id=row["id"], principal_id=row["principal_id"], name=row["name"],
        role=row["role"], rate_per_min=row["rate_per_min"], expires_at=expires_at,
        credential_expires_at=cred, parent_key_id=row["parent_key_id"],
        depth=len(chain) - 1, denies=denies,
        chain_key_ids=tuple(link["id"] for link in chain),
        chain_roles=tuple(link["role"] for link in chain),
    )


def context_for_key(key_id: int) -> AuthContext | None:
    """The AuthContext a key would have right now, without its secret.

    Used when the broker acts later on a key's behalf (a scheduled or
    approved action): the same chain checks as bearer auth apply, so a key
    disabled or expired since queuing (or with a dead ancestor) yields None.
    """
    now = int(time.time())
    conn = db.connect()
    try:
        chain = key_chain(key_id, conn)
        if _chain_problem(chain, now) is not None or chain[-1]["id"] != key_id:
            return None
        row = chain[-1]
        if not _principal_ok(conn, row["principal_id"]):
            return None
        try:
            denies = merged_denies(chain)
        except ValueError:
            return None
    finally:
        conn.close()
    expires_at = min((link["expires_at"] for link in chain if link["expires_at"] is not None),
                     default=None)
    return AuthContext(
        key_id=row["id"], principal_id=row["principal_id"], name=row["name"],
        role=row["role"], rate_per_min=row["rate_per_min"], expires_at=expires_at,
        credential_expires_at=expires_at, parent_key_id=row["parent_key_id"],
        depth=len(chain) - 1, denies=denies,
        chain_key_ids=tuple(link["id"] for link in chain),
        chain_roles=tuple(link["role"] for link in chain),
    )


def _touch_last_used(conn, row, now: int, client_ip: str) -> None:
    """Throttled update of last_used_at/last_used_ip (skips the write if the
    row was touched within the throttle window and the IP is unchanged)."""
    last = row["last_used_at"] or 0
    if now - last < _LAST_USED_THROTTLE and (row["last_used_ip"] or "") == client_ip:
        return
    conn.execute("UPDATE api_keys SET last_used_at = ?, last_used_ip = ? WHERE id = ?",
                 (now, client_ip or None, row["id"]))


# ---- lifecycle ----------------------------------------------------------------------

def create_key(principal_id: str, name: str, role: str, rate_per_min: int,
               expires_at: int | None, parent_key_id: int | None = None,
               created_by: str = "owner", denies: dict | None = None) -> NewKey:
    """Insert a key; returns its id and the plaintext (the only time it exists).

    A child key (parent_key_id set) must be no stronger than its parent on
    every axis the key row carries: role, rate, expiry, and depth. Its denies
    are stored as given; the parent's are merged in at authentication time.
    """
    check_role(role)
    if created_by not in CREATED_BY:
        raise ValueError(f"created_by must be one of {CREATED_BY}")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("key name must be non-empty")
    if not isinstance(rate_per_min, int) or isinstance(rate_per_min, bool) or rate_per_min < 1:
        raise ValueError("rate_per_min must be a positive integer")
    now = int(time.time())
    if expires_at is not None and expires_at <= now:
        raise ValueError("expires_at is in the past")
    own_denies = parse_denies(denies or {})
    plaintext, key_hash = generate_key()
    conn = db.connect()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if not _principal_ok(conn, principal_id):
                raise ValueError("unknown or disabled principal")
            if parent_key_id is not None:
                _check_child_of(conn, parent_key_id, principal_id, role,
                                rate_per_min, expires_at, now)
            cur = conn.execute(
                "INSERT INTO api_keys (principal_id, name, key_hash, role, rate_per_min,"
                " expires_at, created_at, parent_key_id, created_by, denies)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (principal_id, name, key_hash, role, rate_per_min, expires_at, now,
                 parent_key_id, created_by, json.dumps(own_denies, sort_keys=True)),
            )
            key_id = cur.lastrowid
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"key name {name!r} is taken") from exc
    finally:
        conn.close()
    return NewKey(key_id, plaintext)


def _check_child_of(conn, parent_key_id: int, principal_id: str, role: str,
                    rate_per_min: int, expires_at: int | None, now: int) -> None:
    chain = key_chain(parent_key_id, conn)
    problem = _chain_problem(chain, now)
    if problem:
        raise ValueError(f"parent key cannot delegate: {problem}")
    parent = chain[-1]
    if parent["principal_id"] != principal_id:
        raise ValueError("parent key belongs to another principal")
    if len(chain) > get_settings().max_delegation_depth:   # child depth = len(chain)
        raise ValueError("delegation depth limit reached")
    if ROLE_RANK[role] > ROLE_RANK.get(parent["role"], -1):
        raise ValueError(f"role {role!r} exceeds parent role {parent['role']!r}")
    if rate_per_min > parent["rate_per_min"]:
        raise ValueError("rate_per_min exceeds the parent's")
    parent_exp = min((link["expires_at"] for link in chain if link["expires_at"] is not None),
                     default=None)
    if parent_exp is not None and (expires_at is None or expires_at > parent_exp):
        raise ValueError("a child key cannot outlive its parent")


def rotate_key(key_id: int, grace_seconds: int | None = None) -> str:
    """Issue a fresh secret, keeping the old one valid for grace_seconds
    (default: key_rotation_grace_seconds) so agents can swap without downtime.
    Returns the new plaintext. Role, grants, expiry and identity are kept."""
    if grace_seconds is None:
        grace_seconds = get_settings().key_rotation_grace_seconds
    now = int(time.time())
    new_plaintext, new_hash = generate_key()
    conn = db.connect()
    try:
        with conn:
            # One statement: prev_key_hash captures the CURRENT key_hash before
            # it is overwritten, so concurrent rotations can't lose an update.
            cur = conn.execute(
                "UPDATE api_keys SET prev_key_hash = key_hash, prev_expires_at = ?, "
                "key_hash = ? WHERE id = ?",
                (now + grace_seconds, new_hash, key_id),
            )
            if cur.rowcount == 0:
                raise KeyError("no such key")
    finally:
        conn.close()
    return new_plaintext


def disable_key(key_id: int) -> bool:
    """Disable a key (both secrets, immediately). Descendants stop
    authenticating too, because every auth walks the chain. Returns False if
    the key does not exist."""
    conn = db.connect()
    try:
        with conn:
            cur = conn.execute("UPDATE api_keys SET disabled = 1 WHERE id = ?", (key_id,))
            return cur.rowcount > 0
    finally:
        conn.close()


__all__ = ["ADMIN_TOKEN_PREFIX", "AuthContext", "KEY_PREFIX", "NewKey", "ROLES",
           "authenticate_bearer", "context_for_key", "create_key", "disable_key", "generate_key",
           "hash_key", "key_chain", "rotate_key"]
