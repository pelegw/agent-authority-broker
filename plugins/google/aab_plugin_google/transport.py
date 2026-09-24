"""The one way this plugin opens an HTTP connection to Google.

Shared by the OAuth connection (token endpoint) and the API client so both
get the same safety settings:
  * redirects are never followed: a redirect would carry the bearer token
    (or the refresh request) to wherever it points;
  * proxy environment variables are ignored (`trust_env=False`), so no
    ambient HTTP(S)_PROXY can route tokens through a third party;
  * one timeout below the broker's 30 s plugin timeout, so this plugin
    reports its own clean 503/502 before the broker has to guess.

Tests hand in an `httpx.MockTransport` (the fake Google) and no socket is
ever opened.
"""

import httpx

DEFAULT_TIMEOUT_SECONDS = 25.0
SAFE_METHODS = frozenset({"GET", "HEAD"})


def open_client(transport: httpx.BaseTransport | None,
                timeout: float = DEFAULT_TIMEOUT_SECONDS) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=transport, follow_redirects=False,
                        trust_env=False)
