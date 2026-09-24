"""Minted credentials and their in-memory cache.

A `GitHubToken` is what the connection hands the action handlers: the bearer
value plus what it was minted for. The value is reachable only through
`bearer()`; it is excluded from `repr`/`str`, so a token that ends up in a
log line, a traceback or an assertion message shows as `<redacted>`.

The cache lives in this process only. Tokens are never written to disk,
never returned over the plugin API and never logged. Keys are the exact
(installation, sorted repositories, sorted permissions) tuple a call needs,
so two calls share a token only when they need exactly the same authority.
"""

import threading
from dataclasses import dataclass, field

# GitHub installation tokens live 60 minutes; reuse one for at most 50, and
# never within 5 minutes of its own expiry (clock skew, long calls).
MAX_REUSE_SECONDS = 50 * 60
EXPIRY_MARGIN_SECONDS = 5 * 60

CacheKey = tuple[str, tuple[str, ...] | None, tuple[tuple[str, str], ...]]


@dataclass(frozen=True, repr=False)
class GitHubToken:
    mode: str                                      # "app" | "pat"
    repositories: tuple[str, ...] | None           # names; None = all the credential reaches
    permissions: tuple[tuple[str, str], ...]       # sorted (unit, level); () for a PAT
    _value: str = field(compare=False)

    def bearer(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return (f"GitHubToken(mode={self.mode!r}, repositories={self.repositories!r}, "
                f"permissions={self.permissions!r}, value=<redacted>)")

    __str__ = __repr__


class TokenCache:
    def __init__(self):
        self._entries: dict[CacheKey, tuple[float, GitHubToken]] = {}
        self._lock = threading.Lock()

    def get(self, key: CacheKey, now: float) -> GitHubToken | None:
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            if hit[0] <= now:
                del self._entries[key]
                return None
            return hit[1]

    def put(self, key: CacheKey, token: GitHubToken, now: float,
            expires_at: float | None) -> None:
        until = now + MAX_REUSE_SECONDS
        if expires_at is not None:
            until = min(until, expires_at - EXPIRY_MARGIN_SECONDS)
        if until <= now:
            return                      # usable once, not worth keeping
        with self._lock:
            self._entries[key] = (until, token)

    def drop(self, token: GitHubToken) -> None:
        """Forget a token GitHub has refused (revoked, installation changed)."""
        with self._lock:
            for k in [k for k, (_, t) in self._entries.items() if t is token]:
                del self._entries[k]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __repr__(self) -> str:
        return f"TokenCache(entries={len(self)})"
