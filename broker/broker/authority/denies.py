"""Deny sets: the part of authority that lives OUTSIDE the grant lattice.

The lattice has only allow-statements, so "never this chat" cannot be a
capability. Denies are plain sets {target: {kind: [ids]}} subtracted after
the lattice: per-key denies (api_keys.denies) and, in phase 3, the owner's
hidden_resources. They only ever grow along a delegation chain: a child's
effective denies are the union of its own and every ancestor's, computed at
authentication time, so a child can never un-deny what its parent denies.

Kept in its own module (rather than effective.py) because auth.py needs
`merged_denies` and effective.py depends on the store, which depends on auth.
"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

from .capability import Capability, FormTable

Denies = dict[str, dict[str, list[str]]]


def parse_denies(value: Any) -> Denies:
    """Validate and canonicalize a deny set (dict or its JSON text).
    Raises ValueError on any malformed shape: a deny set that silently parsed
    as empty would fail open."""
    if isinstance(value, (str, bytes)):
        value = json.loads(value or "{}")
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("denies must be an object {target: {kind: [ids]}}")
    out: Denies = {}
    for target, kinds in value.items():
        if not isinstance(target, str) or not isinstance(kinds, Mapping):
            raise ValueError("denies must be an object {target: {kind: [ids]}}")
        for kind, ids in kinds.items():
            if (not isinstance(kind, str) or isinstance(ids, (str, bytes))
                    or not isinstance(ids, Iterable)
                    or not all(isinstance(i, str) and i for i in ids)):
                raise ValueError(f"denies.{target}.{kind} must be a list of ids")
            if ids:
                out.setdefault(target, {})[kind] = sorted(set(ids))
    return out


def merged_denies(key_chain: Iterable[Mapping[str, Any]]) -> Denies:
    """Union of the `denies` of every key in a chain (root -> leaf)."""
    merged: dict[str, dict[str, set[str]]] = {}
    for key in key_chain:
        for target, kinds in parse_denies(key.get("denies") if hasattr(key, "get")
                                          else key["denies"]).items():
            for kind, ids in kinds.items():
                merged.setdefault(target, {}).setdefault(kind, set()).update(ids)
    return {t: {k: sorted(v) for k, v in ks.items()} for t, ks in merged.items()}


def denies_le(smaller: Denies, larger: Denies) -> bool:
    """Every id denied in `smaller` is also denied in `larger`."""
    return all(set(ids) <= set(larger.get(t, {}).get(k, ()))
               for t, ks in smaller.items() for k, ids in ks.items())


def is_denied(denies: Denies, target: str, kind: str, resource_id: str) -> bool:
    """Resolution-time check, for resources reached through a "*" selector."""
    return resource_id in denies.get(target, {}).get(kind, ())


def apply_denies(caps: Iterable[Capability], denies: Denies,
                 forms: FormTable) -> list[Capability]:
    """Subtract denied ids from explicit set selectors (a dimension's ids are
    matched against denies of the dimension's resource kind). A capability
    whose selector becomes empty is bottom and dropped. "*" selectors cannot
    be subtracted from; `is_denied` covers them at resolution time."""
    out = []
    for cap in caps:
        target_denies = denies.get(cap.target, {})
        tf = forms.get(cap.target, {})
        selector = dict(cap.selector)
        dead = False
        for dim, ids in cap.selector.items():
            spec = tf.get(dim)
            denied = set(target_denies.get(spec.kind if spec else dim, ()))
            if denied & ids:
                selector[dim] = ids - denied
                dead = dead or not selector[dim]
        if not dead:
            out.append(cap if selector == dict(cap.selector) else cap.replace(selector=selector))
    return sorted(set(out))
