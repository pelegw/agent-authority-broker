"""Read the broker's CallScope, failing closed: visibility and credential requirements.

Every `/perform` carries (docs/plugin-api.md, "CallScope"):

  visibility  {kind: {"deny": [ids], "allow_only": [ids] | null}}
              kinds used here: "repo" and "branch"
  credential  {"permissions": {unit: "read"|"write"},
               "resources": {"repo": ["owner/name", ...]}}   (resources optional)

`visibility()` is the WhatsApp plugin's fail-closed reader, unchanged in
behaviour: `null` allow_only means unrestricted, `[]` means NOTHING (coercing
it to "unrestricted" would fail open), and anything malformed is a 400,
never "no restriction".

`credential()` is stricter still, because it decides what gets minted:
missing or empty `permissions` is a 400, not "mint with everything the
installation has" (omitting `permissions` from GitHub's token request does
exactly that). Repo ids are re-normalized here, so a requirement can never
name a repository by a second spelling.
"""

import re
from typing import Any

from aab_plugin_runtime import AdapterError

from .ids import normalize_repo

LEVELS = ("read", "write")
_UNIT_RE = re.compile(r"[a-z][a-z_]{0,63}")
MAX_REPOS = 500          # GitHub's limit for `repositories` in a token request


def visibility(scope: Any, kind: str) -> tuple[list[str], list[str] | None]:
    """`(deny, allow_only)` for `kind`. deny is de-duplicated, order kept;
    allow_only is None (unrestricted) or a list, possibly empty."""
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
    deny = _ids(entry.get("deny"), f"visibility.{kind}.deny") or []
    allow_only = _ids(entry.get("allow_only"), f"visibility.{kind}.allow_only")
    return list(dict.fromkeys(deny)), allow_only


def is_visible(resource_id: str, deny: list[str], allow_only: list[str] | None) -> bool:
    """Deny wins; then allow_only, when present, must contain the id."""
    if resource_id in deny:
        return False
    return allow_only is None or resource_id in allow_only


def branch_allowed(branch: str, deny, allow_only) -> None:
    """Enforce the `branch` pattern selector: exact-string membership, as the
    grant algebra defines `pattern` (a grant naming `feat/*` reaches only a
    branch literally named `feat/*`, and git forbids `*` in branch names).
    A branch in the key's denies is hidden (404, like a missing one); one
    outside the selector is refused with 403."""
    if branch in deny:
        raise AdapterError(404, "not found")
    if allow_only is not None and branch not in allow_only:
        raise AdapterError(403, "branch not allowed by your grant")


def credential(scope: Any) -> tuple[dict[str, str], list[str] | None]:
    """(permissions, repos) from `scope["credential"]`. repos is None when the
    capability leaves repositories unrestricted, else the normalized list."""
    if not isinstance(scope, dict):
        raise AdapterError(400, "malformed call scope")
    cred = scope.get("credential")
    if not isinstance(cred, dict):
        raise AdapterError(400, "malformed call scope: credential")
    perms = cred.get("permissions")
    if not isinstance(perms, dict) or not perms:
        raise AdapterError(400, "malformed call scope: credential.permissions")
    for unit, level in perms.items():
        if not isinstance(unit, str) or not _UNIT_RE.fullmatch(unit) or level not in LEVELS:
            raise AdapterError(400, "malformed call scope: credential.permissions")
    resources = cred.get("resources", {})
    if resources is None:
        resources = {}
    if not isinstance(resources, dict):
        raise AdapterError(400, "malformed call scope: credential.resources")
    repos = resources.get("repo")
    if repos is not None:
        repos = _ids(repos, "credential.resources.repo")
        if len(repos) > MAX_REPOS:
            raise AdapterError(400, f"a capability may name at most {MAX_REPOS} repositories")
        repos = list(dict.fromkeys(normalize_repo(r) for r in repos))
    return dict(perms), repos


# Installed permission sets can also say "admin" (above write); a requirement
# never asks for it. Anything unknown ranks 0, i.e. grants nothing.
_RANK = {"read": 1, "write": 2, "admin": 3}


def within(requested: dict[str, str], allowed: dict[str, str]) -> bool:
    """Is every requested permission at or below the allowed level?"""
    return all(_RANK.get(level, 99) <= _RANK.get(allowed.get(unit), 0)
               for unit, level in requested.items())


def missing(requested: dict[str, str], allowed: dict[str, str]) -> list[str]:
    """`unit:level` for each requested permission `allowed` does not cover."""
    return sorted(f"{unit}:{level}" for unit, level in requested.items()
                  if not within({unit: level}, allowed))


def _ids(value: Any, where: str) -> list[str] | None:
    if value is None:
        return None
    # A bare string must not be read as a list of its characters.
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise AdapterError(400, f"malformed call scope: {where}")
    return list(value)
