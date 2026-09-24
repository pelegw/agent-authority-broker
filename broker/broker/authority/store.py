"""The grants table. This module is its ONLY writer.

Two insert paths, deliberately asymmetric:
  * `insert_root_grant` takes plain capabilities: root grants are the owner's
    own authority, bounded live by the ceiling. It refuses delegated keys,
    whose authority must derive from their parent's grants.
  * `insert_child_grant` takes nothing but a `NarrowedCapabilities` (only
    `narrow()` can make one) and re-checks `grant_le(child, parent)` against
    the parent row inside the same transaction, rolling back on failure.
    The type is the guarantee; the post-condition catches a parent row that
    changed underneath (or a bug in the algebra).

"Live" means `status='active' AND (expires_at IS NULL OR expires_at > now)`,
evaluated at read time (ported from WA_GW grants.ACTIVE_WHERE), so an
expired grant stops counting the second it expires; `sweep_expired` only
tidies statuses for listings.
"""

import contextlib
import sqlite3
import time
import uuid
from collections.abc import Iterable, Iterator

from .. import db
from ..auth import key_chain
from .capability import Capability, caps_from_json, caps_to_json
from .grant import GRANT_STATUSES, Grant, NarrowedCapabilities, grant_le

ACTIVE_WHERE = "status = 'active' AND (expires_at IS NULL OR expires_at > ?)"
# `agent`: a key revoked a delegation it made (services/delegation.py); the
# revoking key's name is in the audit log. Never an approval: agents can only
# move grants to `revoked`, and only below themselves.
DECIDED_VIA = ("session", "token", "telegram", "system", "agent")
# Allowed status transitions: target status -> statuses it may come from.
# Terminal states (rejected, expired, revoked) never move again.
_TRANSITIONS = {
    "active": ("pending",),              # approve
    "rejected": ("pending",),            # reject
    "revoked": ("pending", "active"),    # revoke
    "expired": ("pending", "active"),    # expire
}
# Chains longer than this are corrupt (depth is capped far lower); stop.
_CHAIN_LIMIT = 64


class GrantInvariantError(RuntimeError):
    """A child grant would not be <= its parent. Nothing was written."""


@contextlib.contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    """One IMMEDIATE transaction: the write lock is taken up front so the
    checks and the insert see the same database state."""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    finally:
        conn.close()


def _read() -> sqlite3.Connection:
    return db.connect()


def _row_to_grant(row) -> Grant:
    return Grant(
        id=row["id"], principal_id=row["principal_id"], key_id=row["key_id"],
        parent_grant_id=row["parent_grant_id"], kind=row["kind"],
        capabilities=tuple(caps_from_json(row["capabilities"])), status=row["status"],
        reason=row["reason"], created_at=row["created_at"], decided_at=row["decided_at"],
        decided_by_principal=row["decided_by_principal"], decided_via=row["decided_via"],
        expires_at=row["expires_at"], requested_by_key_id=row["requested_by_key_id"])


def _check_caps(caps: Iterable[Capability]) -> list[Capability]:
    caps = list(caps)
    if not caps:
        raise ValueError("a grant needs at least one capability")
    if not all(type(c) is Capability for c in caps):
        raise TypeError("grant capabilities must be Capability objects")
    return caps


def _key_row(conn, key_id: int):
    return conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()


def _get(conn, grant_id: str) -> Grant | None:
    row = conn.execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
    return _row_to_grant(row) if row else None


# ---- writes ----------------------------------------------------------------------

def insert_root_grant(principal_id: str, key_id: int, caps: Iterable[Capability],
                      status: str, reason: str, expires_at: int | None,
                      decided_by: str | None = None, *, decided_via: str | None = None,
                      kind: str = "root") -> Grant:
    """Insert a grant with no parent. `kind` is `root` (owner-authored) or
    `expansion` (a root key asking for more; parent = the ceiling, so there
    is no parent row). Status is `active` (owner decided) or `pending`."""
    caps = _check_caps(caps)
    if status not in ("pending", "active"):
        raise ValueError("a new grant is pending or active")
    if kind not in ("root", "expansion"):
        raise ValueError("insert_root_grant creates root or expansion grants only")
    _check_via(decided_via)
    if decided_via == "agent":
        raise ValueError("an agent cannot decide a grant")
    now = int(time.time())
    with _tx() as conn:
        key = _key_row(conn, key_id)
        if key is None or key["principal_id"] != principal_id:
            raise ValueError("unknown key for this principal")
        # A delegated key's authority must flow through its parent's grants;
        # a parentless grant on it would bypass the parent entirely.
        if key["parent_key_id"] is not None:
            raise ValueError("delegated keys only receive child grants (narrow())")
        grant = Grant(
            id=str(uuid.uuid4()), principal_id=principal_id, key_id=key_id,
            parent_grant_id=None, kind=kind, capabilities=tuple(caps), status=status,
            reason=reason or "", created_at=now,
            decided_at=now if status == "active" else None,
            decided_by_principal=decided_by if status == "active" else None,
            decided_via=decided_via if status == "active" else None,
            expires_at=expires_at, requested_by_key_id=key_id if kind == "expansion" else None)
        _insert(conn, grant)
    return grant


def insert_child_grant(principal_id: str, key_id: int, kind: str,
                       narrowed: NarrowedCapabilities, reason: str,
                       expires_at: int | None, requested_by_key_id: int | None) -> Grant:
    """Insert an expansion (pending, awaits a human) or a delegation (active,
    no human interrupt) derived from `narrowed.parent_grant_id`."""
    # `type(...) is`, not isinstance: no look-alike or subclass gets through.
    if type(narrowed) is not NarrowedCapabilities:
        raise TypeError("child grants can only be created from narrow() output")
    if kind not in ("expansion", "delegation"):
        raise ValueError("a child grant is an expansion or a delegation")
    if narrowed.parent_grant_id is None:
        raise ValueError("narrowed against the ceiling: use insert_root_grant")
    caps = _check_caps(narrowed.capabilities)
    now = int(time.time())
    with _tx() as conn:
        parent = _get(conn, narrowed.parent_grant_id)
        if parent is None or parent.principal_id != principal_id:
            raise ValueError("unknown parent grant for this principal")
        if not parent.is_live(now):
            raise ValueError("parent grant is not active")
        key = _key_row(conn, key_id)
        if key is None or key["principal_id"] != principal_id:
            raise ValueError("unknown key for this principal")
        # The parent grant must belong to this key or one of its ancestors.
        if parent.key_id not in {k["id"] for k in key_chain(key_id, conn)}:
            raise ValueError("parent grant is not in this key's lineage")
        child_expiry = expires_at
        if parent.expires_at is not None:
            child_expiry = parent.expires_at if expires_at is None else min(expires_at, parent.expires_at)
        grant = Grant(
            id=str(uuid.uuid4()), principal_id=principal_id, key_id=key_id,
            parent_grant_id=parent.id, kind=kind, capabilities=tuple(caps),
            status="active" if kind == "delegation" else "pending",
            reason=reason or "", created_at=now,
            decided_at=now if kind == "delegation" else None,
            expires_at=child_expiry, requested_by_key_id=requested_by_key_id)
        _insert(conn, grant)
        # Post-condition, re-read from the rows as written.
        child_row, parent_row = _get(conn, grant.id), _get(conn, parent.id)
        if child_row is None or parent_row is None or not grant_le(
                child_row, parent_row, narrowed.lattice):
            raise GrantInvariantError("child grant would exceed its parent; rolled back")
    return grant


def _insert(conn, g: Grant) -> None:
    conn.execute(
        "INSERT INTO grants (id, principal_id, key_id, parent_grant_id, kind, capabilities,"
        " status, reason, created_at, decided_at, decided_by_principal, decided_via,"
        " expires_at, requested_by_key_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (g.id, g.principal_id, g.key_id, g.parent_grant_id, g.kind,
         caps_to_json(g.capabilities), g.status, g.reason, g.created_at, g.decided_at,
         g.decided_by_principal, g.decided_via, g.expires_at, g.requested_by_key_id))


def set_root_capabilities(grant_id: str, caps: Iterable[Capability]) -> bool:
    """Replace a root grant's capabilities (an owner edit). Allowed only on
    parentless grants: descendants are re-bounded live by chain_meet, so an
    edit narrower shrinks them instantly and an edit wider cannot lift them
    above their own rows."""
    caps = _check_caps(caps)
    with _tx() as conn:
        cur = conn.execute(
            "UPDATE grants SET capabilities = ? WHERE id = ? AND parent_grant_id IS NULL",
            (caps_to_json(caps), grant_id))
        return cur.rowcount > 0


def set_status(grant_id: str, status: str, decided_by_principal: str | None = None,
               decided_via: str | None = None) -> bool:
    """Move a grant to `status` if the transition is allowed (atomic
    UPDATE ... WHERE status IN (...)). Returns False if the grant is missing
    or not in a state that can move there."""
    if status not in _TRANSITIONS:
        raise ValueError(f"cannot set status {status!r}")
    _check_via(decided_via)
    if decided_via == "agent" and status != "revoked":
        raise ValueError("an agent can only revoke a grant, never decide one")
    sources = _TRANSITIONS[status]
    with _tx() as conn:
        cur = conn.execute(
            f"UPDATE grants SET status = ?, decided_at = ?, decided_by_principal = ?,"
            f" decided_via = ? WHERE id = ? AND status IN ({','.join('?' * len(sources))})",
            (status, int(time.time()), decided_by_principal, decided_via, grant_id, *sources))
        return cur.rowcount > 0


def sweep_expired(now: int | None = None) -> int:
    """Flip active/pending grants past their expiry to 'expired' (cosmetic for
    listings: evaluation already re-checks expiry). Returns rows changed."""
    now = int(time.time()) if now is None else now
    with _tx() as conn:
        cur = conn.execute(
            "UPDATE grants SET status = 'expired', decided_via = 'system', decided_at = ?"
            " WHERE status IN ('active', 'pending') AND expires_at IS NOT NULL"
            " AND expires_at <= ?", (now, now))
        return cur.rowcount


def _check_via(via: str | None) -> None:
    if via is not None and via not in DECIDED_VIA:
        raise ValueError(f"decided_via must be one of {DECIDED_VIA}")


# ---- reads -------------------------------------------------------------------------

def get(grant_id: str) -> Grant | None:
    conn = _read()
    try:
        return _get(conn, grant_id)
    finally:
        conn.close()


def list_for_key(key_id: int, limit: int = 100) -> list[Grant]:
    conn = _read()
    try:
        rows = conn.execute(
            "SELECT * FROM grants WHERE key_id = ? ORDER BY created_at DESC, id LIMIT ?",
            (key_id, limit)).fetchall()
    finally:
        conn.close()
    return [_row_to_grant(r) for r in rows]


def list_active_for_key(key_id: int, now: int) -> list[Grant]:
    """The key's live grants (the leaves evaluation starts from)."""
    conn = _read()
    try:
        rows = conn.execute(
            "SELECT * FROM grants WHERE key_id = ? AND " + ACTIVE_WHERE +
            " ORDER BY created_at, id", (key_id, now)).fetchall()
    finally:
        conn.close()
    return [_row_to_grant(r) for r in rows]


def chain(grant_id: str) -> list[Grant]:
    """The grant and its ancestors, root first. A missing ancestor or a loop
    yields a chain whose first link still has a parent, which chain_meet
    treats as broken (bottom)."""
    conn = _read()
    try:
        out: list[Grant] = []
        seen: set[str] = set()
        current: str | None = grant_id
        while current is not None and current not in seen and len(out) < _CHAIN_LIMIT:
            seen.add(current)
            g = _get(conn, current)
            if g is None:
                break
            out.append(g)
            current = g.parent_grant_id
    finally:
        conn.close()
    out.reverse()
    return out


__all__ = ["ACTIVE_WHERE", "GRANT_STATUSES", "GrantInvariantError", "chain", "get",
           "insert_child_grant", "insert_root_grant", "list_active_for_key",
           "list_for_key", "set_root_capabilities", "set_status", "sweep_expired"]
