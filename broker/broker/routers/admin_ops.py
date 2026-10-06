"""Owner routes for day-to-day operation: queued actions, hidden resources,
resource lookup for pickers, the decision record, and the health summary
an uptime monitor polls.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import decisions, hidden
from ..actions import queue
from ..deps import AdminContext, require_admin
from ..errors import PolicyError
from ..logging_setup import kv
from ..services import admin, plugins_admin, system_health

router = APIRouter(dependencies=[Depends(require_admin)])
log = logging.getLogger(__name__)


# ------------------------------------------------------------ actions

@router.get("/v1/admin/actions")
def list_actions(status: str | None = None, target: str | None = None, limit: int = 100,
                 cursor: int | None = None) -> dict:
    return queue.list_all(status, target, limit, cursor)


@router.get("/v1/admin/actions/{action_id}")
def get_action(action_id: str) -> dict:
    row = queue.get_row(action_id)
    if row is None:
        raise PolicyError(404, "no such action", "not_found")
    return row


@router.post("/v1/admin/actions/{action_id}/{verb}")
def decide_action(action_id: str, verb: Literal["approve", "reject", "cancel"],
                  ctx: AdminContext = Depends(require_admin)) -> dict:
    fn = {"approve": admin.approve_action, "reject": admin.reject_action,
          "cancel": admin.cancel_action}[verb]
    return fn(ctx, action_id)


# ------------------------------------------------------------ hidden resources

class HiddenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=64)
    resource_id: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=500)


@router.get("/v1/admin/hidden")
def list_hidden(target: str | None = None) -> list[dict]:
    return hidden.list_hidden(target)


@router.post("/v1/admin/hidden")
def add_hidden(body: HiddenBody, ctx: AdminContext = Depends(require_admin)) -> dict:
    return admin.add_hidden(ctx, body.target, body.kind, body.resource_id, body.label,
                            body.reason)


@router.delete("/v1/admin/hidden/{target}/{kind}/{resource_id:path}")
def remove_hidden(target: str, kind: str, resource_id: str,
                  ctx: AdminContext = Depends(require_admin)) -> dict:
    return admin.remove_hidden(ctx, target, kind, resource_id)


@router.get("/v1/admin/resolve")
def resolve(target: str, kind: str, q: str = "", limit: int = 20,
            ctx: AdminContext = Depends(require_admin)) -> list[dict]:
    return plugins_admin.resolve(ctx, target, kind, q, limit)


# ------------------------------------------------------------ decision record

@router.get("/v1/admin/decisions")
def list_decisions(key: int | None = None, target: str | None = None,
                   decision: str | None = None, since: int | None = None, limit: int = 50,
                   cursor: int | None = None) -> dict:
    return decisions.list_decisions(key_id=key, target=target, decision=decision,
                                    since=since, limit=limit, cursor=cursor)


@router.get("/v1/admin/decisions/verify")
def verify_decisions(from_id: int | None = None,
                     ctx: AdminContext = Depends(require_admin)) -> dict:
    result = decisions.verify(from_id)
    # A broken chain is the one result an operator must never miss.
    log.log(logging.INFO if result["ok"] else logging.ERROR, "decision chain verified %s", kv(
        ok=result["ok"], checked=result["checked"], first_bad_id=result["first_bad_id"],
        signed=result["signed"], from_id=from_id, by=ctx.username, via=ctx.via))
    return result


# ------------------------------------------------------------ health summary

@router.head("/v1/admin/health", include_in_schema=False)
@router.get("/v1/admin/health")
def health_summary(request: Request) -> Response:
    """200 when every check is ok, 503 when any is not: the status code is
    what a monitor alerts on, the body says which check and why. HEAD runs
    the same checks and answers with the status alone (UptimeRobot and
    other monitors probe with HEAD)."""
    report = system_health.summary()
    code = 200 if report["status"] == "ok" else 503
    if request.method == "HEAD":
        return Response(status_code=code)
    return JSONResponse(report, status_code=code)
