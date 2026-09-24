"""Owner routes for the Telegram approval channel (the console's Channels view).

Guarded router-wide by `require_admin` like every admin router (a test walks
the route table). The bot token is write-only: `POST .../token` stores it
encrypted, `DELETE .../token` removes it, and no response ever contains it.
Storing or clearing the token starts or stops the poll loop within a few
seconds (notify/telegram_inbound.supervise), with no restart.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..deps import AdminContext, require_admin
from ..notify import telegram

router = APIRouter(dependencies=[Depends(require_admin)])


class TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=128)


class EnableBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True


@router.get("/v1/admin/telegram")
def status() -> dict:
    return telegram.status()


@router.post("/v1/admin/telegram/token")
def set_token(body: TokenBody, ctx: AdminContext = Depends(require_admin)) -> dict:
    return telegram.set_token(ctx, body.token)


@router.delete("/v1/admin/telegram/token")
def clear_token(ctx: AdminContext = Depends(require_admin)) -> dict:
    return telegram.clear_token(ctx)


@router.post("/v1/admin/telegram/link/start")
def link_start(ctx: AdminContext = Depends(require_admin)) -> dict:
    return telegram.start_linking(ctx)


@router.post("/v1/admin/telegram/enable")
def enable(body: EnableBody | None = None, ctx: AdminContext = Depends(require_admin)) -> dict:
    # A body of {"enabled": false} is accepted too (the WA_GW shape).
    return telegram.set_enabled(ctx, True if body is None else body.enabled)


@router.post("/v1/admin/telegram/disable")
def disable(ctx: AdminContext = Depends(require_admin)) -> dict:
    return telegram.set_enabled(ctx, False)


@router.post("/v1/admin/telegram/test")
def test(ctx: AdminContext = Depends(require_admin)) -> dict:
    return telegram.send_test(ctx)


@router.post("/v1/admin/telegram/unlink")
def unlink(ctx: AdminContext = Depends(require_admin)) -> dict:
    return telegram.unlink(ctx)
