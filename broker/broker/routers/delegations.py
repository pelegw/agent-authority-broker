"""Agent REST surface for delegation: mint, list and revoke child keys.

`POST /v1/delegations` returns the child's plaintext key exactly once, so
the response is marked `Cache-Control: no-store`. No human approves a
delegation: everything it creates is carved out of the caller's own
authority (services/delegation.py), and the owner sees every delegated key
in the key tree and can revoke it.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..agent_auth import current_auth
from ..auth import AuthContext
from ..services import delegation
from ..services.delegation import MAX_HOURS

router = APIRouter()


class DelegateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=40,
                      description="Child key name; stored as '<your name>/<name>'.")
    capabilities: list[dict] = Field(min_length=1, max_length=50)
    reason: str = Field(default="", max_length=1000)
    expires_in_hours: int | None = Field(default=None, ge=1, le=MAX_HOURS)
    role: str | None = Field(default=None, max_length=20)
    rate_per_min: int | None = Field(default=None, ge=1, le=10000)
    denies: dict | None = None


@router.post("/v1/delegations", status_code=201)
def delegate(body: DelegateBody, auth: AuthContext = Depends(current_auth)) -> JSONResponse:
    out = delegation.delegate(auth, body.name, body.capabilities, body.reason,
                              body.expires_in_hours, body.role, body.rate_per_min, body.denies)
    # The body carries a secret: no cache on the way may keep it.
    return JSONResponse(out, status_code=201, headers={"Cache-Control": "no-store"})


@router.get("/v1/delegations")
def list_delegations(auth: AuthContext = Depends(current_auth)) -> dict:
    return delegation.list_my_delegations(auth)


@router.post("/v1/delegations/{key_id}/revoke")
def revoke_delegation(key_id: int, auth: AuthContext = Depends(current_auth)) -> dict:
    return delegation.revoke_delegation(auth, key_id)
