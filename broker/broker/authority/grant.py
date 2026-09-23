"""Grants and the one way to derive a child grant: `narrow()`.

A grant is a list of capabilities held by a key. Root grants are authored by
the owner; every other grant (an agent's expansion request, a delegation to a
child key) is a child of an existing grant and must be <= it.

That rule is enforced structurally, not by convention:
  * `narrow(parent, requested)` is the only producer of `NarrowedCapabilities`
    (its constructor refuses to run without a sentinel private to this
    module), and
  * `store.insert_child_grant` accepts nothing but a `NarrowedCapabilities`,
    then re-checks `grant_le` against the parent row as a post-condition.

`grant_le` uses single-cover semantics: every child capability must fit
inside ONE parent capability. That is conservative (a child spanning two
parent caps is refused) but O(n*m) and gives each child capability a single,
explainable parent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import InitVar, dataclass, field

from ..plugins.manifest import Manifest
from .capability import (Ancestors, Capability, FormTable, cap_le, dedupe, form_table,
                         meet, no_ancestry)

__all__ = ["GRANT_KINDS", "GRANT_STATUSES", "Grant", "Lattice", "NarrowedCapabilities",
           "clipped", "grant_le", "narrow"]

GRANT_KINDS = ("root", "expansion", "delegation")
GRANT_STATUSES = ("pending", "active", "rejected", "expired", "revoked")

# The construction proof for NarrowedCapabilities. Deliberately not exported
# (see __all__); only narrow() below passes it.
_PROOF = object()


@dataclass(frozen=True)
class Lattice:
    """The context the algebra runs in: each target's forms (from validated
    manifests) plus folder ancestry for subtree dimensions."""
    forms: FormTable
    ancestors: Ancestors = no_ancestry

    @classmethod
    def from_manifests(cls, manifests: Iterable[Manifest] | Mapping[str, Manifest],
                       ancestors: Ancestors = no_ancestry) -> Lattice:
        ms = manifests.values() if isinstance(manifests, Mapping) else manifests
        return cls(form_table(ms), ancestors)

    def le(self, child: Capability, parent: Capability) -> bool:
        return cap_le(child, parent, self.forms, self.ancestors)

    def meet(self, a: Capability, b: Capability) -> Capability | None:
        return meet(a, b, self.forms, self.ancestors)


@dataclass(frozen=True)
class Grant:
    """One row of the grants table, parsed."""
    id: str
    principal_id: str
    key_id: int
    parent_grant_id: str | None
    kind: str
    capabilities: tuple[Capability, ...]
    status: str
    reason: str = ""
    created_at: int = 0
    decided_at: int | None = None
    decided_by_principal: str | None = None
    decided_via: str | None = None
    expires_at: int | None = None
    requested_by_key_id: int | None = None

    def is_live(self, now: int) -> bool:
        """Active and unexpired: the same predicate as store.ACTIVE_WHERE."""
        return self.status == "active" and (self.expires_at is None or self.expires_at > now)


def _caps(x) -> tuple[Capability, ...]:
    """Capabilities of a Grant, a NarrowedCapabilities, or a plain sequence."""
    if isinstance(x, (Grant, NarrowedCapabilities)):
        return tuple(x.capabilities)
    return tuple(x)


def grant_le(child, parent, lattice: Lattice) -> bool:
    """`child`/`parent`: a Grant, a NarrowedCapabilities, or a capability list."""
    # Single cover: each child capability is <= some single parent capability.
    parent_caps = _caps(parent)
    return all(any(lattice.le(c, p) for p in parent_caps) for c in _caps(child))


@dataclass(frozen=True)
class NarrowedCapabilities:
    """Capabilities proven <= a parent, as produced by `narrow()`.

    Constructing one anywhere else raises. `dataclasses.replace()` raises too:
    the proof is an InitVar with a default, so a copy made by replace() gets
    the default and fails the check.
    """
    capabilities: tuple[Capability, ...]
    parent_grant_id: str | None
    lattice: Lattice = field(repr=False)
    _proof: InitVar[object] = None

    def __post_init__(self, _proof: object) -> None:
        if _proof is not _PROOF:
            raise TypeError("NarrowedCapabilities can only be produced by narrow()")

    def __init_subclass__(cls, **kwargs):
        # A subclass could override __post_init__ and skip the proof check.
        raise TypeError("NarrowedCapabilities cannot be subclassed")


def narrow(parent: Grant | Sequence[Capability], requested: Iterable[Capability],
           lattice: Lattice) -> NarrowedCapabilities:
    """Meet every requested capability with every parent capability on the
    same target; keep the non-bottom results, deduped.

    `requested` should already be normalized against its manifest. Each result
    is <= the requested capability it came from and <= one parent capability,
    so the output always satisfies grant_le(output, parent).
    """
    parent_caps = _caps(parent)
    out = dedupe(lattice.meet(r, p) for r in requested for p in parent_caps)
    parent_id = parent.id if isinstance(parent, Grant) else None
    return NarrowedCapabilities(tuple(out), parent_id, lattice, _PROOF)


def clipped(requested: Iterable[Capability], narrowed: NarrowedCapabilities) -> list[Capability]:
    """The requested capabilities that did not survive narrowing intact (not
    covered by a single narrowed capability). Empty means the caller got
    exactly what it asked for; otherwise a 400 can show these alongside
    `narrowed.capabilities` ("this is what your parent can give")."""
    lat = narrowed.lattice
    return sorted({r for r in requested
                   if not any(lat.le(r, n) for n in narrowed.capabilities)})
