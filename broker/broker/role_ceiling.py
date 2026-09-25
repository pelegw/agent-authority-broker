"""The role as a ceiling: what it does to a key's capabilities, spelled out.

A key's role never grants anything. In `effective = P ∩ G ∩ R` it only caps
what the key's capabilities (its grants) reach: under `read-draft` a direct
capability's writes still run as drafts, under `read-only` they are denied,
and under `full` nothing is capped, so the capabilities decide. That is easy
to miss when an owner ticks "direct" and the key's calls keep queueing, so
the approval cards, the admin grant list and `get_my_access` use this module
to say it per action ("post_item: draft, capped by ceiling read-draft"). The
console's capability editor shows the same thing client-side from
`CEILING_MODES`, which a test holds equal to `roles.role_caps`.

Nothing here decides a call; `policy.evaluate` does, with the full algebra.
The rule is built from the same pieces, so the two cannot disagree: the
role's capabilities come from `roles.role_caps` (a meet keeps the lower
mode) and the mode a call runs at from `policy.run_mode`. A test checks this
module's answers against real engine decisions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from .authority.capability import MODE_RANK, Capability
from .authority.effective import effective_with_chains
from .authority.roles import ROLE_FULL, ROLE_RANK, role_caps
from .plugins.manifest import Manifest
from .policy import run_mode

DENIED = "denied"


def key_ceiling(roles: Iterable[str]) -> str | None:
    """The ceiling bounding a key: the lowest role along its key chain (root
    to self), None for an empty chain. R is met for every key in the chain,
    and each role's mode per side effect only rises with its rank, so meeting
    them all is exactly meeting the lowest one. An unknown role ranks below
    every known one (and caps everything, see `mode_under`)."""
    chain = list(roles)
    if not chain:
        return None
    return min(chain, key=lambda r: ROLE_RANK.get(r, -1))


def auth_ceiling(auth) -> str:
    """The ceiling of an authenticated key (an AuthContext), with the same
    fallback `effective` uses when the chain roles are absent."""
    return key_ceiling(tuple(auth.chain_roles) or (auth.role,)) or auth.role


def mode_under(manifest: Manifest, action: str, cap_mode: str, role: str) -> str | None:
    """The mode a call to `action` runs at under a capability of `cap_mode`
    bounded by the ceiling `role`, or None when it cannot run at all. Under
    `full` this is what the capability alone gives. An unknown action, role
    or mode answers None: what this module cannot read, it never shows as
    runnable."""
    act = manifest.action(action)
    if act is None or role not in ROLE_RANK or cap_mode not in MODE_RANK:
        return None
    if act.side_effect == "read":
        # A capability never holds a read at draft: capability.normalize
        # splits reads out at direct, whatever mode was asked for.
        cap_mode = "direct"
    bound = next((r.mode for r in role_caps(manifest, role) if action in r.actions), None)
    if bound is None:
        return None
    mode = cap_mode if MODE_RANK[cap_mode] <= MODE_RANK[bound] else bound
    return run_mode(mode, act)


def lowered(manifest: Manifest, cap: Capability, role: str) -> dict[str, str]:
    """{action: the mode the ceiling lowers it to, or DENIED} for every action
    of `cap` whose mode the ceiling changes; empty under `full`. An action the
    capability cannot run by itself (a draft capability over an action that
    cannot be drafted) is not the ceiling's doing and is left out."""
    out = {}
    for action in sorted(cap.actions):
        alone = mode_under(manifest, action, cap.mode, ROLE_FULL)
        if alone is None:
            continue
        capped = mode_under(manifest, action, cap.mode, role)
        if capped != alone:
            out[action] = capped or DENIED
    return out


def grant_lowered(manifests: Mapping[str, Manifest], caps: Iterable[Capability],
                  role: str) -> dict[str, str]:
    """`lowered` over a whole grant, labelled by action (`<target>.<action>`
    once the grant spans more than one target, so two plugins' actions of the
    same name stay apart). A capability on an unregistered plugin is skipped:
    nothing about it can be shown."""
    caps = [c for c in caps if c.target in manifests]
    several = len({c.target for c in caps}) > 1
    out: dict[str, str] = {}
    for cap in caps:
        for action, mode in lowered(manifests[cap.target], cap, role).items():
            label = f"{cap.target}.{action}" if several else action
            # The same action in two capabilities of one grant: the ceiling's
            # bound depends on the action alone, so both give the same answer;
            # were they ever to differ, the more restrictive one is shown.
            if out.get(label) != DENIED:
                out[label] = mode
    return dict(sorted(out.items()))


def note(role: str, capped: Mapping[str, str]) -> str | None:
    """One sentence for the human deciding a permission request, or None when
    the ceiling changes nothing. Approving such a request does not do what
    the agent asked, and the owner should know that before tapping Approve."""
    if not capped:
        return None
    queued = [a for a, m in capped.items() if m == "draft"]
    denied = [a for a, m in capped.items() if m == DENIED]
    parts = []
    if queued:
        parts.append(f"{', '.join(queued)} will still queue for your approval")
    if denied:
        parts.append(f"{', '.join(denied)} will stay denied")
    return (f"This key's ceiling is {role}: {' and '.join(parts)}, whatever this grant "
            "says. Raise the key's ceiling to change that.")


def uncapped_with_chains(auth, now: int, plugin_states, lattice
                         ) -> list[tuple[Capability, tuple[str, ...]]]:
    """What the key's capabilities give before its ceiling: the live
    evaluation (`effective_with_chains`) with every role in its key chain
    lifted to `full`, i.e. P ∩ G minus denies, with the same chain walk and
    fail-closed rules. For showing an agent "your capability says direct,
    your ceiling says draft" and nothing else: it is never an authority, and
    the lifted context it builds never leaves this function. Meeting with
    `full` changes no capability (it covers every action at direct), and the
    role meet commutes with the denies (role capabilities have no
    selectors), so the real effective set is exactly these met with the
    ceiling."""
    roles = tuple(auth.chain_roles) or (auth.role,)
    lifted = replace(auth, role=ROLE_FULL, chain_roles=tuple(ROLE_FULL for _ in roles))
    return effective_with_chains(lifted, now, plugin_states, lattice)
