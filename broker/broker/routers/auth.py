"""Pre-login owner endpoints: /auth/status, /auth/setup, /auth/login, /auth/logout.

These are reachable without an owner credential (they are how one is
obtained), so they sit outside `require_admin`. When Cloudflare Access is
enabled they still require the Access identity, like the rest of the admin
plane (deploy/DEPLOY.md puts /auth* behind the Access application too).
`/auth/me` needs a credential and therefore lives in routers/admin.py under
the router-wide guard.
"""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from ..audit import audit
from ..deps import CSRF_HEADER, CSRF_VALUE, client_ip, require_cf_access
from ..errors import PolicyError
from ..identity import principals, ratelimit, sessions, setup

router = APIRouter(prefix="/auth", dependencies=[Depends(require_cf_access)])


class SetupBody(BaseModel):
    # Defaults to empty so a missing token is a clean 401, not a 422.
    setup_token: str = Field(default="", max_length=512)
    username: str = Field(max_length=256)
    password: str = Field(max_length=4096)


class LoginBody(BaseModel):
    username: str = Field(max_length=256)
    password: str = Field(max_length=4096)


@router.get("/status")
def status(request: Request) -> dict:
    """What the console should show: setup page, login page, or the app.

    login_required is true when an owner exists and this request carries no
    live session.
    """
    completed = setup.is_completed()
    has_session = sessions.lookup(request.cookies.get(sessions.COOKIE_NAME)) is not None
    return {"setup_completed": completed, "login_required": completed and not has_session}


@router.post("/setup")
def do_setup(body: SetupBody, request: Request) -> dict:
    owner = setup.run(body.setup_token, body.username, body.password, client_ip(request))
    return {"username": owner.username, "setup_completed": True}


def _loggable_username(username: str) -> str:
    # A password typed into the username box is a classic slip; only record
    # values that could actually be usernames so the audit log never holds one.
    return username if principals.USERNAME_RE.match(username) else "(invalid)"


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    ip = client_ip(request)
    ratelimit.check(ip)
    owner = principals.verify_password(body.username, body.password)
    if owner is None:
        ratelimit.record_failure(ip)
        audit("anonymous", "auth.login_failed",
              detail={"username": _loggable_username(body.username), "ip": ip},
              result="denied")
        raise PolicyError(401, "invalid username or password", "unauthorized")
    value, _ = sessions.create(owner.id, ip, request.headers.get("user-agent", ""))
    sessions.set_cookie(response, value)
    audit(owner.username, "auth.login", detail={"ip": ip},
          actor_principal=owner.id, actor_via="session")
    session = sessions.lookup(value)
    return {"username": owner.username, "expires_at": session.expires_at()}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    value = request.cookies.get(sessions.COOKIE_NAME)
    if value:
        # A cookie-authenticated write: same CSRF rule as the admin plane.
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise PolicyError(403, f"missing {CSRF_HEADER}: {CSRF_VALUE} header", "csrf")
        live = sessions.lookup(value)
        sessions.delete(sessions.hash_cookie(value))
        if live:
            audit(live.username, "auth.logout", detail={"ip": client_ip(request)},
                  actor_principal=live.principal_id, actor_via="session")
    sessions.clear_cookie(response)
    return {"ok": True}
