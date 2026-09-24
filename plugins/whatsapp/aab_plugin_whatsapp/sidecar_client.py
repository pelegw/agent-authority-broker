"""HTTP client for the Go sidecar's internal API (the only path to WhatsApp actions).

Ported from WA_GW `gateway/app/sidecar.py`. The sidecar is reachable only on
the `wa_internal` network and requires the shared `X-Internal-Token` on
every route but `/health`. It holds no policy: every check happens in this
plugin (and in the broker) before a request is sent.

The transport contract is WA_GW's, and it is what the broker's queue relies
on (docs/plugin-api.md, "Errors"):
  * never reached the sidecar (connection refused, connect timeout)
      -> 503: definitely not performed, safe to retry;
  * anything once the request is on the wire (read timeout, reset, a broken
    or unexpected response) -> 502: the message may have been sent, so it is
    never retried automatically.
HTTP answers from the sidecar map onto the same contract (`_raise_for`).

`SidecarError` is an `AdapterError`, so the runtime answers the broker with
exactly the status decided here. The token is a request header only: it is
never part of an error message, a log line or this object's repr.

Every call carries the broker's request id (X-Request-Id), which the
sidecar puts on its own request log line, and logs one line here: method,
path (never the query: /media's names a chat and a message), status,
duration. Never a message text.
"""

import json
import logging
import time

import httpx

from aab_plugin_runtime import AdapterError
from aab_plugin_runtime.logging_setup import current_request_id, kv

log = logging.getLogger("aab_plugin_whatsapp.sidecar")

# Below the broker's 30 s plugin timeout (PLUGIN_TIMEOUT_SECONDS): if the
# sidecar hangs, this plugin reports its own clean 502 before the broker
# gives up on the plugin and has to guess.
DEFAULT_TIMEOUT_SECONDS = 25.0
# The sidecar's /send reads at most this much body (http.MaxBytesReader).
MAX_SEND_BODY_BYTES = 64 * 1024


class SidecarError(AdapterError):
    """The sidecar refused or failed; `.status` follows the 503/502 contract."""


class SidecarClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 transport: httpx.BaseTransport | None = None):
        if not isinstance(token, str) or not token.strip():
            # An empty token would make every call a 401; refuse at boot.
            raise ValueError("SIDECAR_TOKEN is empty; refusing to start")
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ValueError("SIDECAR_URL must be an http(s) URL")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport       # tests hand in an httpx.MockTransport

    def __repr__(self) -> str:            # the token must never appear here
        return f"SidecarClient(base_url={self.base_url!r})"

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"X-Internal-Token": self._token},
            timeout=self._timeout,
            transport=self._transport,
            # A redirect would carry X-Internal-Token to wherever it points,
            # and proxy env vars would route it through a third party.
            follow_redirects=False,
            trust_env=False,
        )

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One sidecar call; network-level failures become SidecarError(503|502)
        so callers uniformly see the contract instead of a raw 500."""
        rid = current_request_id()
        if rid:
            kwargs["headers"] = {**(kwargs.get("headers") or {}), "X-Request-Id": rid}
        started = time.perf_counter()
        try:
            with self._client() as c:
                resp = c.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            _log_call(method, path, None, started, type(e).__name__)
            # Never reached the sidecar -> definitely not delivered -> retryable.
            raise SidecarError(503, f"sidecar unreachable ({type(e).__name__})") from e
        except httpx.HTTPError as e:
            _log_call(method, path, None, started, type(e).__name__)
            # Request was already on the wire (e.g. read timeout): the send may have
            # gone through. Surface as 502 so a queued action is NOT auto-retried —
            # re-sending could double-send. The human investigates.
            raise SidecarError(502, "sidecar request failed with unknown outcome "
                                    f"({type(e).__name__})") from e
        _log_call(method, path, resp.status_code, started)
        _raise_for(resp)
        return resp

    def status(self) -> dict:
        body = _json(self._request("GET", "/status"))
        if not isinstance(body, dict):
            raise SidecarError(502, "sidecar returned a malformed status")
        return body

    def qr_png(self) -> bytes:
        return self._request("GET", "/qr").content

    def send_text(self, to: str, text: str) -> dict:
        """Returns {"message_id": ..., "ts": ...} on success."""
        # Encode here so the size is known exactly: the sidecar reads at most
        # 64 KiB of body, and an oversized one would come back as a confusing
        # "invalid JSON". Refused before the network, so plainly a 400.
        payload = json.dumps({"to": to, "text": text}, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_SEND_BODY_BYTES:
            raise SidecarError(400, "message too long for WhatsApp (64 KiB encoded)")
        body = _json(self._request("POST", "/send", content=payload,
                                   headers={"Content-Type": "application/json"}))
        if not isinstance(body, dict) or not isinstance(body.get("message_id"), str):
            # A 2xx we cannot read: the message was probably sent. Unknown outcome.
            raise SidecarError(502, "sidecar returned a malformed send result")
        return {"message_id": body["message_id"], "ts": body.get("ts")}

    def media(self, chat_jid: str, message_id: str) -> tuple[bytes, str]:
        """Returns (bytes, content_type)."""
        resp = self._request("GET", "/media", params={"chat_jid": chat_jid,
                                                      "message_id": message_id})
        return resp.content, resp.headers.get("content-type", "application/octet-stream")


def _log_call(method: str, path: str, status: int | None, started: float,
              error: str | None = None) -> None:
    ok = status is not None and 200 <= status < 300
    log.log(logging.INFO if ok else logging.WARNING, "sidecar call %s", kv(
        method=method, path=path, status=status, error=error,
        duration_ms=round((time.perf_counter() - started) * 1000)))


def _json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError as e:
        raise SidecarError(502, "sidecar returned a non-JSON response") from e


def _raise_for(resp: httpx.Response) -> None:
    """Map a sidecar HTTP answer onto the contract. WA_GW passed the status
    through as-is; three cases are narrowed here because they matter to the
    broker's retry logic:
      401/403  the token was refused before any handler ran: not performed
               (503), and never passed on as if the *agent* were unauthorized;
      3xx      the sidecar never redirects, so this is not an answer we can
               trust (502), and the redirect is not followed;
      other 5xx (500, 504, ...) nothing promises "not performed" (502).
    Other 4xx pass through (400 bad input, 404 no such message/media, 409
    already paired), as do the contract statuses 502 and 503."""
    status = resp.status_code
    if 200 <= status < 300:
        return
    try:
        msg = resp.json().get("error", "")
    except (ValueError, AttributeError):
        msg = ""
    msg = str(msg or f"sidecar answered {status}")[:300]
    if status in (401, 403):
        raise SidecarError(503, "sidecar refused the internal token (check SIDECAR_TOKEN)")
    if 300 <= status < 400 or status < 200:
        raise SidecarError(502, f"sidecar answered an unexpected {status}")
    if 400 <= status < 500 or status in (502, 503):
        raise SidecarError(status, msg)
    raise SidecarError(502, msg)
