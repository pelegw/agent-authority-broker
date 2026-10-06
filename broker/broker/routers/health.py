"""GET and HEAD /health and /v1/health: liveness for anyone, the health
summary for a monitor token.

Without a credential the answer is deliberately minimal: these paths are
exempt from the origin secret (so orchestration probes work) and therefore
reachable by anonymous internet callers, so they must NOT disclose plugin,
connection or sidecar state. The version is safe to show and helps confirm
which build is live after a deploy.

With a monitor token (`aab_monitor_...`, minted by the owner with scope
`monitor`) the same paths answer the full summary of services/system_health.py:
200 ok or 503 degraded. The token is accepted as a Bearer token or as the
password of HTTP Basic auth, because uptime monitors on free plans can send
Basic auth but not a custom header. It works nowhere else, and an admin token
or a session works nowhere here, so the admin plane stays behind Cloudflare
Access even though this path is outside it. The owner's own view of the same
summary is GET /v1/admin/health. HEAD is explicit because FastAPI does not
derive it from GET, and monitors probe with HEAD: the status alone, no body.
"""

import base64
import binascii
import logging

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from .. import __version__
from ..agent_auth import client_ip
from ..errors import PolicyError
from ..identity import admin_tokens, ratelimit
from ..logging_setup import kv, set_actor
from ..services import system_health

router = APIRouter()
log = logging.getLogger(__name__)


def _unauthorized(request: Request, reason: str) -> PolicyError:
    # One message for every failure; the reason class is for the operator's log.
    log.warning("monitor authentication failed %s",
                kv(reason=reason, path=request.url.path, ip=client_ip(request)))
    return PolicyError(401, "monitor token required", "unauthorized")


def _presented(authorization: str) -> str | None:
    """The credential in an Authorization header: `Bearer <token>`, or
    `Basic base64(<user>:<password>)` with the token as the password (or as
    the user, for a monitor with a single credential field)."""
    scheme, _, rest = authorization.partition(" ")
    rest = rest.strip()
    if scheme == "Bearer":
        return rest or None
    if scheme == "Basic":
        try:
            user, _, password = base64.b64decode(rest, validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        for candidate in (password, user):
            if candidate.startswith(admin_tokens.MONITOR_PREFIX):
                return candidate
        return password or user or None
    return None


def _monitor(request: Request, authorization: str) -> admin_tokens.TokenAuth:
    ip = client_ip(request)
    # The same per-IP throttle as the login and the admin plane: this path is
    # outside Cloudflare Access and the origin secret, so guesses are cheap to
    # make and must be expensive to repeat.
    ratelimit.check(ip)
    token = _presented(authorization)
    auth = admin_tokens.authenticate(token, scope="monitor") if token else None
    if auth is None:
        ratelimit.record_failure(ip)
        if token is None:
            reason = "malformed"
        elif not token.startswith(admin_tokens.MONITOR_PREFIX):
            reason = "not_a_monitor_token"     # an admin token, an agent key, noise
        else:
            reason = "unknown_or_revoked_token"
        raise _unauthorized(request, reason)
    set_actor(f"monitor:{auth.name}")
    return auth


@router.head("/health", include_in_schema=False)
@router.head("/v1/health", include_in_schema=False)
@router.get("/health", include_in_schema=False)
@router.get("/v1/health")
def health(request: Request, authorization: str | None = Header(None)) -> Response:
    head = request.method == "HEAD"
    if authorization is None:
        if head:
            return Response(status_code=200)
        return JSONResponse({"status": "ok", "version": __version__})
    _monitor(request, authorization)
    report = system_health.summary()
    code = 200 if report["status"] == "ok" else 503
    if head:
        return Response(status_code=code)
    return JSONResponse(report, status_code=code)
