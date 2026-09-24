"""Agent REST surface for targets: list, perform, long-poll, resolve.

`POST /v1/targets/{t}/actions/{a}` is the generic dispatch route; its body is

    {"params": {...}, "as_draft": false, "run_at": null, "delay_seconds": null,
     "note": ""}

(scheduling and draft controls are top-level body fields, never headers, so
one JSON document fully describes the call). Answers: 200 with the plugin's
data (or raw bytes for binary actions, always as a nosniff attachment), 202
`{"status": "pending_approval" | "scheduled", "action_id"}`, or
`{"error", "code", "hint?"}` with 400 / 403 / 404 / 429 / 502 / 503.

The long-poll GET is the only `async def` route (ported from WA_GW
routers/events.py): the wait sits on the event loop and only each short
check hops to a thread, so waiting agents cannot starve the threadpool.
"""

import asyncio
import time

import anyio.to_thread
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import engine
from ..agent_auth import current_auth
from ..auth import AuthContext
from ..config import get_settings
from ..errors import PolicyError
from ..plugins.registry import get_registry
from ..services import agent

router = APIRouter()


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    params: dict = Field(default_factory=dict)
    as_draft: bool = False
    run_at: int | None = None
    delay_seconds: int | None = None
    note: str = Field(default="", max_length=1000)


@router.get("/v1/targets")
def list_targets(auth: AuthContext = Depends(current_auth)) -> dict:
    return agent.list_targets(auth)


@router.post("/v1/targets/{target}/actions/{action}")
def perform(target: str, action: str, body: ActionBody,
            auth: AuthContext = Depends(current_auth)) -> Response:
    r = engine.perform(auth, target, action, body.params, as_draft=body.as_draft,
                       run_at=body.run_at, delay_seconds=body.delay_seconds, note=body.note)
    if r.binary is not None:
        # Target content (a WhatsApp attachment, say) was chosen by a third
        # party: never let a browser sniff it into HTML or render it inline
        # on the broker's origin. The engine's filename is a safe token.
        return Response(r.binary, media_type=r.mime or "application/octet-stream", headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{r.filename or "download"}"'})
    return JSONResponse(r.body, status_code=r.status)


def _query_params(target: str, action: str, request: Request) -> dict:
    """Query string -> action params, typed by the action's schema (integers
    and booleans arrive as text in a URL)."""
    manifest = get_registry().enabled_manifests().get(target)
    act = manifest.action(action) if manifest else None
    props = (act.params.get("properties") or {}) if act else {}
    out = {}
    for name, value in request.query_params.items():
        if name == "wait":
            continue
        kind = (props.get(name) or {}).get("type")
        try:
            if kind == "integer":
                out[name] = int(value)
            elif kind == "boolean":
                out[name] = {"true": True, "false": False}[value.lower()]
            else:
                out[name] = value
        except (ValueError, KeyError) as exc:
            raise PolicyError(400, f"query parameter {name!r} must be {kind}",
                              "invalid_params") from exc
    return out


@router.get("/v1/targets/{target}/actions/{action}")
async def long_poll(target: str, action: str, request: Request, wait: int = 0,
                    auth: AuthContext = Depends(current_auth)) -> dict:
    """For `long_poll` actions: params as query parameters, `wait` seconds
    to hold the request until something new arrives. A call without its
    cursor is a bootstrap and returns at once (engine.is_bootstrap)."""
    params = _query_params(target, action, request)
    poll = await anyio.to_thread.run_sync(engine.open_poll, auth, target, action, params)
    s = get_settings()
    wait = 0 if poll.bootstrap else max(0, min(wait, s.long_poll_max_wait_seconds))
    deadline = time.monotonic() + wait
    while True:
        data = await anyio.to_thread.run_sync(engine.poll_step, auth, poll)
        if not engine.is_empty(data) or time.monotonic() >= deadline:
            await anyio.to_thread.run_sync(engine.close_poll, auth, poll, "ok")
            return data
        await asyncio.sleep(s.long_poll_interval_seconds)


@router.get("/v1/targets/{target}/resolve")
def resolve(target: str, kind: str, q: str = "", limit: int = 20,
            auth: AuthContext = Depends(current_auth)) -> dict:
    return agent.resolve_resource(auth, target, kind, q, limit)
