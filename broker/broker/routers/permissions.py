"""Agent REST surface for scope expansion: request, status, list.

A request is clipped against what the key's parent could give; anything
beyond that is a 400 carrying `clipped` and `allowed`, so a pending request
is always grantable exactly as asked.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..agent_auth import current_auth
from ..auth import AuthContext
from ..services import agent

router = APIRouter()


class PermissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capabilities: list[dict] = Field(min_length=1, max_length=50)
    reason: str = Field(default="", max_length=1000)
    expires_in_hours: int | None = None


@router.post("/v1/permissions", status_code=202)
def request_permission(body: PermissionBody, auth: AuthContext = Depends(current_auth)) -> dict:
    return agent.request_permission(auth, body.capabilities, body.reason, body.expires_in_hours)


@router.get("/v1/permissions")
def list_permissions(limit: int = 50, cursor: int | None = None,
                     auth: AuthContext = Depends(current_auth)) -> dict:
    return agent.list_my_permissions(auth, limit, cursor)


@router.get("/v1/permissions/{grant_id}")
def get_permission(grant_id: str, auth: AuthContext = Depends(current_auth)) -> dict:
    return agent.get_permission_status(auth, grant_id)
