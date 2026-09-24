"""Agent-facing operations besides performing actions (that is engine.py).

Everything here acts as the calling key (an `AuthContext`) and discloses
only what the key could discover by trying: its own capabilities, grants
and queued actions. It never lists hidden resources or deny sets, because
listing them would reveal exactly what exists but is hidden.

This module must not import services/admin.py or identity/ (a test walks
the import graph): an agent has no path to approving anything.
"""

from __future__ import annotations

import time

from .. import db, notify
from ..actions import queue
from ..audit import audit
from ..authority import store
from ..authority.capability import Capability, from_json, normalize_all, to_json
from ..authority.ceiling import ceiling
from ..authority.effective import effective_with_chains
from ..authority.grant import clipped, narrow
from ..authority.store import GrantInvariantError
from ..config import get_settings
from ..engine import adapter_error
from ..errors import PolicyError
from ..hidden import deny_sets, is_denied
from ..ledger import remaining
from ..plugins.adapter import AdapterError
from ..plugins.manifest import ManifestError
from ..plugins.registry import get_registry
from ..policy import enforced_where, run_mode


def _caps_by_target(auth) -> dict[str, list[tuple[Capability, tuple[str, ...]]]]:
    reg = get_registry()
    out: dict[str, list] = {}
    for cap, chain in effective_with_chains(auth, int(time.time()), reg.plugin_states(),
                                            reg.lattice()):
        out.setdefault(cap.target, []).append((cap, chain))
    return out


# ---- discovery ----------------------------------------------------------------------

def reachable_actions(auth) -> dict[str, set[str]]:
    """{enabled plugin: actions some effective capability of this key can
    reach}. The one definition of "what this key may try", shared by REST
    `/v1/targets`, `/v1/me/openapi.json` and the MCP tool list, so the
    surfaces cannot drift. A capability reaches an action only when the
    action exists and the capability's mode can run it (policy.run_mode);
    resource selectors are not considered here, they are checked per call."""
    reg = get_registry()
    caps = _caps_by_target(auth)
    out: dict[str, set[str]] = {}
    for pid, m in reg.enabled_manifests().items():
        acts = set()
        for cap, _ in caps.get(pid, []):
            for name in cap.actions:
                act = m.action(name)
                if act is not None and run_mode(cap.mode, act) is not None:
                    acts.add(name)
        out[pid] = acts
    return out


def list_targets(auth) -> dict:
    reg = get_registry()
    reach = reachable_actions(auth)
    items = []
    for pid, m in sorted(reg.enabled_manifests().items()):
        items.append({"id": pid, "display_name": m.display_name,
                      "description": m.description, "actions": sorted(reach.get(pid, ()))})
    return {"items": items}


def _visible_cap(cap: Capability, deny: dict[str, set[str]], dims: dict[str, str]) -> dict | None:
    """Capability JSON with denied/hidden ids removed from its selectors;
    None when that empties a selector (nothing left to show)."""
    out = to_json(cap)
    for dim, ids in list(out["selector"].items()):
        kind = dims.get(dim, dim)
        kept = [i for i in ids if i not in deny.get(kind, set())]
        if not kept:
            return None
        out["selector"][dim] = kept
    return out


def get_my_access(auth) -> dict:
    """Self-introspection: what this key can do, where it is enforced, and
    how much budget is left. Not audited (agents poll it)."""
    reg = get_registry()
    caps = _caps_by_target(auth)
    targets = {}
    for pid, m in sorted(reg.enabled_manifests().items()):
        deny = deny_sets(auth, pid)
        dims = {n.dimension: (n.resource or n.dimension) for n in m.narrowings}
        health = reg.last_health(pid)
        entries, where = [], {}
        for cap, chain in caps.get(pid, []):
            shown = _visible_cap(cap, deny, dims)
            if shown is None:
                continue
            for action in cap.actions:
                where.update(enforced_where(m, action, health))
            budget = remaining(chain, pid, sorted(cap.actions)[0])
            if budget:
                shown["remaining"] = budget
            shown["grant_chain"] = list(chain)
            entries.append(shown)
        targets[pid] = {"capabilities": entries, "enforced_where": dict(sorted(where.items()))}
    return {
        "name": auth.name, "role": auth.role, "rate_per_min": auth.rate_per_min,
        "key_expires_at": auth.expires_at, "credential_expires_at": auth.credential_expires_at,
        "depth": auth.depth, "delegated": auth.parent_key_id is not None, "targets": targets,
    }


def resolve_resource(auth, target: str, kind: str, query: str, limit: int = 20) -> dict:
    """Name -> id lookup inside what the key may see."""
    reg = get_registry()
    manifest = reg.enabled_manifests().get(target)
    if manifest is None:
        raise PolicyError(404, "no such target", "not_found")
    res = manifest.resources.get(kind)
    if res is None or not res.resolve:
        raise PolicyError(400, f"{target} cannot resolve {kind!r}", "bad_request")
    caps = [c for c, _ in _caps_by_target(auth).get(target, [])]
    if not caps:
        raise PolicyError(403, "no grant on this target", "out_of_grant",
                          hint="request_permission")
    # Allowed ids for this kind: unrestricted if any capability leaves the
    # kind open, else the union of the explicit selectors.
    dims = {n.dimension: (n.resource or n.dimension) for n in manifest.narrowings}
    allowed: set[str] | None = set()
    for cap in caps:
        restricting = [d for d, k in dims.items() if k == kind and d in cap.selector]
        if not restricting:
            allowed = None
            break
        for d in restricting:
            allowed |= set(cap.selector[d])
    try:
        found = reg.adapter(target).resolve(kind, query, max(1, min(limit, 50)))
    except AdapterError as exc:
        raise adapter_error(exc) from exc
    deny = deny_sets(auth, target).get(kind, set())
    anc = reg.ancestors
    items = []
    for item in found:
        rid = item.get("id")
        if not isinstance(rid, str) or is_denied(deny, rid, kind, anc):
            continue
        if allowed is not None and rid not in allowed and not any(
                a in allowed for a in anc(kind, rid)):
            continue
        items.append({"id": rid, "label": item.get("label", "")})
    return {"items": items}


# ---- permission requests -------------------------------------------------------------

def _grant_view(g) -> dict:
    return {"id": g.id, "kind": g.kind, "status": g.status,
            "capabilities": [to_json(c) for c in g.capabilities],
            "created_at": g.created_at, "decided_at": g.decided_at,
            "expires_at": g.expires_at}


def _clipped_error(requested, narrowed_caps) -> PolicyError:
    return PolicyError(
        400, "exceeds what your parent can give", "clipped",
        hint="request only the allowed capabilities, or ask the owner directly",
        extra={"clipped": [to_json(c) for c in requested],
               "allowed": [to_json(c) for c in narrowed_caps]})


def request_permission(auth, capabilities: list, reason: str = "",
                       expires_in_hours: int | None = None) -> dict:
    """Ask the owner for more authority. The request is first narrowed
    against what the key's parent could give (the ceiling for a root key,
    the parent key's grants for a delegated one); anything clipped is a 400
    listing it, so a pending request is always grantable as asked."""
    reg = get_registry()
    if not isinstance(capabilities, list) or not capabilities:
        raise PolicyError(400, "capabilities must be a non-empty list", "invalid_capabilities")
    enabled = reg.enabled_manifests()
    try:
        requested = normalize_all([from_json(c) for c in capabilities], enabled)
    except (ValueError, ManifestError) as exc:
        raise PolicyError(400, f"invalid capability: {exc}", "invalid_capabilities") from exc
    if not requested:
        raise PolicyError(400, "the request is empty after normalization",
                          "invalid_capabilities")
    now = int(time.time())
    expires_at = None
    if expires_in_hours is not None:
        if isinstance(expires_in_hours, bool) or expires_in_hours <= 0:
            raise PolicyError(400, "expires_in_hours must be positive", "bad_request")
        expires_at = now + min(expires_in_hours, get_settings().grant_max_hours) * 3600
    lattice = reg.lattice()
    reason = (reason or "")[:1000]
    try:
        if auth.parent_key_id is None:
            narrowed = narrow(ceiling(auth.principal_id, reg.plugin_states()), requested, lattice)
            if clipped(requested, narrowed):
                raise _clipped_error(clipped(requested, narrowed), narrowed.capabilities)
            grant = store.insert_root_grant(auth.principal_id, auth.key_id,
                                            narrowed.capabilities, "pending", reason,
                                            expires_at, kind="expansion")
        else:
            options = [narrow(pg, requested, lattice)
                       for pg in store.list_active_for_key(auth.parent_key_id, now)]
            best = next((n for n in options if n.capabilities and not clipped(requested, n)),
                        None)
            if best is None:
                pooled = [c for n in options for c in n.capabilities]
                clip = [r for r in requested if not any(lattice.le(r, c) for c in pooled)] \
                    or list(requested)
                raise _clipped_error(clip, pooled)
            grant = store.insert_child_grant(auth.principal_id, auth.key_id, "expansion",
                                             best, reason, expires_at, auth.key_id)
    except (ValueError, TypeError, GrantInvariantError) as exc:
        raise PolicyError(400, str(exc), "invalid_capabilities") from exc
    audit(auth.name, "permission.requested", grant.id, {"expires_at": expires_at})
    notify.notify_grant_request({**_grant_view(grant), "key_id": auth.key_id,
                                 "key_name": auth.name, "reason": reason})
    return {"id": grant.id, "status": grant.status}


def get_permission_status(auth, grant_id: str) -> dict:
    store.sweep_expired()
    g = store.get(grant_id)
    if g is None or g.key_id != auth.key_id:
        raise PolicyError(404, "no such permission request", "not_found")
    return _grant_view(g)


def list_my_permissions(auth, limit: int = 50, cursor: int | None = None) -> dict:
    store.sweep_expired()
    limit = max(1, min(int(limit), 200))
    sql, args = "SELECT rowid AS r, id FROM grants WHERE key_id = ?", [auth.key_id]
    if cursor is not None:
        sql += " AND rowid < ?"
        args.append(cursor)
    sql += " ORDER BY rowid DESC LIMIT ?"
    args.append(limit + 1)
    with db.connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [_grant_view(g) for g in (store.get(r["id"]) for r in rows) if g is not None]
    return {"items": items, "next_cursor": rows[-1]["r"] if more and rows else None}


# ---- queued actions -------------------------------------------------------------------

def get_action_status(auth, action_id: str) -> dict:
    return queue.get_for_key(auth, action_id)


def list_my_actions(auth, status: str | None = None, limit: int = 50,
                    cursor: int | None = None) -> dict:
    return queue.list_for_key(auth, status, limit, cursor)


def cancel_action(auth, action_id: str) -> dict:
    return queue.cancel_for_key(auth, action_id)
