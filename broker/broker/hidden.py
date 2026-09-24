"""Hidden resources and the visibility sets handed to plugins.

Two kinds of deny live outside the grant lattice (docs/grant-algebra.md):
the owner's `hidden_resources` (no key may see them) and each key's own
`denies` (merged along its delegation chain at authentication). For a call,
both are unioned per resource kind into the `deny` set of the CallScope.

`allow_only` is the other half of visibility: the covering capability's
explicit selector for that kind (e.g. rooms r1, r2), which list reads must
stay inside. Deny always wins over allow_only.

Hidden == 404: a hidden resource must be indistinguishable from a missing
one on every surface, so nothing here ever returns hidden ids to an agent;
only the admin plane lists them. Enforcement is by normalized id; the label
is display-only, so renaming a resource can never unhide it.
"""

import time
from collections.abc import Callable, Iterable

from . import db
from .authority.capability import Capability

Ancestors = Callable[[str, str], Iterable[str]]


# ---- the table -------------------------------------------------------------------

def list_hidden(target: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM hidden_resources", []
    if target:
        sql += " WHERE target = ?"
        args.append(target)
    sql += " ORDER BY target, kind, resource_id"
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def add(target: str, kind: str, resource_id: str, label: str = "", reason: str = "") -> dict:
    """Hide a resource (idempotent: re-adding updates label and reason)."""
    now = int(time.time())
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO hidden_resources (target, kind, resource_id, label, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(target, kind, resource_id)"
            " DO UPDATE SET label = excluded.label, reason = excluded.reason",
            (target, kind, resource_id, label, reason, now))
    return {"target": target, "kind": kind, "resource_id": resource_id, "label": label,
            "reason": reason}


def remove(target: str, kind: str, resource_id: str) -> bool:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM hidden_resources WHERE target = ? AND kind = ?"
                           " AND resource_id = ?", (target, kind, resource_id))
        return cur.rowcount > 0


def hidden_sets(target: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    with db.connect() as conn:
        for r in conn.execute("SELECT kind, resource_id FROM hidden_resources WHERE target = ?",
                              (target,)):
            out.setdefault(r["kind"], set()).add(r["resource_id"])
    return out


# ---- per-call visibility ---------------------------------------------------------

def deny_sets(auth, target: str) -> dict[str, set[str]]:
    """Owner-hidden ids UNION the key's merged denies, per kind."""
    out = hidden_sets(target)
    for kind, ids in (auth.denies or {}).get(target, {}).items():
        out.setdefault(kind, set()).update(ids)
    return out


def is_denied(deny: set[str], resource_id: str, kind: str, ancestors: Ancestors) -> bool:
    """A resource is denied if it, or any ancestor (hiding a folder hides
    its subtree), is in the deny set."""
    if resource_id in deny:
        return True
    return any(a in deny for a in ancestors(kind, resource_id))


def allow_only(cap: Capability | None, dims: dict[str, str], kind: str) -> set[str] | None:
    """The cap's explicit selector ids for `kind` (intersected when several
    dimensions map to the same kind), or None when unrestricted.
    `dims` maps each applicable dimension name to its resource kind."""
    if cap is None:
        return None
    out: set[str] | None = None
    for dim, dim_kind in dims.items():
        if dim_kind != kind or dim not in cap.selector:
            continue
        ids = set(cap.selector[dim])
        out = ids if out is None else out & ids
    return out


def visibility(auth, target: str, kind: str, cap: Capability | None = None,
               dims: dict[str, str] | None = None) -> tuple[set[str], set[str] | None]:
    """(deny, allow_only) for one resource kind of one target."""
    return deny_sets(auth, target).get(kind, set()), allow_only(cap, dims or {}, kind)
