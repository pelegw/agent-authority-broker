"""OpenAPI generated from the enabled plugins' manifests, per request.

The static schema FastAPI derives from the routers has one opaque
`POST /v1/targets/{target}/actions/{action}` route. This module replaces it
with one path per enabled action, whose request body carries that action's
own params schema (manifest params are a JSON-schema subset, so they drop
straight in) and whose summary is the manifest's doc. `/v1/me/openapi.json`
passes `only=` the caller's reachable actions and `agent_only=True`, so an
agent loads exactly what it may use and nothing of the admin plane.

Manifests are data: nothing here names a plugin.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping

from fastapi.openapi.utils import get_openapi

from .plugins.manifest import Manifest

GENERIC = "/v1/targets/{target}/actions/{action}"
AGENT_PREFIXES = ("/v1/targets", "/v1/me", "/v1/actions", "/v1/permissions",
                  "/v1/delegations")
_ERROR = {"description": "refused",
          "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}


def _body_schema(act) -> dict:
    props: dict = {"params": copy.deepcopy(act.params),
                   "note": {"type": "string", "maxLength": 1000,
                            "description": "Rationale shown to the human approver."}}
    if "draft" in act.effective_modes and act.side_effect != "read":
        props["as_draft"] = {"type": "boolean", "default": False,
                             "description": "Queue for human approval instead of acting."}
    if act.schedulable:
        props["run_at"] = {"type": "integer", "description": "Unix time to act at."}
        props["delay_seconds"] = {"type": "integer", "description": "Act after this delay."}
    required = ["params"] if act.params.get("required") else []
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def _operation(pid: str, m: Manifest, act) -> dict:
    ok = ({"description": "binary content",
           "content": {"application/octet-stream": {"schema": {"type": "string",
                                                               "format": "binary"}}}}
          if act.returns == "binary" else {"description": "the plugin's data",
                                           "content": {"application/json": {"schema": {}}}})
    responses = {"200": ok, "400": _ERROR, "403": _ERROR, "404": _ERROR, "429": _ERROR,
                 "502": _ERROR, "503": _ERROR}
    if act.side_effect != "read":
        responses["202"] = {"description": "queued: pending_approval or scheduled",
                            "content": {"application/json": {"schema": {
                                "type": "object",
                                "properties": {"status": {"type": "string",
                                                          "enum": ["pending_approval",
                                                                   "scheduled"]},
                                               "action_id": {"type": "string"}}}}}}
    return {"tags": [pid], "operationId": f"{pid}_{act.name}",
            "summary": act.doc or act.name, "x-side-effect": act.side_effect,
            "requestBody": {"required": True,
                            "content": {"application/json": {"schema": _body_schema(act)}}},
            "responses": responses, "security": [{"bearer": []}]}


def _long_poll_operation(pid: str, act) -> dict:
    params = [{"name": name, "in": "query", "required": False,
               "schema": copy.deepcopy(spec)}
              for name, spec in (act.params.get("properties") or {}).items()]
    params.append({"name": "wait", "in": "query", "required": False,
                   "schema": {"type": "integer", "minimum": 0},
                   "description": "Seconds to wait for something new."})
    return {"tags": [pid], "operationId": f"{pid}_{act.name}_poll",
            "summary": f"{act.doc or act.name} (long-poll)", "parameters": params,
            "responses": {"200": {"description": "new items, or empty after the wait"},
                          "403": _ERROR, "404": _ERROR}, "security": [{"bearer": []}]}


def build(app, manifests: Mapping[str, Manifest],
          only: Mapping[str, set[str]] | None = None, agent_only: bool = False) -> dict:
    doc = get_openapi(title=app.title, version=app.version, routes=app.routes)
    paths = doc.setdefault("paths", {})
    paths.pop(GENERIC, None)
    for pid, m in sorted(manifests.items()):
        for act in m.actions:
            if only is not None and act.name not in only.get(pid, set()):
                continue
            entry = {"post": _operation(pid, m, act)}
            if act.long_poll:
                entry["get"] = _long_poll_operation(pid, act)
            paths[f"/v1/targets/{pid}/actions/{act.name}"] = entry
    if agent_only:
        doc["paths"] = {p: v for p, v in paths.items() if p.startswith(AGENT_PREFIXES)}
    comps = doc.setdefault("components", {})
    comps.setdefault("schemas", {})["Error"] = {
        "type": "object", "required": ["error", "code"],
        "properties": {"error": {"type": "string"}, "code": {"type": "string"},
                       "hint": {"type": "string"}}}
    comps.setdefault("securitySchemes", {})["bearer"] = {
        "type": "http", "scheme": "bearer", "description": "aab_ agent key"}
    return doc
