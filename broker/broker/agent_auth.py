"""Agent-key authentication for the agent surface (`Bearer aab_...`).

Separate from deps.py on purpose: deps.py carries the owner's
`require_admin` and imports identity/, and the agent routers must not have
identity/ (or services/admin.py) anywhere in their import graph. A test
walks that graph, so "an agent cannot reach approvals" is structural.
"""

from fastapi import Header, Request

from . import auth as _auth
from .errors import PolicyError


def client_ip(request: Request) -> str:
    """Real client IP, as stamped by OriginGuardMiddleware (CF-Connecting-IP
    when the request is trusted, else the socket peer)."""
    ip = request.scope.get("state", {}).get("client_ip")
    if ip:
        return ip
    return request.client.host if request.client else ""


def authenticate(authorization: str | None, ip: str = "") -> "_auth.AuthContext":
    """The agent key behind an Authorization header, or a 401 PolicyError.
    Admin tokens are not agent keys and are refused even when valid. Shared
    by the REST dependency below and the MCP auth middleware, so both
    surfaces accept exactly the same credentials with the same error."""
    ctx = _auth.authenticate_bearer(authorization, ip)
    if ctx is None:
        raise PolicyError(401, "missing or invalid API key (Authorization: Bearer aab_...)",
                          "unauthorized")
    return ctx


def current_auth(request: Request,
                 authorization: str | None = Header(None)) -> "_auth.AuthContext":
    """FastAPI dependency: the calling agent key, or 401."""
    return authenticate(authorization, client_ip(request))
