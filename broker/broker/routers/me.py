"""Agent self-introspection: `/v1/me` and the key-filtered OpenAPI document.

`/v1/me` is `get_my_access`: capabilities per target with `enforced_where`,
remaining budgets, the key's ceiling (role) with `effective_mode` wherever it
lowers an action, expiries and delegation depth. It never lists hidden
resources or deny sets.
"""

from fastapi import APIRouter, Depends, Request

from .. import openapi_doc
from ..agent_auth import current_auth
from ..auth import AuthContext
from ..plugins.registry import get_registry
from ..services import agent

router = APIRouter()


@router.get("/v1/me")
def me(auth: AuthContext = Depends(current_auth)) -> dict:
    return agent.get_my_access(auth)


@router.get("/v1/me/openapi.json")
def my_openapi(request: Request, auth: AuthContext = Depends(current_auth)) -> dict:
    """Only the actions this key can reach, and only agent paths."""
    reachable = {t["id"]: set(t["actions"]) for t in agent.list_targets(auth)["items"]}
    return openapi_doc.build(request.app, get_registry().enabled_manifests(),
                             only=reachable, agent_only=True)
