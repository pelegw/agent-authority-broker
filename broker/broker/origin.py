"""Origin lockdown for public (Cloudflare-fronted) deployment.

When the broker is reachable on a public IP, Cloudflare sits in front but the
origin is still directly addressable by anyone who learns the IP. Two guards:

1. A shared secret header that a Cloudflare Transform Rule injects on every
   proxied request. Requests without it never went through Cloudflare -> 403.
2. The real client IP is taken from CF-Connecting-IP, but ONLY after guard 1
   passes, so a direct-to-origin caller cannot spoof their source IP.

Both are no-ops until `origin_secret` is configured, so local/dev runs and the
test suite run without it.
"""

import logging
import secrets

from starlette.types import ASGIApp, Receive, Scope, Send

from .config import get_settings
from .logging_setup import kv
from .request_log import request_path

log = logging.getLogger(__name__)

# Health is exempt so container/orchestrator probes work without the secret.
_EXEMPT_PATHS = frozenset({"/health", "/v1/health"})


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin1").lower(): v.decode("latin1")
            for k, v in scope.get("headers", [])}


def client_ip(headers: dict[str, str], peer: str | None, trusted: bool) -> str:
    """Best available client IP. Trust CF-Connecting-IP only when the request
    is verified as coming through Cloudflare (origin secret already checked)."""
    s = get_settings()
    if trusted and s.trust_cf_connecting_ip:
        cf = headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
    return peer or ""


class OriginGuardMiddleware:
    """ASGI middleware enforcing the origin secret and stamping trust state.

    Sets scope["state"]["origin_trusted"] and scope["state"]["client_ip"] so
    downstream code (auth, audit) can read the real caller without re-parsing.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        s = get_settings()
        headers = _headers(scope)
        peer = scope["client"][0] if scope.get("client") else None

        trusted = True
        if s.origin_secret:
            supplied = headers.get(s.origin_secret_header.lower(), "")
            # Header values are latin1 str; compare as bytes so a non-ASCII
            # value can't raise TypeError (which would 500 every request).
            trusted = secrets.compare_digest(
                supplied.encode("latin1", "ignore"), s.origin_secret.encode())
            if not trusted and scope.get("path") not in _EXEMPT_PATHS:
                # Outside the request context (it runs first), so this line
                # has no request id; the socket peer is the only ip there is.
                log.warning("request refused: did not come through the trusted edge %s",
                            kv(method=scope.get("method"), path=request_path(scope),
                               ip=peer, header_present=bool(supplied)))
                await _deny(send)
                return

        state = scope.setdefault("state", {})
        state["origin_trusted"] = trusted
        state["client_ip"] = client_ip(headers, peer, trusted)
        await self.app(scope, receive, send)


async def _deny(send: Send) -> None:
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body",
                "body": b'{"error": "request did not originate from the trusted edge"}'})
