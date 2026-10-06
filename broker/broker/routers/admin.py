"""The owner's management plane: account, admin tokens, sessions.

Every route here is guarded by the router-level `require_admin` dependency, so
no admin route can forget authentication (a test walks the route table to
prove it). Handlers that need to know who is acting also declare
`ctx: AdminContext = Depends(require_admin)`; FastAPI caches the dependency
per request, so the guard still runs only once.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..audit import audit
from ..deps import AdminContext, client_ip, require_admin
from ..errors import PolicyError
from ..identity import admin_tokens, principals, ratelimit, sessions
from ..logging_setup import kv

router = APIRouter(dependencies=[Depends(require_admin)])
log = logging.getLogger(__name__)


def _audit(ctx: AdminContext, action: str, resource: str = "",
           detail: dict | None = None, result: str = "ok") -> None:
    audit(ctx.username, action, resource, detail, result,
          actor_principal=ctx.principal_id, actor_via=ctx.via)


# ------------------------------------------------------------ account

@router.get("/auth/me")
def me(ctx: AdminContext = Depends(require_admin)) -> dict:
    return {"username": ctx.username, "principal_id": ctx.principal_id,
            "via": ctx.via, "expires_at": ctx.expires_at}


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=4096)
    new_password: str = Field(max_length=4096)


@router.post("/v1/admin/password")
def change_password(body: PasswordBody, request: Request,
                    ctx: AdminContext = Depends(require_admin)) -> dict:
    """Change the owner password; every OTHER session is logged out.

    The current password is required even with a valid credential, so a
    hijacked session (or a borrowed unlocked laptop) cannot lock the owner out.
    Wrong guesses feed the same per-IP limiter as login.
    """
    ip = client_ip(request)
    ratelimit.check(ip)
    if not principals.check_password(ctx.principal_id, body.current_password):
        ratelimit.record_failure(ip)
        _audit(ctx, "auth.password_change", detail={"ip": ip}, result="denied")
        log.warning("owner password change refused: wrong current password %s",
                    kv(username=ctx.username, via=ctx.via, ip=ip))
        raise PolicyError(403, "current password is incorrect", "wrong_password")
    principals.set_password(ctx.principal_id, body.new_password)
    # A token-authenticated change has no session to keep: log out every session.
    keep = ctx.credential_id if ctx.via == "session" else None
    revoked = sessions.revoke_all_except(ctx.principal_id, keep)
    _audit(ctx, "auth.password_change", detail={"ip": ip, "sessions_revoked": revoked})
    log.info("owner password changed %s",
             kv(username=ctx.username, via=ctx.via, sessions_revoked=revoked, ip=ip))
    return {"ok": True, "sessions_revoked": revoked}


# ------------------------------------------------------------ admin tokens

class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    expires_in_hours: int | None = Field(default=None, ge=1, le=24 * 3650)
    # admin: the CLI, scripts and deploys. monitor: /health and /v1/health
    # only, where it turns the liveness probe into the full health summary.
    scope: Literal["admin", "monitor"] = "admin"


@router.post("/v1/admin/tokens")
def create_token(body: TokenBody, ctx: AdminContext = Depends(require_admin)) -> dict:
    """Mint an admin or monitor token. The plaintext is in this response and
    nowhere else."""
    created = admin_tokens.create(ctx.principal_id, body.name, body.expires_in_hours,
                                  body.scope)
    _audit(ctx, "admin_token.create", created["id"],
           {"name": body.name, "scope": body.scope, "expires_at": created["expires_at"]})
    # The id, name and scope only: the plaintext exists in the response alone.
    log.info("admin token created %s", kv(token_id=created["id"], name=body.name,
                                          scope=body.scope,
                                          expires_at=created["expires_at"],
                                          by=ctx.username, via=ctx.via))
    return created


@router.get("/v1/admin/tokens")
def list_tokens(ctx: AdminContext = Depends(require_admin)) -> list[dict]:
    return admin_tokens.list_for(ctx.principal_id)


@router.post("/v1/admin/tokens/{token_id}/revoke")
def revoke_token(token_id: str, ctx: AdminContext = Depends(require_admin)) -> dict:
    if not admin_tokens.revoke(ctx.principal_id, token_id):
        raise PolicyError(404, "no such token")
    _audit(ctx, "admin_token.revoke", token_id)
    log.info("admin token revoked %s", kv(token_id=token_id, by=ctx.username, via=ctx.via))
    return {"ok": True}


# ------------------------------------------------------------ sessions

@router.get("/v1/admin/sessions")
def list_sessions(ctx: AdminContext = Depends(require_admin)) -> list[dict]:
    current = ctx.credential_id if ctx.via == "session" else None
    return sessions.list_for(ctx.principal_id, current)


@router.post("/v1/admin/sessions/{session_id}/revoke")
def revoke_session(session_id: str, ctx: AdminContext = Depends(require_admin)) -> dict:
    if not sessions.revoke(ctx.principal_id, session_id):
        raise PolicyError(404, "no such session")
    _audit(ctx, "session.revoke", session_id)
    # A prefix of the stored id (a hash, not the cookie) is enough to match
    # the console's session list.
    log.info("owner session revoked %s", kv(session=session_id[:12], by=ctx.username,
                                            via=ctx.via))
    return {"ok": True}
