"""Shared HTTP client for the Gmail, Calendar and Drive APIs.

One class for all three adapters (the plan's `_google/client.py`): it asks
the connection for an access token minted for exactly the call's scope set,
sends one request, and maps the answer onto the broker's contract
(docs/plugin-api.md, "Errors"):

  never reached Google (connect error/timeout)           503, not performed
  lost after sending: a read (GET)                       503, nothing changed
  lost after sending: a write                            502, outcome unknown
  401 (token rejected)                                   503; the cached token
                                                         is dropped, the retry
                                                         mints a fresh one
  403 rate limit / 429                                   429
  other 4xx                                              passthrough (404 is
                                                         one "not found")
  5xx, 3xx or an unreadable 2xx: a read                  503
  5xx, 3xx or an unreadable 2xx: a write                 502

Reads are split from writes because a read that timed out changed nothing
at Google, so retrying it is always safe; a write that timed out may have
happened, and retrying it could send an email twice.

The bearer token goes only into the Authorization header of this one
request; it is never logged, never in an error, never in a repr.
"""

import logging
from typing import Any

import httpx
from aab_plugin_runtime import AdapterError

from .transport import DEFAULT_TIMEOUT_SECONDS, SAFE_METHODS, open_client

log = logging.getLogger("aab_plugin_google.client")

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"
DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"

_RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"})


class GoogleClient:
    def __init__(self, connection, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.connection = connection
        self._transport = transport
        self._timeout = timeout

    def __repr__(self) -> str:
        return "GoogleClient()"

    def request(self, method: str, url: str, requirements: dict, *,
                params: dict | None = None, json: Any = None, content: bytes | None = None,
                headers: dict | None = None, raw: bool = False) -> Any:
        """One API call on a token for exactly `requirements`' scopes.
        Returns parsed JSON ({} for an empty body), or bytes when `raw`."""
        method = method.upper()
        read = method in SAFE_METHODS
        token = self.connection.mint(requirements)
        hdrs = {"Authorization": f"Bearer {token.value}", **(headers or {})}
        try:
            with open_client(self._transport, self._timeout) as c:
                resp = c.request(method, url, params=_clean(params), json=json,
                                 content=content, headers=hdrs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise AdapterError(503, f"Google unreachable ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            if read:
                raise AdapterError(503, f"Google read failed ({type(exc).__name__})") from exc
            raise AdapterError(502, "Google call failed with unknown outcome "
                                    f"({type(exc).__name__})") from exc
        if resp.status_code == 401:
            self.connection.invalidate(requirements)
            raise AdapterError(503, "Google rejected the access token; retry")
        _raise_for(resp, read)
        if raw:
            return resp.content
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise AdapterError(503 if read else 502,
                               "Google returned an unreadable response") from exc


def _clean(params: dict | None) -> dict | None:
    """Drop None values: an absent query parameter, not the string 'None'."""
    if params is None:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _raise_for(resp: httpx.Response, read: bool) -> None:
    status = resp.status_code
    if 200 <= status < 300:
        return
    message, reason = _error(resp)
    if status == 429 or (status == 403 and reason in _RATE_REASONS):
        raise AdapterError(429, "Google rate limit reached; retry later")
    if status == 404:
        # One message for "missing" whatever Google said, like hidden == 404.
        raise AdapterError(404, "not found")
    if 400 <= status < 500:
        raise AdapterError(status, f"Google refused: {message}")
    # 3xx (never followed) and 5xx promise nothing about a write.
    log.warning("google answered %s", status)
    if read:
        raise AdapterError(503, f"Google answered {status}")
    raise AdapterError(502, f"Google answered {status}; outcome unknown")


def _error(resp: httpx.Response) -> tuple[str, str]:
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        err = None
    if isinstance(err, dict):
        reasons = [e.get("reason") for e in err.get("errors") or [] if isinstance(e, dict)]
        reason = next((r for r in reasons if isinstance(r, str)), "")
        message = err.get("message") if isinstance(err.get("message"), str) else ""
        return (message or f"status {resp.status_code}")[:300], reason
    return f"status {resp.status_code}", ""
