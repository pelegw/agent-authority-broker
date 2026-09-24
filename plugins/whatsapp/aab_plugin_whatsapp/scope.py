"""Read the broker's CallScope visibility for one resource kind, failing closed.

Every `/perform` carries `scope["visibility"][kind] = {"deny": [ids],
"allow_only": [ids] | null}` (docs/plugin-api.md, "CallScope"). This module
turns that JSON into the `(deny, allow_only)` pair the archive's
`_visibility_clause` takes, which is the same pair WA_GW's
`privacy.visible_filter` produced.

One difference from WA_GW is deliberate: WA_GW stored a key's read
allowlist as a list where *empty meant unrestricted*, so `visible_filter`
turned `[]` into `None`. In a CallScope `null` means unrestricted and `[]`
means "nothing is allowed". Passing `[]` through unchanged makes the SQL
`AND 0` (see nothing); coercing it to `None` would fail open.

Anything malformed (a string where a list belongs, non-string ids) is a 400:
a scope we cannot read is never treated as "no restriction".
"""

from typing import Any

from aab_plugin_runtime import AdapterError


def visibility(scope: Any, kind: str) -> tuple[list[str], list[str] | None]:
    """`(deny, allow_only)` for `kind`. deny is de-duplicated, order kept (stable
    SQL); allow_only is None (unrestricted) or a list, possibly empty."""
    if not isinstance(scope, dict):
        raise AdapterError(400, "malformed call scope")
    vis = scope.get("visibility")
    if vis is None:
        return [], None
    if not isinstance(vis, dict):
        raise AdapterError(400, "malformed call scope: visibility")
    entry = vis.get(kind)
    if entry is None:
        return [], None
    if not isinstance(entry, dict):
        raise AdapterError(400, f"malformed call scope: visibility.{kind}")
    deny = _ids(entry.get("deny"), kind, "deny", allow_none=True) or []
    allow_only = _ids(entry.get("allow_only"), kind, "allow_only", allow_none=True)
    return list(dict.fromkeys(deny)), allow_only


def _ids(value: Any, kind: str, field: str, *, allow_none: bool) -> list[str] | None:
    if value is None and allow_none:
        return None
    # A bare string must not be read as a list of its characters.
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise AdapterError(400, f"malformed call scope: visibility.{kind}.{field}")
    return list(value)


def is_visible(resource_id: str, deny: list[str], allow_only: list[str] | None) -> bool:
    """The same rule as `_visibility_clause`, for an id not read from the
    archive (a send may go to a chat the archive has never seen). Deny wins."""
    if resource_id in deny:
        return False
    return allow_only is None or resource_id in allow_only
