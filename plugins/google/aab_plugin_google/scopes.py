"""OAuth scope names: the short form manifests use, and the URLs Google wants.

Manifests name scopes the way Google's docs do in prose (`gmail.readonly`,
`calendar.events`, `drive`), which keeps `target_permissions`, the decision
record and `get_my_access` readable. The connection turns them into the
URLs the token endpoint expects. The mapping is closed on purpose: a name
that does not have the expected shape is a packaging error and stops the
container at boot, rather than turning into a scope nobody reviewed.
"""

import re
from collections.abc import Iterable

from aab_plugin_runtime import AdapterError

API_PREFIX = "https://www.googleapis.com/auth/"
# Gmail's full-access scope is the one that does not live under API_PREFIX.
FULL_MAIL = "mail.google.com"
FULL_MAIL_URL = "https://mail.google.com/"

_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9]+)*$")


def scope_url(name: str) -> str:
    """`gmail.readonly` -> `https://www.googleapis.com/auth/gmail.readonly`."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"not a Google scope name: {name!r}")
    return FULL_MAIL_URL if name == FULL_MAIL else API_PREFIX + name


def scope_name(url: str) -> str | None:
    """The reverse, or None for a URL this plugin never asks for (Google may
    report extra granted scopes such as `openid` via include_granted_scopes)."""
    if url == FULL_MAIL_URL:
        return FULL_MAIL
    if isinstance(url, str) and url.startswith(API_PREFIX):
        name = url[len(API_PREFIX):]
        return name if _NAME_RE.match(name) else None
    return None


def names(urls: Iterable[str]) -> list[str]:
    """Short names of the URLs this plugin knows, sorted (for status output)."""
    return sorted({n for n in (scope_name(u) for u in urls) if n})


def manifest_scopes(manifest: dict) -> frozenset[str]:
    """Every scope name the manifest's actions may need (boot-time validated)."""
    out = set()
    for action in manifest.get("actions", []):
        for name in (action.get("target_permissions") or {}):
            scope_url(name)                     # raises on a malformed name
            out.add(name)
    return frozenset(out)


def requirement_urls(requirements: object) -> tuple[str, ...]:
    """The exact scope set a call needs, from the broker's CallScope
    `credential` (`{"permissions": {scope_name: level}}`), as sorted URLs.

    Fails closed: a missing or malformed requirement is refused, never read
    as "whatever the refresh token allows"."""
    perms = requirements.get("permissions") if isinstance(requirements, dict) else None
    if not isinstance(perms, dict) or not perms:
        raise AdapterError(400, "call scope carries no credential requirements")
    try:
        return tuple(sorted({scope_url(n) for n in perms}))
    except ValueError as exc:
        raise AdapterError(400, "call scope names an unknown Google scope") from exc
