"""Read the broker's CallScope for one call, failing closed.

Every `/perform` carries the broker's decision as JSON (docs/plugin-api.md,
"CallScope"): per resource kind a `deny` set and an optional `allow_only`
set, the covering capability's constraints, and the credential
requirements. This module turns that JSON into small typed values the
adapters check against, with three rules:

  * `allow_only: null` means unrestricted and `[]` means nothing at all.
    Coercing `[]` to "unrestricted" (as WA_GW's per-key allowlist did)
    would fail open;
  * anything malformed (a string where a list belongs, a constraint of the
    wrong type, a constraint the manifest does not declare) is a 400, never
    "no restriction";
  * an absent constraint is unrestricted, exactly as in the grant algebra:
    a flag reads as `true`, a range as no bound, a level as its top.

Kinds whose ids are case-insensitive (email addresses, domains, calendar
ids) are compared lowercased on both sides, which can only make a deny
match more and an allow match the same address it names.
"""

from dataclasses import dataclass
from typing import Any

from aab_plugin_runtime import AdapterError

# Kinds compared case-insensitively (their ids are addresses or domains).
CASELESS_KINDS = frozenset({"contact", "domain", "attendee", "calendar"})


@dataclass(frozen=True)
class Visibility:
    """One kind's visibility. `allow` None = unrestricted; empty = nothing."""
    deny: frozenset[str] = frozenset()
    allow: frozenset[str] | None = None

    @property
    def restricted(self) -> bool:
        return bool(self.deny) or self.allow is not None

    def denied(self, resource_id: str) -> bool:
        return resource_id in self.deny

    def admits(self, resource_id: str) -> bool:
        """Deny wins; then the allow set, when there is one."""
        if resource_id in self.deny:
            return False
        return self.allow is None or resource_id in self.allow

    def check_named(self, resource_id: str, what: str) -> None:
        """For an id the agent itself names in a write (a recipient, a label
        to add): outside the capability's allow set is a 403 (the agent can
        read its own grant), a denied id is a 404 (hidden == missing). The
        allow check runs FIRST, so a 404 never tells the agent which ids
        outside its grant happen to be hidden."""
        if self.allow is not None and resource_id not in self.allow:
            raise AdapterError(403, f"{what} is outside your grant")
        if resource_id in self.deny:
            raise AdapterError(404, "not found")


class CallScope:
    """The parsed scope. `forms` maps each declared constraint (and scalar
    narrowing) name to `(form, level_values)` from the plugin's manifest."""

    def __init__(self, raw: Any, forms: dict[str, tuple[str, tuple[str, ...]]]):
        if not isinstance(raw, dict):
            raise AdapterError(400, "malformed call scope")
        self.request_id = raw.get("request_id") if isinstance(raw.get("request_id"), str) else ""
        self.requirements = raw.get("credential") or {}
        if not isinstance(self.requirements, dict):
            raise AdapterError(400, "malformed call scope: credential")
        self._vis = self._parse_visibility(raw.get("visibility"))
        self._constraints = self._parse_constraints(raw.get("constraints"), forms)

    # ---- visibility ------------------------------------------------------------

    @staticmethod
    def _parse_visibility(vis: Any) -> dict[str, Visibility]:
        if vis is None:
            return {}
        if not isinstance(vis, dict):
            raise AdapterError(400, "malformed call scope: visibility")
        out = {}
        for kind, entry in vis.items():
            if not isinstance(kind, str) or not isinstance(entry, dict):
                raise AdapterError(400, "malformed call scope: visibility entry")
            deny = _ids(entry.get("deny"), kind, "deny", allow_none=True) or []
            allow = _ids(entry.get("allow_only"), kind, "allow_only", allow_none=True)
            if kind in CASELESS_KINDS:
                deny = [d.lower() for d in deny]
                allow = None if allow is None else [a.lower() for a in allow]
            out[kind] = Visibility(frozenset(deny), None if allow is None else frozenset(allow))
        return out

    def vis(self, kind: str) -> Visibility:
        return self._vis.get(kind, Visibility())

    # ---- constraints ---------------------------------------------------------------

    @staticmethod
    def _parse_constraints(raw: Any, forms) -> dict[str, Any]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise AdapterError(400, "malformed call scope: constraints")
        out = {}
        for name, value in raw.items():
            spec = forms.get(name)
            if spec is None:
                # A constraint this plugin does not know cannot be enforced;
                # refusing is the only answer that does not fail open.
                raise AdapterError(400, f"unknown constraint {name!r} in call scope")
            form, values = spec
            if form == "range":
                ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
            elif form == "flag":
                ok = isinstance(value, bool)
            else:
                ok = isinstance(value, str) and value in values
            if not ok:
                raise AdapterError(400, f"malformed constraint {name!r} in call scope")
            out[name] = value
        return out

    def flag(self, name: str) -> bool:
        """A flag; absent means true (unrestricted)."""
        return self._constraints.get(name, True) is True

    def bound(self, name: str) -> int | None:
        """A range; absent means no bound."""
        return self._constraints.get(name)

    def level(self, name: str, top: str) -> str:
        """A level; absent means its top value."""
        return self._constraints.get(name, top)


def _ids(value: Any, kind: str, field: str, *, allow_none: bool) -> list[str] | None:
    if value is None and allow_none:
        return None
    # A bare string must never be read as a list of its characters.
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise AdapterError(400, f"malformed call scope: visibility.{kind}.{field}")
    return list(value)


def constraint_forms(manifest: dict) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Scalar dimensions a scope may carry for this manifest."""
    out = {}
    for c in manifest.get("constraints", []) or []:
        out[c["name"]] = (c["form"], tuple(c.get("values") or ()))
    for n in manifest.get("narrowings", []) or []:
        if n["form"] in ("range", "flag", "level") and not n.get("derived_from"):
            out[n["dimension"]] = (n["form"], tuple(n.get("values") or ()))
    return out
