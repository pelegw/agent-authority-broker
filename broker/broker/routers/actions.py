"""Agent REST surface for the key's own queued actions.

Ownership is the authorization: a key sees and cancels only its own rows,
and another key's action id is a plain 404.
"""

from fastapi import APIRouter, Depends

from ..agent_auth import current_auth
from ..auth import AuthContext
from ..services import agent

router = APIRouter()


@router.get("/v1/actions")
def list_actions(status: str | None = None, limit: int = 50, cursor: int | None = None,
                 auth: AuthContext = Depends(current_auth)) -> dict:
    return agent.list_my_actions(auth, status, limit, cursor)


@router.get("/v1/actions/{action_id}")
def get_action(action_id: str, auth: AuthContext = Depends(current_auth)) -> dict:
    return agent.get_action_status(auth, action_id)


@router.delete("/v1/actions/{action_id}")
def cancel_action(action_id: str, auth: AuthContext = Depends(current_auth)) -> dict:
    return agent.cancel_action(auth, action_id)
