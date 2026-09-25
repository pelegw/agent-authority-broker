"""Agent-facing operations besides performing actions (that is engine.py).

Everything here acts as the calling key (an `AuthContext`) and discloses
only what the key could discover by trying: its own capabilities, grants
and queued actions. It never lists hidden resources or deny sets, because
listing them would reveal exactly what exists but is hidden.

Delegation (delegate / list / revoke) lives in services/delegation.py, which
builds on the helpers here.

This module must not import services/admin.py or identity/ (a test walks
the import graph): an agent has no path to approving anything.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

from .. import db, notify, role_ceiling
from ..actions import queue
from ..audit import audit
from ..auth import max_delegation_depth
from ..authority import store
from ..authority.capability import Capability, from_json, normalize_all, to_json
from ..authority.ceiling import ceiling
from ..authority.effective import effective_with_chains
from ..authority.grant import clipped, narrow
from ..authority.store import GrantInvariantError
from ..engine import adapter_error
from ..errors import PolicyError
from ..hidden import deny_sets, is_denied
from ..ledger import remaining
from ..logging_setup import kv
from ..plugins.adapter import AdapterError
from ..plugins.manifest import ManifestError
from ..plugins.registry import get_registry
from ..policy import enforced_where, run_mode
from ..runtime_settings import runtime_settings
from ..skill.generator import KeyContext, render

log = logging.getLogger(__name__)


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


def visible_caps(auth, caps, extra_denies: dict | None = None) -> list[dict]:
    """Capabilities as JSON with every hidden or denied id removed (the
    owner's hidden resources, the key's merged denies, and `extra_denies`),
    dropping any capability that loses a whole selector. The one way this
    surface shows capabilities it did not receive verbatim from the caller."""
    reg = get_registry()
    manifests = reg.manifests()
    out = []
    for cap in caps:
        m = manifests.get(cap.target)
        if m is None:
            continue
        deny = deny_sets(auth, cap.target)
        for kind, ids in ((extra_denies or {}).get(cap.target) or {}).items():
            deny.setdefault(kind, set()).update(ids)
        dims = {n.dimension: (n.resource or n.dimension) for n in m.narrowings}
        shown = _visible_cap(cap, deny, dims, reg.ancestors)
        if shown is not None:
            out.append(shown)
    return out


def _visible_cap(cap: Capability, deny: dict[str, set[str]], dims: dict[str, str],
                 ancestors=None) -> dict | None:
    """Capability JSON with denied/hidden ids removed from its selectors;
    None when that empties a selector (nothing left to show). An id under a
    hidden folder counts as hidden (the same ancestry rule the policy
    applies), so a subtree selector never names what hiding its parent hid."""
    anc = ancestors or (lambda kind, rid: ())
    out = to_json(cap)
    for dim, ids in list(out["selector"].items()):
        kind = dims.get(dim, dim)
        kept = [i for i in ids if not is_denied(deny.get(kind, set()), i, kind, anc)]
        if not kept:
            return None
        out["selector"][dim] = kept
    return out


def _uncapped_by_target(auth) -> dict[str, list[tuple[Capability, tuple[str, ...]]]]:
    """The key's capabilities before its role ceiling (display only; see
    role_ceiling.uncapped_with_chains), grouped by target."""
    reg = get_registry()
    out: dict[str, list] = {}
    for cap, chain in role_ceiling.uncapped_with_chains(auth, int(time.time()),
                                                        reg.plugin_states(), reg.lattice()):
        out.setdefault(cap.target, []).append((cap, chain))
    return out


def get_my_access(auth) -> dict:
    """Self-introspection: what this key can do, where it is enforced, and
    how much budget is left. Not audited (agents poll it).

    Capabilities are listed as the key's grants give them (`mode` is the
    capability's), with `ceiling` beside them: the lowest role along the key
    chain. Where that ceiling lowers an action's mode, the capability carries
    `effective_mode` {action: "draft" | "denied"}, what a call will really
    run at, so an agent can tell "my capability says direct but my ceiling
    says draft" (asking for more will not help; only the owner can raise the
    ceiling) without probing. The tool list (`reachable_actions`) stays on
    the capped set: it never offers an action the ceiling denies."""
    reg = get_registry()
    caps = _uncapped_by_target(auth)
    ceiling_role = role_ceiling.auth_ceiling(auth)
    targets = {}
    for pid, m in sorted(reg.enabled_manifests().items()):
        deny = deny_sets(auth, pid)
        dims = {n.dimension: (n.resource or n.dimension) for n in m.narrowings}
        health = reg.last_health(pid)
        entries, where = [], {}
        for cap, chain in caps.get(pid, []):
            shown = _visible_cap(cap, deny, dims, reg.ancestors)
            if shown is None:
                continue
            capped = role_ceiling.lowered(m, cap, ceiling_role)
            if capped:
                shown["effective_mode"] = capped
            for action in cap.actions:
                if capped.get(action) != role_ceiling.DENIED:
                    where.update(enforced_where(m, action, health))
            budget = remaining(chain, pid, sorted(cap.actions)[0])
            if budget:
                shown["remaining"] = budget
            shown["grant_chain"] = list(chain)
            entries.append(shown)
        targets[pid] = {"capabilities": entries, "enforced_where": dict(sorted(where.items()))}
    parent, children = _lineage(auth)
    return {
        "name": auth.name, "role": auth.role, "ceiling": ceiling_role,
        "rate_per_min": auth.rate_per_min,
        "key_expires_at": auth.expires_at, "credential_expires_at": auth.credential_expires_at,
        "depth": auth.depth, "delegated": auth.parent_key_id is not None,
        "parent": parent, "delegations": children,
        "can_delegate": auth.depth < max_delegation_depth(), "targets": targets,
    }


def _lineage(auth) -> tuple[str | None, int]:
    """(the delegating key's name or None, how many live keys this key has
    delegated directly). Only the parent's NAME: nothing else about another
    key is an agent's business."""
    parent = None
    if auth.parent_key_id is not None:
        conn = db.connect()
        try:
            row = conn.execute("SELECT name FROM api_keys WHERE id = ?",
                               (auth.parent_key_id,)).fetchone()
        finally:
            conn.close()
        parent = row["name"] if row else None
    return parent, live_children(auth)


def live_children(auth) -> int:
    """Keys this key delegated directly that are neither disabled nor expired."""
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE parent_key_id = ? AND principal_id = ?"
            " AND disabled = 0 AND (expires_at IS NULL OR expires_at > ?)",
            (auth.key_id, auth.principal_id, int(time.time()))).fetchone()[0]
    finally:
        conn.close()


def skill_doc(auth, base_url: str) -> str:
    """The skill doc for this key: only the actions it can reach right now,
    plus its current capabilities (from get_my_access, so never a hidden
    resource or a deny list). Served at /v1/me/skill and as the MCP
    resource broker://skill."""
    ctx = KeyContext(reachable=reachable_actions(auth), access=get_my_access(auth))
    return render(base_url, get_registry().enabled_manifests(), ctx)


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

def _grant_view(g, auth=None) -> dict:
    """A grant as JSON. With `auth` (every agent-facing answer) its
    capabilities go through visible_caps, so an id the owner hid, or the key
    was denied, after the grant was made never appears. Without it (the
    owner's notification card) the capabilities are shown as stored."""
    caps = [to_json(c) for c in g.capabilities] if auth is None else \
        visible_caps(auth, g.capabilities)
    return {"id": g.id, "kind": g.kind, "status": g.status, "capabilities": caps,
            "created_at": g.created_at, "decided_at": g.decided_at,
            "expires_at": g.expires_at}


def _clipped_error(auth, requested, narrowed_caps) -> PolicyError:
    # `allowed` is derived from the parent's grants, so it is filtered like
    # any capability this surface did not receive from the caller.
    log.info("permission request clipped %s", kv(key=auth.name, clipped=len(requested)))
    return clipped_error("exceeds what your parent can give",
                         "request only the allowed capabilities, or ask the owner directly",
                         requested, visible_caps(auth, narrowed_caps))


def clipped_error(message: str, hint: str, requested, allowed: list[dict]) -> PolicyError:
    """A 400 for a request that does not fit: `clipped` echoes the caller's
    own capabilities that exceeded, `allowed` is what could be given (the
    caller filters it through visible_caps first)."""
    return PolicyError(400, message, "clipped", hint=hint,
                       extra={"clipped": [to_json(c) for c in requested], "allowed": allowed})


def _default_to_draft(raw, manifests) -> object:
    """An agent's capability with no `mode` asks for draft: its writes and
    destructive actions queue for a human unless the agent says `direct`
    explicitly (reads are always direct; normalize splits them out). Least
    privilege by default: forgetting a field must never buy autonomy.

    A write that cannot be drafted (manifest `modes: [direct]`) would be
    unreachable in a draft capability, so asking for one without a mode is a
    400 that says to ask for direct explicitly, never a silent no-op grant."""
    if not isinstance(raw, Mapping) or "mode" in raw:
        return raw                    # explicit (or malformed: from_json reports it)
    m = manifests.get(raw.get("target"))
    if m is not None:
        try:
            names = m.expand_actions(raw.get("actions") or ())
        except (ManifestError, TypeError):
            names = frozenset()       # normalize reports the bad action list
        stuck = sorted(n for n in names if m.action(n).side_effect != "read"
                       and "draft" not in m.action(n).effective_modes)
        if stuck:
            raise PolicyError(
                400, f"{m.id}: {', '.join(stuck)} cannot be drafted; without a mode a "
                     "capability asks for draft", "invalid_capabilities",
                hint='set "mode": "direct" explicitly to ask for it')
    return {**raw, "mode": "draft"}


def normalize_request(capabilities) -> list[Capability]:
    """Parse and normalize capability JSON an agent sent, against the ENABLED
    plugins (a disabled plugin cannot be asked for). A capability without
    `mode` asks for draft (see _default_to_draft). 400 on anything invalid or
    on a request that normalizes to nothing. Owner-authored grants do not
    come through here and keep their explicit (or `direct`) mode."""
    if not isinstance(capabilities, list) or not capabilities:
        raise PolicyError(400, "capabilities must be a non-empty list", "invalid_capabilities")
    manifests = get_registry().enabled_manifests()
    try:
        requested = normalize_all([from_json(_default_to_draft(c, manifests))
                                   for c in capabilities], manifests)
    except (ValueError, ManifestError) as exc:
        raise PolicyError(400, f"invalid capability: {exc}", "invalid_capabilities") from exc
    if not requested:
        raise PolicyError(400, "the request is empty after normalization",
                          "invalid_capabilities")
    return requested


def request_permission(auth, capabilities: list, reason: str = "",
                       expires_in_hours: int | None = None) -> dict:
    """Ask the owner for more authority. The request is first narrowed
    against what the key's parent could give (the ceiling for a root key,
    the parent key's grants for a delegated one); anything clipped is a 400
    listing it, so a pending request is always grantable as asked."""
    reg = get_registry()
    requested = normalize_request(capabilities)
    now = int(time.time())
    expires_at = None
    if expires_in_hours is not None:
        if isinstance(expires_in_hours, bool) or expires_in_hours <= 0:
            raise PolicyError(400, "expires_in_hours must be positive", "bad_request")
        expires_at = now + min(expires_in_hours, runtime_settings().grant_max_hours) * 3600
    lattice = reg.lattice()
    reason = (reason or "")[:1000]
    try:
        if auth.parent_key_id is None:
            narrowed = narrow(ceiling(auth.principal_id, reg.plugin_states()), requested, lattice)
            if clipped(requested, narrowed):
                raise _clipped_error(auth, clipped(requested, narrowed), narrowed.capabilities)
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
                raise _clipped_error(auth, clip, pooled)
            grant = store.insert_child_grant(auth.principal_id, auth.key_id, "expansion",
                                             best, reason, expires_at, auth.key_id)
    except (ValueError, TypeError, GrantInvariantError) as exc:
        raise PolicyError(400, str(exc), "invalid_capabilities") from exc
    audit(auth.name, "permission.requested", grant.id, {"expires_at": expires_at})
    # The reason is the agent's free text for the owner: never logged.
    log.info("permission requested %s", kv(grant=grant.id, key=auth.name,
                                           capabilities=len(grant.capabilities),
                                           expires_at=expires_at))
    notify.notify_grant_request({**_grant_view(grant), "key_id": auth.key_id,
                                 "key_name": auth.name, "reason": reason})
    return {"id": grant.id, "status": grant.status}


def get_permission_status(auth, grant_id: str) -> dict:
    store.sweep_expired()
    g = store.get(grant_id)
    if g is None or g.key_id != auth.key_id:
        raise PolicyError(404, "no such permission request", "not_found")
    return _grant_view(g, auth)


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
    items = [_grant_view(g, auth) for g in (store.get(r["id"]) for r in rows) if g is not None]
    return {"items": items, "next_cursor": rows[-1]["r"] if more and rows else None}


# ---- queued actions -------------------------------------------------------------------

def get_action_status(auth, action_id: str) -> dict:
    return queue.get_for_key(auth, action_id)


def list_my_actions(auth, status: str | None = None, limit: int = 50,
                    cursor: int | None = None) -> dict:
    return queue.list_for_key(auth, status, limit, cursor)


def cancel_action(auth, action_id: str) -> dict:
    return queue.cancel_for_key(auth, action_id)
