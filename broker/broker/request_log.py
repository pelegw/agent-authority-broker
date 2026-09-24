"""`RequestContextMiddleware`: a request id and one access line per HTTP request.

Shared by the broker and the plugin runtime: two byte-identical copies
(`broker/broker/request_log.py`, `plugin-runtime/aab_plugin_runtime/
request_log.py`), kept identical by a test, like logging_setup.py. For every
HTTP request it:

  * takes the caller's `X-Request-Id` when it is well formed and does not
    use a prefix reserved for this service's own background jobs, else
    generates one. The broker sends its id to the plugins, so a plugin's
    lines carry the id of the broker request that caused them;
  * runs the request under that id (logging_setup.bind): every line the
    request logs carries it, and the broker records it as the decision
    record's `request_id`;
  * echoes it in the `X-Request-Id` response header;
  * writes one access line (skipping health probes): method, path, status,
    duration, actor, client ip.

The path is the raw path WITHOUT its query string, always. The OAuth
callback's query carries an authorization code and a state nonce, and an
agent's GET query carries params; the query string is never read here.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

from .logging_setup import NO_ID, bind, kv, new_request_id, valid_request_id

HEADER = b"x-request-id"
# Container and orchestrator probes: frequent, and never interesting.
QUIET_PATHS = frozenset({"/health", "/v1/health"})


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def request_path(scope) -> str:
    """The path as sent (percent-encoded), never the query string."""
    raw = scope.get("raw_path")
    if isinstance(raw, bytes):
        return raw.split(b"?", 1)[0].decode("latin-1")
    return quote(str(scope.get("path") or "/").split("?", 1)[0], safe="/")


def client_ip(scope) -> str:
    """The broker's OriginGuard stamps the trusted client ip into the scope
    state; elsewhere it is the socket peer."""
    state = scope.get("state")
    stamped = state.get("client_ip") if isinstance(state, dict) else None
    if stamped:
        return stamped
    client = scope.get("client")
    return client[0] if client else NO_ID


class RequestContextMiddleware:
    """Pure ASGI (no response buffering, streaming untouched)."""

    def __init__(self, app, *, access_logger: str, reserved_prefixes: tuple[str, ...] = ()):
        self.app = app
        self.log = logging.getLogger(access_logger)
        self.reserved = tuple(reserved_prefixes)

    def request_id_for(self, scope) -> str:
        supplied = _header(scope, HEADER)
        if valid_request_id(supplied) and not supplied.startswith(self.reserved):
            return supplied
        return new_request_id()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = self.request_id_for(scope)
        status: int | None = None

        async def send_with_id(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers") or ()
                           if k.lower() != HEADER]
                message = {**message,
                           "headers": [*headers, (HEADER, request_id.encode("ascii"))]}
            await send(message)

        started = time.perf_counter()
        with bind(request_id) as ctx:
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                status = status or 500
                raise
            finally:
                if scope.get("path") not in QUIET_PATHS:
                    self.log.log(
                        logging.WARNING if (status or 0) >= 500 else logging.INFO,
                        "request %s", kv(
                            method=scope.get("method"), path=request_path(scope),
                            status=status, duration_ms=round(
                                (time.perf_counter() - started) * 1000),
                            actor=ctx.actor, ip=client_ip(scope)))
