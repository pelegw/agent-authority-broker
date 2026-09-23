"""Capability statements and their lattice order: `cap_le` and `meet`.

A capability is an allow-statement on one target: which actions, restricted
to which resources (the `selector`, set-valued dimensions), under which
scalar limits (`constraints`), at which `mode`, until when, within which
`budget`. There is no deny and no "widen" representation, so the only
operations are comparison (`cap_le`) and intersection (`meet`); grants can
only narrow because nothing in this vocabulary can express more.

Conventions (docs/grant-algebra.md has the full table):
  * an absent selector dimension, constraint, expiry or budget field means
    "unrestricted" (top); `"*"` is accepted on input and dropped;
  * an empty action set or an empty set-valued selector is bottom, which is
    represented by `None`, never by a Capability object;
  * anything the lattice cannot interpret (unknown target, unknown dimension,
    a value of the wrong type) compares as NOT <= and meets to None, so a
    stale or tampered capability loses authority instead of gaining it.

`FormTable` (target -> {name: FormSpec}) carries the manifest's forms into
these pure functions, and `ancestors(kind, id)` supplies folder ancestry for
`subtree` dimensions (injected; the default knows no ancestry, so only exact
ids match).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..plugins.manifest import MODES, SCALAR_FORMS, SET_FORMS, Manifest

WILDCARD = "*"
MODE_RANK = {m: i for i, m in enumerate(MODES)}      # draft < direct
BUDGET_FIELDS = ("per_minute", "per_day")

Ancestors = Callable[[str, str], Iterable[str]]


def no_ancestry(kind: str, resource_id: str) -> tuple[str, ...]:
    """Default ancestry: nothing is inside anything else except itself."""
    return ()


@dataclass(frozen=True)
class FormSpec:
    """How one dimension or constraint of a target compares and meets."""
    form: str                     # list | subtree | pattern | range | flag | level
    values: tuple[str, ...] = ()  # level order, low -> high
    kind: str = ""                # resource kind (ancestry for subtree, denies)


FormTable = Mapping[str, Mapping[str, FormSpec]]


def target_forms(manifest: Manifest) -> dict[str, FormSpec]:
    """The lattice-relevant forms a manifest declares. Derived narrowings are
    excluded: they are computed from actions and never stored in a grant."""
    out: dict[str, FormSpec] = {}
    for n in manifest.narrowings:
        if n.derived_from:
            continue
        out[n.dimension] = FormSpec(n.form, tuple(n.values or ()), n.resource or n.dimension)
    for c in manifest.constraints:
        out[c.name] = FormSpec(c.form, tuple(c.values or ()), c.name)
    return out


def form_table(manifests: Iterable[Manifest]) -> dict[str, dict[str, FormSpec]]:
    return {m.id: target_forms(m) for m in manifests}


# ---- the capability value ----------------------------------------------------

class Capability:
    """An immutable allow-statement. Construct via the constructor (which
    validates shapes) or `from_json`; bottom is never representable here."""

    __slots__ = ("target", "actions", "selector", "constraints", "mode",
                 "expires_at", "budget", "_key")

    def __init__(self, target: str, actions: Iterable[str],
                 selector: Mapping[str, Any] | None = None,
                 constraints: Mapping[str, Any] | None = None,
                 mode: str = "direct", expires_at: int | None = None,
                 budget: Mapping[str, int] | None = None):
        if not isinstance(target, str) or not target:
            raise ValueError("capability target must be a non-empty string")
        acts = _str_set(actions, "actions")
        if not acts:
            raise ValueError("empty action set is bottom; represent it as None")
        sel: dict[str, frozenset[str]] = {}
        for dim, value in (selector or {}).items():
            if not isinstance(dim, str) or not dim:
                raise ValueError("selector dimensions must be non-empty strings")
            if value == WILDCARD:
                continue     # "*" == absent == unrestricted
            ids = _str_set(value, f"selector.{dim}")
            if not ids:
                raise ValueError(f"empty selector {dim!r} is bottom; represent it as None")
            sel[dim] = ids
        cons: dict[str, Any] = {}
        for name, value in (constraints or {}).items():
            if not isinstance(name, str) or not name:
                raise ValueError("constraint names must be non-empty strings")
            if not isinstance(value, (bool, int, str)):
                raise ValueError(f"constraint {name!r} must be a bool, int or level name")
            if isinstance(value, int) and not isinstance(value, bool) and value < 0:
                raise ValueError(f"constraint {name!r} must be >= 0")
            cons[name] = value
        if mode not in MODE_RANK:
            raise ValueError(f"mode must be one of {MODES}")
        if expires_at is not None and (not isinstance(expires_at, int) or isinstance(expires_at, bool)):
            raise ValueError("expires_at must be an int unix timestamp or None")
        bud: dict[str, int] = {}
        for f, v in (budget or {}).items():
            if f not in BUDGET_FIELDS:
                raise ValueError(f"unknown budget field {f!r}")
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"budget.{f} must be an int >= 0")
            bud[f] = v
        s = object.__setattr__
        s(self, "target", target)
        s(self, "actions", acts)
        s(self, "selector", MappingProxyType(dict(sorted(sel.items()))))
        s(self, "constraints", MappingProxyType(dict(sorted(cons.items()))))
        s(self, "mode", mode)
        s(self, "expires_at", expires_at)
        s(self, "budget", MappingProxyType(dict(sorted(bud.items()))))
        s(self, "_key", json.dumps(to_json(self), sort_keys=True, separators=(",", ":")))

    def __setattr__(self, name, value):
        raise AttributeError("Capability is immutable")

    def __eq__(self, other) -> bool:
        return isinstance(other, Capability) and self._key == other._key

    def __hash__(self) -> int:
        return hash(self._key)

    def __lt__(self, other: Capability) -> bool:   # deterministic ordering for lists
        return self._key < other._key

    def __repr__(self) -> str:
        return f"Capability({self._key})"

    def replace(self, **changes) -> Capability:
        d = dict(target=self.target, actions=self.actions, selector=self.selector,
                 constraints=self.constraints, mode=self.mode,
                 expires_at=self.expires_at, budget=self.budget)
        d.update(changes)
        return Capability(**d)


def _str_set(value: Any, what: str) -> frozenset[str]:
    # A bare string would iterate into characters; refuse it outright.
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ValueError(f"{what} must be a list of strings")
    out = frozenset(value)
    if not all(isinstance(x, str) and x for x in out):
        raise ValueError(f"{what} must contain non-empty strings")
    return out


# ---- JSON --------------------------------------------------------------------

_JSON_KEYS = {"target", "actions", "selector", "constraints", "mode", "expires_at", "budget"}


def to_json(cap: Capability) -> dict:
    """Canonical JSON shape (sorted lists, "*" dims omitted)."""
    return {
        "target": cap.target,
        "actions": sorted(cap.actions),
        "selector": {d: sorted(v) for d, v in cap.selector.items()},
        "constraints": dict(cap.constraints),
        "mode": cap.mode,
        "expires_at": cap.expires_at,
        "budget": dict(cap.budget),
    }


def from_json(data: Any) -> Capability:
    """Parse a stored/submitted capability. Unknown keys are rejected: a field
    we dropped silently could have been a restriction."""
    if not isinstance(data, Mapping):
        raise ValueError("capability must be an object")
    unknown = set(data) - _JSON_KEYS
    if unknown:
        raise ValueError(f"unknown capability fields: {sorted(unknown)}")
    if "target" not in data or "actions" not in data:
        raise ValueError("capability needs target and actions")
    return Capability(
        target=data["target"], actions=data["actions"],
        selector=_mapping(data.get("selector"), "selector"),
        constraints=_mapping(data.get("constraints"), "constraints"),
        mode=data.get("mode", "direct"), expires_at=data.get("expires_at"),
        budget=_mapping(data.get("budget"), "budget"))


def _mapping(value: Any, what: str) -> Mapping:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be an object")
    return value


def caps_to_json(caps: Iterable[Capability]) -> str:
    return json.dumps([to_json(c) for c in sorted(caps)], sort_keys=True,
                      separators=(",", ":"))


def caps_from_json(text: str) -> list[Capability]:
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("capabilities must be a JSON list")
    return [from_json(d) for d in data]


# ---- normalization -----------------------------------------------------------

def normalize(cap: Capability, manifest: Manifest) -> list[Capability]:
    """Canonical form of a capability against its manifest.

    Expands action glob sugar, validates every dimension and constraint
    against its declared form, drops values equal to top (flag true, the
    highest level), and forces reads to direct mode. A draft capability that
    mixes reads and writes is split in two, so every stored draft capability
    contains writes only; that keeps `cap_le`/`meet` purely form-driven.
    Returns [] when the result is bottom. Raises ValueError on anything the
    manifest does not declare.
    """
    if cap.target != manifest.id:
        raise ValueError(f"capability target {cap.target!r} is not {manifest.id!r}")
    forms = target_forms(manifest)
    actions = manifest.expand_actions(cap.actions)
    if not actions:
        return []
    selector: dict[str, frozenset[str]] = {}
    for dim, ids in cap.selector.items():
        spec = forms.get(dim)
        if spec is None or spec.form not in SET_FORMS:
            raise ValueError(f"{manifest.id}: {dim!r} is not a selector dimension")
        selector[dim] = ids
    constraints: dict[str, Any] = {}
    for name, value in cap.constraints.items():
        spec = forms.get(name)
        if spec is None or spec.form not in SCALAR_FORMS:
            raise ValueError(f"{manifest.id}: {name!r} is not a constraint")
        if not _scalar_valid(spec, value):
            raise ValueError(f"{manifest.id}: invalid value {value!r} for {spec.form} {name!r}")
        if not _is_top(spec, value):
            constraints[name] = value
    reads = actions & manifest.actions_by_effect("read")
    writes = actions - reads

    def build(acts: frozenset[str], mode: str) -> Capability:
        return Capability(cap.target, acts, selector, constraints, mode,
                          cap.expires_at, cap.budget)

    if cap.mode == "direct" or not reads:
        out = [build(actions, "direct" if not writes else cap.mode)]
    elif not writes:
        out = [build(reads, "direct")]
    else:
        out = [build(reads, "direct"), build(writes, "draft")]
    return sorted(out)


def normalize_all(caps: Iterable[Capability], manifests: Mapping[str, Manifest]) -> list[Capability]:
    """Normalize a list (each against its own target's manifest), deduped."""
    out: set[Capability] = set()
    for c in caps:
        m = manifests.get(c.target)
        if m is None:
            raise ValueError(f"unknown target {c.target!r}")
        out.update(normalize(c, m))
    return sorted(out)


# ---- order and meet ----------------------------------------------------------

def cap_le(child: Capability, parent: Capability, forms: FormTable,
           ancestors: Ancestors = no_ancestry) -> bool:
    """child <= parent: everything child allows, parent allows too."""
    if child.target != parent.target:
        return False
    tf = forms.get(child.target)
    if tf is None:
        return False
    if not child.actions <= parent.actions:
        return False
    if MODE_RANK[child.mode] > MODE_RANK[parent.mode]:
        return False
    if parent.expires_at is not None and (child.expires_at is None
                                          or child.expires_at > parent.expires_at):
        return False
    for f in BUDGET_FIELDS:
        p = parent.budget.get(f)
        if p is not None:
            c = child.budget.get(f)
            if c is None or c > p:
                return False
    for dim in set(child.selector) | set(parent.selector):
        spec = tf.get(dim)
        if spec is None or spec.form not in SET_FORMS:
            return False
        p_ids = parent.selector.get(dim)
        if p_ids is None:
            continue                     # parent "*" accepts all
        c_ids = child.selector.get(dim)
        if c_ids is None:
            return False                 # child "*" exceeds an explicit parent
        if spec.form == "subtree":
            if not all(_inside(x, p_ids, spec.kind, ancestors) for x in c_ids):
                return False
        elif not c_ids <= p_ids:         # list and pattern: exact-string subset
            return False
    for name in set(child.constraints) | set(parent.constraints):
        spec = tf.get(name)
        if spec is None or spec.form not in SCALAR_FORMS:
            return False
        c, p = child.constraints.get(name), parent.constraints.get(name)
        if (c is not None and not _scalar_valid(spec, c)) or (
                p is not None and not _scalar_valid(spec, p)):
            return False
        if not _scalar_le(spec, c, p):
            return False
    return True


def meet(a: Capability, b: Capability, forms: FormTable,
         ancestors: Ancestors = no_ancestry) -> Capability | None:
    """Greatest capability <= both, or None (bottom)."""
    if a.target != b.target:
        return None
    tf = forms.get(a.target)
    if tf is None:
        return None
    actions = a.actions & b.actions
    if not actions:
        return None
    mode = a.mode if MODE_RANK[a.mode] <= MODE_RANK[b.mode] else b.mode
    expires = _min_opt(a.expires_at, b.expires_at)
    budget = {f: v for f in BUDGET_FIELDS
              if (v := _min_opt(a.budget.get(f), b.budget.get(f))) is not None}
    selector: dict[str, frozenset[str]] = {}
    for dim in set(a.selector) | set(b.selector):
        spec = tf.get(dim)
        if spec is None or spec.form not in SET_FORMS:
            return None
        av, bv = a.selector.get(dim), b.selector.get(dim)
        if spec.form == "subtree":
            ids = _subtree_meet(av, bv, spec.kind, ancestors)
        elif av is None or bv is None:
            ids = av if bv is None else bv
        else:
            ids = av & bv
        if not ids:
            return None
        selector[dim] = ids
    constraints: dict[str, Any] = {}
    for name in set(a.constraints) | set(b.constraints):
        spec = tf.get(name)
        if spec is None or spec.form not in SCALAR_FORMS:
            return None
        av, bv = a.constraints.get(name), b.constraints.get(name)
        if (av is not None and not _scalar_valid(spec, av)) or (
                bv is not None and not _scalar_valid(spec, bv)):
            return None
        value = _scalar_meet(spec, av, bv)
        if value is not None and not _is_top(spec, value):
            constraints[name] = value
    return Capability(a.target, actions, selector, constraints, mode, expires, budget)


def dedupe(caps: Iterable[Capability | None]) -> list[Capability]:
    """Drop bottoms and duplicates; deterministic order."""
    return sorted({c for c in caps if c is not None})


# ---- form helpers --------------------------------------------------------------

def _min_opt(a: int | None, b: int | None) -> int | None:
    """min where None means +infinity."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _inside(x: str, roots: frozenset[str], kind: str, ancestors: Ancestors) -> bool:
    return x in roots or any(p in roots for p in ancestors(kind, x) if p != x)


def _subtree_meet(a: frozenset[str] | None, b: frozenset[str] | None, kind: str,
                  ancestors: Ancestors) -> frozenset[str] | None:
    """Roots of the intersection of two subtree unions: the nodes of each side
    that lie inside the other, minus any root inside another kept root."""
    if a is None and b is None:
        return None
    if a is None or b is None:
        keep = set(a if b is None else b)
    else:
        keep = {x for x in a if _inside(x, b, kind, ancestors)} | {
            y for y in b if _inside(y, a, kind, ancestors)}
    # Pruning makes the representation canonical (unique for a given
    # denotation), which is what keeps meet exactly associative.
    return frozenset(x for x in keep
                     if not any(p in keep for p in ancestors(kind, x) if p != x))


def _scalar_valid(spec: FormSpec, value: Any) -> bool:
    if spec.form == "range":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if spec.form == "flag":
        return isinstance(value, bool)
    if spec.form == "level":
        return isinstance(value, str) and value in spec.values
    return False


def _is_top(spec: FormSpec, value: Any) -> bool:
    if spec.form == "flag":
        return value is True
    if spec.form == "level":
        return value == spec.values[-1]
    return False                          # a range has no finite top


def _scalar_le(spec: FormSpec, c: Any, p: Any) -> bool:
    """Scalar order where None (absent) is top."""
    if p is None or _is_top(spec, p):
        return True
    if c is None:
        return False
    if spec.form == "range":
        return c <= p
    if spec.form == "flag":
        return c is False                 # parent false => child false
    return spec.values.index(c) <= spec.values.index(p)


def _scalar_meet(spec: FormSpec, a: Any, b: Any) -> Any:
    if a is None:
        return b
    if b is None:
        return a
    if spec.form == "range":
        return min(a, b)
    if spec.form == "flag":
        return a and b
    return a if spec.values.index(a) <= spec.values.index(b) else b
