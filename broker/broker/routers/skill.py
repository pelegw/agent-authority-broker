"""The agent skill doc over HTTP (Markdown, generated from manifests).

  GET /skill, /skill.md   every enabled plugin. Unauthenticated for a local
                          broker (like WA_GW's /skill); in public mode it needs
                          an agent key, so the internet cannot map the surface.
  GET /v1/me/skill        filtered to the calling key: only reachable actions,
                          plus its current capabilities (never hidden lists).

The base URL written into the doc is the one the caller used (Host, and
X-Forwarded-Proto behind the edge), sanitized by skill.generator.base_url_from.
The same text is the MCP resource `broker://skill` (mcp_server.py).
"""

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response

from ..agent_auth import current_auth
from ..auth import AuthContext
from ..config import get_settings
from ..plugins.registry import get_registry
from ..services import agent
from ..skill.generator import base_url_from, render

router = APIRouter()

MARKDOWN = "text/markdown; charset=utf-8"
_DOC = {200: {"content": {"text/markdown": {}}, "description": "the skill doc (Markdown)"}}


def request_base_url(request: Request) -> str:
    return base_url_from(request.headers, request.url.scheme)


def key_if_public(request: Request, authorization: str | None = Header(None)) -> None:
    """Read at request time (not import time), so the setting cannot go stale:
    in public mode the full surface is for key holders only."""
    if get_settings().public_mode():
        current_auth(request, authorization)


@router.get("/skill", include_in_schema=False, dependencies=[Depends(key_if_public)])
@router.get("/skill.md", include_in_schema=False, dependencies=[Depends(key_if_public)])
def skill(request: Request) -> Response:
    text = render(request_base_url(request), get_registry().enabled_manifests())
    return Response(text, media_type=MARKDOWN)


@router.get("/v1/me/skill", responses=_DOC, response_class=Response)
def my_skill(request: Request, auth: AuthContext = Depends(current_auth)) -> Response:
    return Response(agent.skill_doc(auth, request_base_url(request)), media_type=MARKDOWN)
