"""Owner routes for agent keys and grants.

Creating a key creates its active root grant in the same flow and returns
the key's plaintext exactly once. Grant decisions are atomic status moves
recording the owner's username and surface.
"""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..deps import AdminContext, require_admin
from ..services import admin

router = APIRouter(dependencies=[Depends(require_admin)])


class KeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    role: str = "read-only"
    rate_per_min: int = Field(default=6, ge=1, le=10000)
    expires_at: int | None = None
    capabilities: list[dict] = Field(default_factory=list, max_length=100)
    denies: dict | None = None


class KeyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str | None = None
    rate_per_min: int | None = Field(default=None, ge=1, le=10000)
    expires_at: int | None = None
    clear_expiry: bool = False
    denies: dict | None = None
    disabled: bool | None = None
    capabilities: list[dict] | None = Field(default=None, max_length=100)


@router.post("/v1/admin/keys")
def create_key(body: KeyCreate, ctx: AdminContext = Depends(require_admin)) -> dict:
    return admin.create_key(ctx, body.name, body.role, body.rate_per_min, body.expires_at,
                            body.capabilities, body.denies)


@router.get("/v1/admin/keys")
def list_keys() -> list[dict]:
    return admin.list_keys()


@router.get("/v1/admin/keys/{key_id}")
def get_key(key_id: int) -> dict:
    return admin.get_key(key_id)


@router.patch("/v1/admin/keys/{key_id}")
def update_key(key_id: int, body: KeyPatch, ctx: AdminContext = Depends(require_admin)) -> dict:
    return admin.update_key(ctx, key_id, role=body.role, rate_per_min=body.rate_per_min,
                            expires_at=body.expires_at, clear_expiry=body.clear_expiry,
                            denies=body.denies, disabled=body.disabled,
                            capabilities=body.capabilities)


@router.post("/v1/admin/keys/{key_id}/rotate")
def rotate_key(key_id: int, ctx: AdminContext = Depends(require_admin)) -> dict:
    return admin.rotate_key(ctx, key_id)


@router.get("/v1/admin/grants")
def list_grants(status: str | None = None, key_id: int | None = None,
                limit: int = 100) -> list[dict]:
    return admin.list_grants(status, key_id, limit)


_VERBS = {"approve": "active", "reject": "rejected", "revoke": "revoked"}


@router.post("/v1/admin/grants/{grant_id}/{verb}")
def decide_grant(grant_id: str, verb: Literal["approve", "reject", "revoke"],
                 ctx: AdminContext = Depends(require_admin)) -> dict:
    return admin.decide_grant(ctx, grant_id, _VERBS[verb])
