"""Live evaluation of what a key may do right now.

    P  = ceiling: enabled ∧ connected plugins, all "*", direct
    G  = for each live grant of the key: chain_meet over grant -> ... -> root
    R  = role caps of the key AND of every ancestor key
    effective = { meet(meet(c, P), R) : c ∈ G }, minus the key's merged denies

Nothing here trusts a stored child grant: the chain is re-walked and
re-met on every call, so revoking or expiring any link, editing a root
narrower, disabling a plugin, or lowering an ancestor's role takes effect
on the next request with no cascade writes.

R is applied for every key in the chain (not just the caller's) so a child
can never out-rank an ancestor whose role was lowered after delegation.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence

from ..plugins.manifest import Manifest
from . import store
from .capability import Ancestors, Capability, dedupe, no_ancestry
from .ceiling import PluginState, ceiling
from .denies import apply_denies, is_denied, merged_denies  # noqa: F401  (re-exported)
from .grant import Grant, Lattice
from .roles import role_caps

log = logging.getLogger(__name__)

_ROOT_KINDS = ("root", "expansion")
_CHILD_KINDS = ("expansion", "delegation")


def chain_meet(grant: Grant, chain: Sequence[Grant], lattice: Lattice, now: int,
               key_chain_ids: Sequence[int] | None = None) -> list[Capability]:
    """The capabilities a grant really carries: the meet down its chain.

    Bottom ([]) unless the chain is intact (root first, each link the parent
    of the next, ending at `grant`), every link is active and unexpired, all
    links share one principal, and (when `key_chain_ids` is given, root key
    first) the root grant belongs to the root key and each link's key is at
    or below the previous link's key in that lineage.
    """
    if not chain or chain[-1].id != grant.id or chain[0].parent_grant_id is not None:
        return []
    if chain[0].kind not in _ROOT_KINDS or any(g.kind not in _CHILD_KINDS for g in chain[1:]):
        return []
    for prev, link in zip(chain, chain[1:], strict=False):
        if link.parent_grant_id != prev.id:
            return []
    principal = chain[0].principal_id
    if any(not g.is_live(now) or g.principal_id != principal for g in chain):
        return []
    if key_chain_ids is not None and not _lineage_ok(chain, key_chain_ids):
        return []
    acc = list(chain[0].capabilities)
    for link in chain[1:]:
        acc = dedupe(lattice.meet(c, p) for c in link.capabilities for p in acc)
        if not acc:
            return []
    return _unexpired(acc, now)


def _lineage_ok(chain: Sequence[Grant], key_chain_ids: Sequence[int]) -> bool:
    index = {k: i for i, k in enumerate(key_chain_ids)}
    positions = [index.get(g.key_id) for g in chain]
    if any(p is None for p in positions):
        return False
    if positions[0] != 0 or positions[-1] != len(key_chain_ids) - 1:
        return False
    return all(a <= b for a, b in zip(positions, positions[1:], strict=False))


def _unexpired(caps: Iterable[Capability], now: int) -> list[Capability]:
    return [c for c in caps if c.expires_at is None or c.expires_at > now]


def _meet_all(caps: Iterable[Capability], bound: Sequence[Capability],
              lattice: Lattice) -> list[Capability]:
    return dedupe(lattice.meet(c, b) for c in caps for b in bound)


def effective(auth, now: int, plugin_states: Iterable[PluginState],
              manifests: Iterable[Manifest] | Mapping[str, Manifest],
              ancestors: Ancestors = no_ancestry) -> list[Capability]:
    """Effective capabilities of `auth` (an AuthContext) at time `now`.

    Fails closed: a grant row that does not parse, or anything unexpected
    while walking chains, yields [] for the whole key rather than a partial
    answer that might hide a restriction.
    """
    ms = list(manifests.values() if isinstance(manifests, Mapping) else manifests)
    lattice = Lattice.from_manifests(ms, ancestors)
    by_id = {m.id: m for m in ms}
    ceiling_caps = [c for c in ceiling(auth.principal_id, plugin_states) if c.target in by_id]
    if not ceiling_caps:
        return []
    chain_ids = tuple(auth.chain_key_ids) or (auth.key_id,)
    roles = tuple(auth.chain_roles) or (auth.role,)
    try:
        granted: list[Capability] = []
        for g in store.list_active_for_key(auth.key_id, now):
            granted.extend(chain_meet(g, store.chain(g.id), lattice, now, chain_ids))
    except ValueError:
        log.warning("unparseable grant for key %s; failing closed", auth.key_id)
        return []
    caps = _meet_all(granted, ceiling_caps, lattice)
    for role in roles:
        caps = _meet_all(caps, [r for m in by_id.values() for r in role_caps(m, role)], lattice)
    caps = apply_denies(caps, auth.denies, lattice.forms)
    return dedupe(_unexpired(caps, now))


def effective_with_chains(auth, now: int, plugin_states: Iterable[PluginState],
                          lattice: Lattice) -> list[tuple[Capability, tuple[str, ...]]]:
    """Like `effective`, but each capability carries the grant chain (ids,
    root -> leaf) it came from, which the engine records in the decision and
    charges in the capacity ledger. Same evaluation, same fail-closed rules;
    a capability reachable through two grants keeps the first chain.
    """
    manifests = {m.id: m for m, _, _ in plugin_states}
    ceiling_caps = ceiling(auth.principal_id, plugin_states)
    if not ceiling_caps:
        return []
    chain_ids = tuple(auth.chain_key_ids) or (auth.key_id,)
    roles = tuple(auth.chain_roles) or (auth.role,)
    role_bounds = [[r for m in manifests.values() for r in role_caps(m, role)]
                   for role in roles]
    out: dict[Capability, tuple[str, ...]] = {}
    try:
        for g in store.list_active_for_key(auth.key_id, now):
            chain = store.chain(g.id)
            caps = _meet_all(chain_meet(g, chain, lattice, now, chain_ids), ceiling_caps, lattice)
            for bound in role_bounds:
                caps = _meet_all(caps, bound, lattice)
            caps = apply_denies(caps, auth.denies, lattice.forms)
            for c in dedupe(_unexpired(caps, now)):
                out.setdefault(c, tuple(link.id for link in chain))
    except ValueError:
        log.warning("unparseable grant for key %s; failing closed", auth.key_id)
        return []
    return sorted(out.items(), key=lambda kv: kv[0])


__all__ = ["apply_denies", "chain_meet", "effective", "effective_with_chains", "is_denied",
           "merged_denies"]
