"""Roles: the coarse, per-key cap R(role) in effective = P ∩ G ∩ R.

A role never grants anything on its own (a key with no grants can do
nothing); it bounds what the key's grants can reach, by side effect:

  read-only   reads direct
  read-draft  reads direct; writes and destructive actions at draft
  read-act    reads and writes direct; destructive actions at draft
  full        everything direct

Roles are totally ordered (ROLES, low -> high), which is what lets a
delegated key's role be checked as "<= its parent's".
"""

from ..plugins.manifest import Manifest
from .capability import Capability

ROLE_READ_ONLY = "read-only"
ROLE_READ_DRAFT = "read-draft"
ROLE_READ_ACT = "read-act"
ROLE_FULL = "full"
ROLES = (ROLE_READ_ONLY, ROLE_READ_DRAFT, ROLE_READ_ACT, ROLE_FULL)
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}

# role -> [(side effects, mode)]; each entry becomes one capability.
_SHAPE = {
    ROLE_READ_ONLY: [(("read",), "direct")],
    ROLE_READ_DRAFT: [(("read",), "direct"), (("write", "destructive"), "draft")],
    ROLE_READ_ACT: [(("read", "write"), "direct"), (("destructive",), "draft")],
    ROLE_FULL: [(("read", "write", "destructive"), "direct")],
}


def check_role(role: str) -> str:
    if role not in ROLE_RANK:
        raise ValueError(f"unknown role {role!r} (want one of {list(ROLES)})")
    return role


def role_caps(manifest: Manifest, role: str) -> list[Capability]:
    """R(role) for one target: all-"*" capabilities shaped by side effect."""
    out = []
    for effects, mode in _SHAPE[check_role(role)]:
        actions = manifest.actions_by_effect(*effects)
        if actions:
            out.append(Capability(manifest.id, actions, mode=mode))
    return sorted(out)
