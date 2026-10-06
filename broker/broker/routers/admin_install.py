"""Owner routes that install, upgrade and remove external plugins through the installer.

Guarded router-wide by `require_admin` like every admin router (a test walks
main.ADMIN_ROUTERS). The work and the rules are in services/plugin_install.py;
the installer itself is a separate container (docker-compose.installer.yml).

  GET  /v1/admin/plugins/install/status        configured and reachable? plus the hints
  POST /v1/admin/plugins/install/inspect       {source, ref} -> the review card
  POST /v1/admin/plugins/install               {source, ref, commit} -> pins, then a job
  GET  /v1/admin/plugins/install/jobs/{id}     the job, polled through the broker's restart
  GET  /v1/admin/plugins/installed             {"items": [install record]}
  POST /v1/admin/plugins/{service}/upgrade     {source, ref, commit} -> re-pins, then a job
  POST /v1/admin/plugins/{service}/remove      {purge} -> a job, then unpins

This router is included BEFORE admin_plugins (main.ADMIN_ROUTERS): routes
match in order, and `GET /v1/admin/plugins/installed` must not be taken for
the view of a plugin named "installed" (a reserved id anyway:
plugins_admin.RESERVED_IDS).
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..deps import AdminContext, require_admin
from ..services import plugin_install

router = APIRouter(dependencies=[Depends(require_admin)])


class _Body(BaseModel):
    # Unknown fields are refused: a misspelt `comit` must not install HEAD.
    model_config = ConfigDict(extra="forbid")


class InspectBody(_Body):
    source: str = Field(min_length=1, max_length=300)
    ref: str = Field(min_length=1, max_length=64)


class InstallBody(InspectBody):
    commit: str = Field(min_length=40, max_length=40)


class RemoveBody(_Body):
    purge: bool = False


@router.get("/v1/admin/plugins/install/status")
def status() -> dict:
    return plugin_install.status()


@router.post("/v1/admin/plugins/install/inspect")
def inspect(body: InspectBody) -> dict:
    return plugin_install.inspect(body.source, body.ref)


@router.post("/v1/admin/plugins/install", status_code=202)
def install(body: InstallBody, ctx: AdminContext = Depends(require_admin)) -> JSONResponse:
    out = plugin_install.install(ctx, body.source, body.ref, body.commit)
    return JSONResponse(out, status_code=202)


@router.get("/v1/admin/plugins/install/jobs/{job_id}")
def job(job_id: str) -> dict:
    return plugin_install.job(job_id)


@router.get("/v1/admin/plugins/installed")
def installed() -> dict:
    return plugin_install.installed()


@router.post("/v1/admin/plugins/{service}/upgrade", status_code=202)
def upgrade(service: str, body: InstallBody,
            ctx: AdminContext = Depends(require_admin)) -> JSONResponse:
    out = plugin_install.upgrade(ctx, service, body.source, body.ref, body.commit)
    return JSONResponse(out, status_code=202)


@router.post("/v1/admin/plugins/{service}/remove", status_code=202)
def remove(service: str, body: RemoveBody | None = None,
           ctx: AdminContext = Depends(require_admin)) -> JSONResponse:
    out = plugin_install.remove(ctx, service, bool(body and body.purge))
    return JSONResponse(out, status_code=202)
