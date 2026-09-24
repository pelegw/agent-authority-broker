"""HTTP client for api.github.com, with the broker's 503/502 contract applied.

Used by the connection (App JWT calls, token minting) and by the action
handlers. It holds no credential itself: every call names the bearer value
it uses, and that value is a request header only. It is never part of an
error message, a log line or this object's repr.

Status mapping (docs/plugin-api.md, "Errors"):

  never reached GitHub (connection refused, connect timeout)
      -> 503: not performed, safe to retry
  failed after sending (read timeout, reset), a 5xx, or a 2xx we cannot read
      -> 502: the action may have happened, never retried automatically
      -> 503 instead when `before_effect=True`: the request was a lookup
         that precedes the action's side effect (or a token mint), so the
         side effect provably has not happened
  3xx -> 404: GitHub redirects renamed/transferred repositories; following
         would act on an object whose name was never checked against the
         scope. Redirects are never followed (and would carry the token)
  401 -> 503: the credential was refused before anything ran
  403/429 rate limit -> 429 with `retry_after` (primary or secondary limit)
  403 other -> 403, 404 -> 404 "not found" (hidden and missing look alike)
  409 -> 409, 422 -> 400 (GitHub's validation message), other 4xx -> as-is

`GitHubError` keeps GitHub's own status and message for handlers that need
to tell cases apart (e.g. 422 "Reference already exists" -> 409).

Every refusal or failure logs one line: the method, GitHub's status (or the
transport error's class) and the status it maps to. Never the path (it can
name a file or a branch from the params), the bearer or GitHub's message.
"""

import logging
import time
from collections.abc import Callable

import httpx

from aab_plugin_runtime import AdapterError
from aab_plugin_runtime.logging_setup import kv

log = logging.getLogger("aab_plugin_github.api")

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "aab-plugin-github"
# Below the broker's 30 s plugin timeout: a hanging GitHub produces this
# plugin's own clean 502/503 before the broker has to guess.
DEFAULT_TIMEOUT_SECONDS = 25.0
MAX_RETRY_AFTER = 3600
NOT_FOUND = "not found"


class GitHubError(AdapterError):
    """A refusal or failure; `.status` follows the contract above."""

    def __init__(self, status: int, message: str, *, github_status: int | None = None,
                 github_message: str = ""):
        super().__init__(status, message)
        self.github_status = github_status
        self.github_message = github_message


class RateLimited(GitHubError):
    """GitHub's rate limit: 429, nothing performed. main.py answers it with
    a `Retry-After` header; the seconds are also in the message, which is
    what reaches the agent through the broker."""

    def __init__(self, retry_after: int, github_status: int):
        super().__init__(429, f"GitHub rate limit reached; retry after {retry_after} s",
                         github_status=github_status)
        self.retry_after = retry_after


class GitHubAPI:
    def __init__(self, base_url: str = API_URL, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.time):
        if not isinstance(base_url, str) or not base_url.startswith(("https://", "http://")):
            raise ValueError("GitHub API URL must be http(s)")
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport       # tests hand in an httpx.MockTransport
        self._clock = clock

    def __repr__(self) -> str:
        return f"GitHubAPI(base_url={self.base_url!r})"

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": API_VERSION,
                     "User-Agent": USER_AGENT},
            timeout=self._timeout,
            transport=self._transport,
            # A redirect would carry the Authorization header to wherever it
            # points, and proxy env vars would route it through a third party.
            follow_redirects=False,
            trust_env=False,
        )

    def request(self, method: str, path: str, *, bearer: str, json=None, params=None,
                accept: str | None = None, before_effect: bool = False) -> httpx.Response:
        """One call. Returns the 2xx response or raises GitHubError."""
        if not isinstance(bearer, str) or not bearer:
            raise GitHubError(503, "no GitHub credential available")
        unknown = 503 if before_effect else 502
        headers = {"Authorization": f"Bearer {bearer}"}
        if accept:
            headers["Accept"] = accept
        try:
            with self._client() as c:
                resp = c.request(method, path, json=json, params=params, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            log.warning("github api unreachable %s", kv(method=method, error=type(exc).__name__,
                                                        maps_to=503))
            raise GitHubError(503, f"GitHub unreachable ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            log.warning("github api call failed %s", kv(method=method, error=type(exc).__name__,
                                                        maps_to=unknown))
            raise GitHubError(unknown, "GitHub request failed with unknown outcome "
                                       f"({type(exc).__name__})") from exc
        try:
            self._raise_for(resp, unknown)
        except GitHubError as exc:
            status = resp.status_code
            log.log(logging.WARNING if status >= 500 or status in (401, 429) else logging.INFO,
                    "github api refused %s", kv(method=method, github_status=status,
                                                status_class=f"{status // 100}xx",
                                                maps_to=exc.status,
                                                retry_after=getattr(exc, "retry_after", None)))
            raise
        return resp

    def json(self, resp: httpx.Response, *, before_effect: bool = False):
        """The response body as JSON; unreadable is an unknown outcome."""
        try:
            return resp.json()
        except ValueError as exc:
            raise GitHubError(503 if before_effect else 502,
                              "GitHub returned a non-JSON response") from exc

    def _raise_for(self, resp: httpx.Response, unknown: int) -> None:
        status = resp.status_code
        if 200 <= status < 300:
            return
        message = _message(resp)
        if 300 <= status < 400:
            raise GitHubError(404, NOT_FOUND, github_status=status)
        if status == 401:
            raise GitHubError(503, "GitHub rejected the credential", github_status=status)
        retry = _retry_after(resp, message, self._clock())
        if retry is not None:
            raise RateLimited(retry, status)
        if status == 404:
            raise GitHubError(404, NOT_FOUND, github_status=status)
        if status == 403:
            raise GitHubError(403, f"GitHub refused: {message}", github_status=status,
                              github_message=message)
        if status == 422:
            raise GitHubError(400, f"GitHub rejected the request: {message}",
                              github_status=status, github_message=message)
        if 400 <= status < 500:
            raise GitHubError(status, message or f"GitHub answered {status}",
                              github_status=status, github_message=message)
        raise GitHubError(unknown, f"GitHub answered {status}; outcome unknown"
                          if unknown == 502 else f"GitHub answered {status}",
                          github_status=status)


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    msg = body.get("message") if isinstance(body, dict) else None
    return msg[:200] if isinstance(msg, str) else ""


def _retry_after(resp: httpx.Response, message: str, now: float) -> int | None:
    """Seconds to wait when `resp` is a rate-limit refusal, else None."""
    if resp.status_code not in (403, 429):
        return None
    header = resp.headers.get("retry-after", "")
    if header.isdigit():
        return _clamp(int(header))
    if resp.headers.get("x-ratelimit-remaining") == "0":
        reset = resp.headers.get("x-ratelimit-reset", "")
        return _clamp(int(reset) - int(now)) if reset.isdigit() else 60
    if resp.status_code == 429 or "rate limit" in message.lower():
        return 60
    return None


def _clamp(seconds: int) -> int:
    return max(1, min(seconds, MAX_RETRY_AFTER))
