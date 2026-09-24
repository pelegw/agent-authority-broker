"""Owner-only operations: keys and their root grants, grant decisions, queued
action decisions, and hidden resources.

Every function takes the acting `AdminContext`; decisions record the owner's
username and surface (`decided_by_principal`, `decided_via`), never "admin".
Nothing in the agent surface imports this module (a test walks the import
graph), which is what makes "agents cannot approve" structural.
"""

from __future__ import annotations

import json
import logging
import time

from .. import auth, db, hidden
from ..actions import deliver, queue
from ..audit import audit
from ..authority import store
from ..authority.capability import caps_to_json, from_json, normalize_all, to_json
from ..authority.denies import parse_denies
from ..authority.roles import check_role
from ..errors import PolicyError
from ..logging_setup import kv
from ..plugins.adapter import AdapterError
from ..plugins.manifest import ManifestError
from ..plugins.registry import get_registry
from .deny_input import normalize_denies

log = logging.getLogger(__name__)
_GRANT_VERBS = {"active": "approved", "rejected": "rejected", "revoked": "revoked"}


def _audit(ctx, action: str, resource: str = "", detail: dict | None = None,
           result: str = "ok") -> None:
    audit(ctx.username, action, resource, detail, result,
          actor_principal=ctx.principal_id, actor_via=ctx.via)


def _by(ctx) -> dict:
    """Who acted, through which surface (session | token | telegram)."""
    return {"by": ctx.username, "via": ctx.via}


# ---- capability / deny input ---------------------------------------------------------

def normalize_caps(raw: list) -> list:
    """Parse and normalize capability JSON against the registered manifests."""
    if not isinstance(raw, list):
        raise PolicyError(400, "capabilities must be a list", "invalid_capabilities")
    try:
        caps = [from_json(c) for c in raw]
        return normalize_all(caps, get_registry().manifests())
    except (ValueError, ManifestError) as exc:
        raise PolicyError(400, f"invalid capability: {exc}", "invalid_capabilities") from exc


# ---- keys ------------------------------------------------------------------------------

def _key_view(row: dict) -> dict:
    now = int(time.time())
    return {
        "id": row["id"], "name": row["name"], "role": row["role"],
        "rate_per_min": row["rate_per_min"], "disabled": bool(row["disabled"]),
        "expires_at": row["expires_at"], "created_at": row["created_at"],
        "last_used_at": row["last_used_at"], "last_used_ip": row["last_used_ip"],
        "parent_key_id": row["parent_key_id"], "created_by": row["created_by"],
        "denies": parse_denies(row["denies"]),
        "rotating": bool(row["prev_key_hash"] and (row["prev_expires_at"] or 0) > now),
    }


def _grant_view(g) -> dict:
    return {"id": g.id, "key_id": g.key_id, "parent_grant_id": g.parent_grant_id,
            "kind": g.kind, "status": g.status, "reason": g.reason,
            "capabilities": [to_json(c) for c in g.capabilities],
            "created_at": g.created_at, "decided_at": g.decided_at,
            "decided_by_principal": g.decided_by_principal, "decided_via": g.decided_via,
            "expires_at": g.expires_at, "requested_by_key_id": g.requested_by_key_id}


def create_key(ctx, name: str, role: str, rate_per_min: int, expires_at: int | None,
               capabilities: list, denies: dict | None = None) -> dict:
    """Create a key and its active root grant in one flow; plaintext once."""
    caps = normalize_caps(capabilities or [])
    clean_denies = normalize_denies(denies)
    try:
        new = auth.create_key(ctx.principal_id, name, role, rate_per_min, expires_at,
                              denies=clean_denies)
    except ValueError as exc:
        raise PolicyError(400, str(exc), "invalid_key") from exc
    grant_id = None
    if caps:
        try:
            grant_id = store.insert_root_grant(
                ctx.principal_id, new.key_id, caps, "active", "created with key", None,
                ctx.username, decided_via=ctx.via).id
        except (ValueError, TypeError) as exc:
            # Compensate: a key without the authority it was created for must
            # not linger half-made.
            with db.connect() as conn:
                conn.execute("DELETE FROM api_keys WHERE id = ?", (new.key_id,))
            raise PolicyError(400, str(exc), "invalid_capabilities") from exc
    _audit(ctx, "key.create", str(new.key_id),
           {"name": name, "role": role, "rate_per_min": rate_per_min,
            "expires_at": expires_at, "grant_id": grant_id})
    log.info("key created %s", kv(key_id=new.key_id, name=name, role=role,
                                  rate_per_min=rate_per_min, expires_at=expires_at,
                                  grant=grant_id, capabilities=len(caps), **_by(ctx)))
    return {"id": new.key_id, "name": name, "key": new.plaintext, "role": role,
            "expires_at": expires_at, "grant_id": grant_id,
            "note": "store this key now; it is never shown again"}


def _key_row(key_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    if row is None:
        raise PolicyError(404, "no such key", "not_found")
    return dict(row)


def list_keys() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM api_keys ORDER BY id").fetchall()
    return [_key_view(dict(r)) for r in rows]


def get_key(key_id: int) -> dict:
    out = _key_view(_key_row(key_id))
    out["grants"] = [_grant_view(g) for g in store.list_for_key(key_id, limit=500)]
    return out


def key_tree() -> list[dict]:
    """Every key as a forest: root keys with their delegated children nested.

    Each node is the key view plus `status` (its own row: active | disabled
    | expired), `live` (it and every ancestor are active and it is within
    the depth limit, i.e. it would authenticate), `depth`, its grants, and
    `children`. A key not reachable from any root (its parent row is missing,
    or a corrupted parent loop) is listed at the top level with
    `orphan: true`; it cannot authenticate, because its chain is broken."""
    now = int(time.time())
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM api_keys ORDER BY id")]
    ids = {r["id"] for r in rows}
    children: dict[int, list[dict]] = {}
    for r in rows:
        if r["parent_key_id"] is not None and r["parent_key_id"] in ids:
            children.setdefault(r["parent_key_id"], []).append(r)
    limit = auth.max_delegation_depth()
    seen: set[int] = set()

    def node(row: dict, depth: int, parent_live: bool) -> dict:
        seen.add(row["id"])
        status = ("disabled" if row["disabled"] else
                  "expired" if row["expires_at"] is not None and row["expires_at"] <= now
                  else "active")
        live = parent_live and status == "active" and depth <= limit
        out = _key_view(row)
        out.update(status=status, live=live, depth=depth,
                   grants=[_grant_view(g) for g in store.list_for_key(row["id"], limit=500)])
        # `seen` stops a corrupted parent loop from recursing forever.
        out["children"] = [node(c, depth + 1, live) for c in children.get(row["id"], ())
                           if c["id"] not in seen]
        return out

    forest = [node(r, 0, True) for r in rows if r["parent_key_id"] is None]
    for r in rows:        # a missing parent, or a parent loop: never reachable from a root
        if r["id"] not in seen:
            forest.append({**node(r, 0, False), "orphan": True})
    return forest


def _root_grant(key_id: int):
    for g in store.list_for_key(key_id, limit=500):
        if g.kind == "root" and g.parent_grant_id is None and g.status == "active":
            return g
    return None


def update_key(ctx, key_id: int, *, role: str | None = None, rate_per_min: int | None = None,
               expires_at: int | None = None, clear_expiry: bool = False,
               denies: dict | None = None, disabled: bool | None = None,
               capabilities: list | None = None) -> dict:
    row = _key_row(key_id)
    sets, args, detail = [], [], {}
    if role is not None:
        try:
            check_role(role)
        except ValueError as exc:
            raise PolicyError(400, str(exc), "invalid_key") from exc
        sets.append("role = ?")
        args.append(role)
        detail["role"] = role
    if rate_per_min is not None:
        if isinstance(rate_per_min, bool) or rate_per_min < 1:
            raise PolicyError(400, "rate_per_min must be a positive integer", "invalid_key")
        sets.append("rate_per_min = ?")
        args.append(rate_per_min)
        detail["rate_per_min"] = rate_per_min
    if expires_at is not None or clear_expiry:
        if expires_at is not None and expires_at <= int(time.time()):
            raise PolicyError(400, "expires_at is in the past", "invalid_key")
        sets.append("expires_at = ?")
        args.append(expires_at)
        detail["expires_at"] = expires_at
    if denies is not None:
        sets.append("denies = ?")
        args.append(json.dumps(normalize_denies(denies), sort_keys=True))
        detail["denies"] = True
    if disabled is not None:
        sets.append("disabled = ?")
        args.append(1 if disabled else 0)
        detail["disabled"] = disabled
    caps = normalize_caps(capabilities) if capabilities is not None else None
    if caps is not None and row["parent_key_id"] is not None:
        raise PolicyError(400, "a delegated key's authority comes from its parent; "
                               "edit the parent's grants", "invalid_key")
    if not sets and caps is None:
        raise PolicyError(400, "nothing to update", "bad_request")
    if sets:
        with db.connect() as conn:
            conn.execute(f"UPDATE api_keys SET {', '.join(sets)} WHERE id = ?", (*args, key_id))
    if caps is not None:
        root = _root_grant(key_id)
        if not caps:
            if root is not None:
                store.set_status(root.id, "revoked", ctx.username, ctx.via)
        elif root is None:
            store.insert_root_grant(ctx.principal_id, key_id, caps, "active", "owner edit",
                                    None, ctx.username, decided_via=ctx.via)
        else:
            store.set_root_capabilities(root.id, caps)
        detail["capabilities"] = caps_to_json(caps)
    _audit(ctx, "key.update", str(key_id), detail)
    log.info("key updated %s", kv(key_id=key_id, name=row["name"], fields=sorted(detail),
                                  disabled=disabled, **_by(ctx)))
    return get_key(key_id)


def rotate_key(ctx, key_id: int) -> dict:
    try:
        plaintext = auth.rotate_key(key_id)
    except KeyError as exc:
        raise PolicyError(404, "no such key", "not_found") from exc
    _audit(ctx, "key.rotate", str(key_id))
    log.info("key rotated %s", kv(key_id=key_id, **_by(ctx)))
    return {"id": key_id, "key": plaintext,
            "note": "the previous secret keeps working until the grace window ends"}


# ---- grants ---------------------------------------------------------------------------

def list_grants(status: str | None = None, key_id: int | None = None,
                limit: int = 100) -> list[dict]:
    store.sweep_expired()
    sql, args = "SELECT id FROM grants WHERE 1=1", []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if key_id is not None:
        sql += " AND key_id = ?"
        args.append(key_id)
    sql += " ORDER BY created_at DESC, id LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute(sql, args).fetchall()]
    out = []
    for gid in ids:
        g = store.get(gid)
        if g is not None:
            out.append(_grant_view(g))
    return out


def decide_grant(ctx, grant_id: str, status: str) -> dict:
    """approve (active) | reject (rejected) | revoke (revoked), atomically."""
    store.sweep_expired()
    if store.get(grant_id) is None:
        raise PolicyError(404, "no such grant", "not_found")
    if not store.set_status(grant_id, status, ctx.username, ctx.via):
        g = store.get(grant_id)
        raise PolicyError(409, f"grant is {g.status!r}; cannot become {status!r}", "conflict")
    _audit(ctx, f"grant.{status}", grant_id)
    g = store.get(grant_id)
    log.info("grant decided %s", kv(decision=_GRANT_VERBS.get(status, status), grant=grant_id,
                                    key_id=g.key_id, kind=g.kind, **_by(ctx)))
    return _grant_view(g)


# ---- queued actions -------------------------------------------------------------------

def approve_action(ctx, action_id: str) -> dict:
    queue.sweep()
    now = int(time.time())
    row = queue.get_row(action_id)
    if row is None:
        raise PolicyError(404, "no such action", "not_found")
    if row["status"] == "pending" and not get_registry().is_enabled(row["target"]):
        raise PolicyError(409, "the plugin is disabled; the action is held", "held")
    if row["run_at"] and row["run_at"] > now:
        # Approved now, delivered at run_at by the scheduler.
        if not deliver.claim(action_id, "scheduled", now, ctx=ctx):
            raise deliver.conflict(action_id)
        _audit(ctx, "action.approve", action_id, {"run_at": row["run_at"]})
        log.info("action approved %s", kv(action_id=action_id, target=row["target"],
                                          action=row["action"], run_at=row["run_at"],
                                          **_by(ctx)))
        return {"id": action_id, "status": "scheduled", "run_at": row["run_at"]}
    if not deliver.claim(action_id, "sending", now, ctx=ctx):
        raise deliver.conflict(action_id)
    _audit(ctx, "action.approve", action_id)
    log.info("action approved %s", kv(action_id=action_id, target=row["target"],
                                      action=row["action"], **_by(ctx)))
    return deliver.deliver_claimed(action_id, queue.get_row(action_id), "pending",
                                   ctx.username, ctx=ctx)


def reject_action(ctx, action_id: str) -> dict:
    queue.sweep()
    if not deliver.claim(action_id, "rejected", int(time.time()), ctx=ctx):
        raise deliver.conflict(action_id)
    _audit(ctx, "action.reject", action_id)
    log.info("action rejected %s", kv(action_id=action_id, **_by(ctx)))
    return {"id": action_id, "status": "rejected"}


def cancel_action(ctx, action_id: str) -> dict:
    """Cancel a scheduled (not yet fired) action."""
    if not deliver.claim(action_id, "canceled", int(time.time()), from_status="scheduled",
                         ctx=ctx):
        raise deliver.conflict(action_id, expected="scheduled")
    _audit(ctx, "action.cancel", action_id)
    log.info("action canceled %s", kv(action_id=action_id, **_by(ctx)))
    return {"id": action_id, "status": "canceled"}


# ---- hidden resources -----------------------------------------------------------------

def add_hidden(ctx, target: str, kind: str, resource_id: str, label: str = "",
               reason: str = "") -> dict:
    reg = get_registry()
    manifest, adapter = reg.manifests().get(target), reg.adapter(target)
    if manifest is None or adapter is None:
        raise PolicyError(404, "no such plugin", "not_found")
    res = manifest.resources.get(kind)
    if res is None or not res.hideable:
        raise PolicyError(400, f"{target}: resource kind {kind!r} is not hideable", "bad_request")
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise PolicyError(400, "resource_id must be a non-empty string", "bad_request")
    rid = resource_id.strip()
    try:
        if res.normalize:
            rid = adapter.normalize(kind, rid)
        if not label:
            label = adapter.label(kind, [rid]).get(rid, "")
    except AdapterError as exc:
        if exc.status == 400:
            raise PolicyError(400, exc.message, "bad_request") from exc
        # Labels are cosmetic, but a failed normalize must not store a raw id
        # that would silently never match.
        if res.normalize:
            raise PolicyError(503, "plugin unavailable; cannot normalize the id",
                              "unavailable") from exc
    out = hidden.add(target, kind, rid, label[:200], reason[:500])
    get_registry().clear_cache()
    _audit(ctx, "hidden.add", f"{target}:{kind}:{rid}", {"reason": reason[:200]})
    # The id only: the label and the owner's reason stay in the database.
    log.info("resource hidden %s", kv(target=target, kind=kind, resource=rid, **_by(ctx)))
    return out


def remove_hidden(ctx, target: str, kind: str, resource_id: str) -> dict:
    if not hidden.remove(target, kind, resource_id):
        raise PolicyError(404, "that resource is not hidden", "not_found")
    _audit(ctx, "hidden.remove", f"{target}:{kind}:{resource_id}")
    log.info("resource unhidden %s", kv(target=target, kind=kind, resource=resource_id,
                                        **_by(ctx)))
    return {"target": target, "kind": kind, "resource_id": resource_id, "removed": True}
