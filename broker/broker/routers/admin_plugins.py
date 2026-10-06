"""Owner routes for plugins: list, config, enable/disable, health, connect, pins.

Guarded router-wide by `require_admin` like every admin router (a test walks
the route table). `{plugin}` in the connect routes accepts a plugin id or a
service name, because one consent (Google) covers every plugin a service
hosts. The pin routes approve (or withdraw) the manifest of a plugin that is
not vendored in the broker tree; `offered` lists what awaits that review.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from ..deps import AdminContext, require_admin
from ..services import plugins_admin

router = APIRouter(dependencies=[Depends(require_admin)])


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict = Field(default_factory=dict)


class FinishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str | None = Field(default=None, max_length=4096)
    state: str | None = Field(default=None, max_length=512)
    installation_id: str | None = Field(default=None, max_length=64)


@router.get("/v1/admin/plugins")
def list_plugins() -> dict:
    return plugins_admin.list_plugins()


# Declared before /v1/admin/plugins/{plugin}: routes match in order, and
# `offered` is a reserved word no plugin id may take (plugins_admin.RESERVED_IDS).
@router.get("/v1/admin/plugins/offered")
def offered() -> dict:
    return plugins_admin.offered()


@router.post("/v1/admin/plugins/{plugin}/pin")
def pin(plugin: str, ctx: AdminContext = Depends(require_admin)) -> dict:
    return plugins_admin.pin(ctx, plugin)


@router.delete("/v1/admin/plugins/{plugin}/pin")
def unpin(plugin: str, ctx: AdminContext = Depends(require_admin)) -> dict:
    return plugins_admin.unpin(ctx, plugin)


@router.get("/v1/admin/plugins/{plugin}")
def get_plugin(plugin: str) -> dict:
    return plugins_admin.view(plugin)


@router.patch("/v1/admin/plugins/{plugin}")
def patch_plugin(plugin: str, body: ConfigBody,
                 ctx: AdminContext = Depends(require_admin)) -> dict:
    return plugins_admin.patch_config(ctx, plugin, body.config)


@router.post("/v1/admin/plugins/{plugin}/enable")
def enable(plugin: str, ctx: AdminContext = Depends(require_admin)) -> dict:
    return plugins_admin.enable(ctx, plugin)


@router.post("/v1/admin/plugins/{plugin}/disable")
def disable(plugin: str, ctx: AdminContext = Depends(require_admin)) -> dict:
    return plugins_admin.disable(ctx, plugin)


@router.post("/v1/admin/plugins/{plugin}/health")
def health(plugin: str, ctx: AdminContext = Depends(require_admin)) -> dict:
    return plugins_admin.health(ctx, plugin)


@router.post("/v1/admin/plugins/{plugin}/connect/start")
def connect_start(plugin: str, request: Request,
                  ctx: AdminContext = Depends(require_admin)) -> dict:
    # The Host header matters only in local mode (the OAuth redirect URI);
    # public mode builds it from SITE_DOMAIN.
    return plugins_admin.connect_start(ctx, plugin, request.headers.get("host", ""))


@router.post("/v1/admin/plugins/{plugin}/connect/finish")
def connect_finish(plugin: str, body: FinishBody,
                   ctx: AdminContext = Depends(require_admin)) -> dict:
    return plugins_admin.connect_finish(ctx, plugin, body.code, body.state,
                                        body.installation_id)


@router.get("/v1/admin/plugins/{plugin}/connect/qr.png")
def connect_qr(plugin: str, ctx: AdminContext = Depends(require_admin)) -> Response:
    # The QR is a pairing secret while it is valid: never cache it.
    return Response(plugins_admin.connect_qr(ctx, plugin), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/v1/admin/plugins/{plugin}/disconnect")
def disconnect(plugin: str, ctx: AdminContext = Depends(require_admin)) -> dict:
    return plugins_admin.disconnect(ctx, plugin)
