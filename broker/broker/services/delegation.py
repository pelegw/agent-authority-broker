"""Delegation: an agent mints a narrower child key for a sub-agent.

`delegate` carves a child key out of the caller's own authority. No human
approves a delegation, so nothing here may widen anything, and the
guarantees are structural rather than hand-checked:

  * the child key row (role, rate, expiry, depth) is created by
    `auth.create_key(parent_key_id=caller)`, which refuses anything stronger
    than the parent. Its denies are merged with every ancestor's at each
    authentication, so they can only grow;
  * the child's grants are `kind=delegation` children of the caller's own
    live grants, produced by `narrow()` and inserted by
    `store.insert_child_grant`, which re-checks `grant_le` against the stored
    parent row inside its transaction;
  * the child's effective set is re-evaluated live along both chains on
    every call, so revoking, narrowing or expiring anything above it shrinks
    it at once, with no cascade writes.

The request is narrowed against what the caller can use RIGHT NOW (each live
grant's chain meet, bounded by the ceiling, every ancestor's role and the
caller's denies), further bounded by the child's role. That is only ever
stricter than narrowing against the stored rows, and it keeps the answer
honest: a delegation is created exactly as asked, or refused with a 400 that
lists what was clipped and what the caller could give (hidden and denied ids
removed from that list).

Child names are namespaced under the caller (`<caller>/<name>`), so a
sub-agent's key can never pose as another key in approval cards, decision
records or the audit log, and an agent cannot probe the global key names.

`revoke_delegation` reaches descendants only: it disables that key and
revokes its grants; keys below it stop at once through the chain walk in
`auth.authenticate_bearer` and `effective.chain_meet`.

Like services/agent.py, this module must not import services/admin.py or
identity/ (a test walks the import graph).
"""

from __future__ import annotations

import dataclasses
import logging
import re
import time

from .. import db
from ..audit import audit
from ..auth import create_key, disable_key, key_chain, max_delegation_depth
from ..authority import store
from ..authority.capability import Capability, dedupe
from ..authority.effective import effective_with_chains
from ..authority.grant import Grant, Lattice, narrow
from ..authority.roles import ROLE_RANK, ROLES, role_caps
from ..authority.store import GrantInvariantError
from ..errors import PolicyError
from ..ledger import rate_limiter
from ..logging_setup import kv
from ..plugins.registry import get_registry
from ..runtime_settings import runtime_settings
from .agent import clipped_error, live_children, normalize_request, visible_caps
from .deny_input import normalize_denies

# The agent-chosen part of a child key's name. No "/" so it cannot fake a
# deeper lineage; the stored name is "<caller name>/<this>".
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
# Longest lifetime a delegation may ask for (also keeps the arithmetic far
# from SQLite's integer range). Always further capped by the caller's own.
MAX_HOURS = 24 * 365 * 10
_GRANT_LIST_LIMIT = 1000

log = logging.getLogger(__name__)


def _exceeds(field: str, message: str, limit) -> PolicyError:
    return PolicyError(400, message, "exceeds_parent", extra={"field": field, "max": limit})


def _child_name(parent_name: str, name) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PolicyError(400, "name must be 1-40 letters, digits, '_', '-' or '.'",
                          "invalid_name")
    return f"{parent_name}/{name}"


def _child_role(auth, role) -> str:
    role = auth.role if role is None else role
    if role not in ROLE_RANK:
        raise PolicyError(400, f"unknown role {role!r} (want one of {list(ROLES)})",
                          "invalid_role")
    if ROLE_RANK[role] > ROLE_RANK.get(auth.role, -1):
        raise _exceeds("role", f"role {role!r} exceeds yours ({auth.role!r})", auth.role)
    return role


def _child_rate(auth, rate) -> int:
    rate = auth.rate_per_min if rate is None else rate
    if not isinstance(rate, int) or isinstance(rate, bool) or rate < 1:
        raise PolicyError(400, "rate_per_min must be a positive integer", "bad_request")
    if rate > auth.rate_per_min:
        raise _exceeds("rate_per_min", f"rate_per_min {rate} exceeds yours "
                                       f"({auth.rate_per_min})", auth.rate_per_min)
    return rate


def _child_expiry(auth, hours, now: int) -> int | None:
    """The child's expiry: the caller's own (the earliest along its chain)
    when not asked, else now + hours, which must not outlive the caller."""
    if hours is None:
        return auth.expires_at
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= MAX_HOURS:
        raise PolicyError(400, f"expires_in_hours must be an integer from 1 to {MAX_HOURS}",
                          "bad_request")
    expires_at = now + hours * 3600
    if auth.expires_at is not None and expires_at > auth.expires_at:
        raise _exceeds("expires_in_hours", "a delegated key cannot outlive yours",
                       auth.expires_at)
    return expires_at


def _live_views(auth, role: str, now: int, lattice: Lattice) -> list[Grant]:
    """Each live grant of the caller, with its capabilities replaced by what
    the caller can use through it right now, met with the child's role.

    A view still carries the stored grant's id, so `narrow()` against it
    yields a child of the real row, and the store's post-condition compares
    the child with the STORED parent capabilities, which a view is <= by
    construction (chain meet, ceiling, roles and denies only narrow)."""
    reg = get_registry()
    by_leaf: dict[str, list[Capability]] = {}
    for cap, chain in effective_with_chains(auth, now, reg.plugin_states(), lattice):
        by_leaf.setdefault(chain[-1], []).append(cap)
    role_bound = [r for m in reg.manifests().values() for r in role_caps(m, role)]
    views = []
    for g in store.list_active_for_key(auth.key_id, now):
        caps = dedupe(lattice.meet(c, r) for c in by_leaf.get(g.id, ()) for r in role_bound)
        if caps:
            views.append(dataclasses.replace(g, capabilities=tuple(caps)))
    return views


# ---- delegate --------------------------------------------------------------------------

def delegate(auth, name: str, capabilities: list, reason: str = "",
             expires_in_hours: int | None = None, role: str | None = None,
             rate_per_min: int | None = None, denies: dict | None = None) -> dict:
    """Mint a child key holding exactly `capabilities` (which must fit inside
    the caller's current authority). Returns the plaintext key once."""
    # Every attempt, granted or not, spends one call of the caller's
    # per-minute rate (shared with its actions): minting is a write.
    if not rate_limiter.check(auth.key_id, auth.rate_per_min):
        raise PolicyError(429, f"rate limit: {auth.rate_per_min} calls/minute for this key",
                          "rate_limited")
    limit = max_delegation_depth()
    if auth.depth >= limit:
        raise PolicyError(400, f"delegation depth limit ({limit}) reached: this key "
                               "cannot delegate", "depth_exceeded")
    full_name = _child_name(auth.name, name)
    role = _child_role(auth, role)
    rate = _child_rate(auth, rate_per_min)
    now = int(time.time())
    expires_at = _child_expiry(auth, expires_in_hours, now)
    own_denies = normalize_denies(denies, strict=True)
    requested = normalize_request(capabilities)
    reason = (reason or "")[:1000]
    # Live direct children one key may hold at once (an operator setting).
    # Delegation needs no human, so without a cap one agent could mint key
    # rows without end; with it (and the depth limit) a tree stays bounded
    # and reviewable in the console.
    cap = runtime_settings().max_live_delegations
    if live_children(auth) >= cap:
        raise PolicyError(409, f"this key already has {cap} live delegations; "
                               "revoke one first", "too_many_delegations")

    lattice = get_registry().lattice()
    narrowed = [n for n in (narrow(v, requested, lattice)
                            for v in _live_views(auth, role, now, lattice)) if n.capabilities]
    pooled = dedupe(c for n in narrowed for c in n.capabilities)
    clip = [r for r in requested if not any(lattice.le(r, c) for c in pooled)]
    if clip:
        log.info("delegation refused: exceeds the caller's authority %s",
                 kv(key=auth.name, requested=len(requested), clipped=len(clip)))
        raise clipped_error("exceeds what you can delegate", "delegate only the allowed "
                            "capabilities (you can only narrow your own authority)",
                            clip, visible_caps(auth, pooled))

    try:
        new = create_key(auth.principal_id, full_name, role, rate, expires_at,
                         parent_key_id=auth.key_id, created_by="delegation", denies=own_denies)
    except ValueError as exc:
        if "taken" in str(exc):
            raise PolicyError(409, f"a key named {full_name!r} already exists",
                              "name_taken") from exc
        raise PolicyError(400, str(exc), "invalid_key") from exc
    grants: list[Grant] = []
    try:
        for n in narrowed:
            grants.append(store.insert_child_grant(auth.principal_id, new.key_id, "delegation",
                                                   n, reason, expires_at, auth.key_id))
    except (ValueError, TypeError, GrantInvariantError) as exc:
        _undo(new.key_id, grants)
        audit(auth.name, "delegation.create", str(new.key_id), {"name": full_name},
              result="error")
        log.warning("delegation undone: the caller's authority changed mid-way %s",
                    kv(key=auth.name, child_key_id=new.key_id))
        raise PolicyError(409, "your authority changed while delegating; nothing was "
                               "created, try again", "conflict") from exc
    audit(auth.name, "delegation.create", str(new.key_id),
          {"name": full_name, "role": role, "rate_per_min": rate, "expires_at": expires_at,
           "grants": [g.id for g in grants], "parent_grants": [g.parent_grant_id for g in grants]})
    log.info("delegation created %s", kv(
        key=auth.name, child_key_id=new.key_id, child=full_name, role=role, rate_per_min=rate,
        expires_at=expires_at, depth=auth.depth + 1, grants=[g.id for g in grants]))
    return {"key_id": new.key_id, "name": full_name, "key": new.plaintext,
            "expires_at": expires_at, "role": role, "rate_per_min": rate,
            "capabilities": visible_caps(auth, pooled, own_denies),
            "note": "hand this key to the sub-agent; it is never shown again"}


def _undo(key_id: int, grants: list[Grant]) -> None:
    """Compensate a half-made delegation (the caller's authority changed
    between the check and the insert). Fail closed: with no grant written the
    key row is deleted (nobody has seen its secret); otherwise it is disabled
    and the grants already written are revoked."""
    for g in grants:
        store.set_status(g.id, "revoked", None, "agent")
    if grants:
        disable_key(key_id)
        return
    conn = db.connect()
    try:
        with conn:
            conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    finally:
        conn.close()


# ---- list / revoke -------------------------------------------------------------------

def _status(row, now: int) -> str:
    if row["disabled"]:
        return "disabled"
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return "expired"
    return "active"


def list_my_delegations(auth) -> dict:
    """The keys this key delegated directly, with status and a grant summary
    (capabilities shown through the caller's own visibility filter)."""
    store.sweep_expired()
    now = int(time.time())
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM api_keys WHERE parent_key_id = ? AND principal_id = ?"
            " ORDER BY id LIMIT 200", (auth.key_id, auth.principal_id)).fetchall()
        counts = {r["parent_key_id"]: r["n"] for r in conn.execute(
            "SELECT parent_key_id, COUNT(*) AS n FROM api_keys WHERE parent_key_id IN"
            " (SELECT id FROM api_keys WHERE parent_key_id = ?) GROUP BY parent_key_id",
            (auth.key_id,))}
    finally:
        conn.close()
    items = []
    for r in rows:
        grants = [{"id": g.id, "status": g.status, "expires_at": g.expires_at,
                   "capabilities": visible_caps(auth, g.capabilities)}
                  for g in store.list_for_key(r["id"], limit=_GRANT_LIST_LIMIT)]
        items.append({"key_id": r["id"], "name": r["name"], "role": r["role"],
                      "rate_per_min": r["rate_per_min"], "status": _status(r, now),
                      "created_at": r["created_at"], "expires_at": r["expires_at"],
                      "last_used_at": r["last_used_at"],
                      "delegations": counts.get(r["id"], 0), "grants": grants})
    return {"items": items}


def revoke_delegation(auth, key_id: int) -> dict:
    """Revoke a key this key delegated, directly or further down. Anything
    else (itself, an ancestor, a sibling, another lineage) is a plain 404."""
    if not isinstance(key_id, int) or isinstance(key_id, bool):
        raise PolicyError(404, "no such delegation", "not_found")
    chain = key_chain(key_id)
    lineage = [k["id"] for k in chain]
    if (not chain or lineage[-1] != key_id or auth.key_id not in lineage[:-1]
            or chain[-1]["principal_id"] != auth.principal_id):
        raise PolicyError(404, "no such delegation", "not_found")
    disable_key(key_id)
    revoked = [g.id for g in store.list_for_key(key_id, limit=_GRANT_LIST_LIMIT)
               if g.status in ("pending", "active")
               and store.set_status(g.id, "revoked", None, "agent")]
    audit(auth.name, "delegation.revoke", str(key_id),
          {"name": chain[-1]["name"], "grants": revoked})
    log.info("delegation revoked %s", kv(key=auth.name, child_key_id=key_id,
                                         child=chain[-1]["name"], grants_revoked=len(revoked)))
    return {"key_id": key_id, "name": chain[-1]["name"], "status": "revoked"}
