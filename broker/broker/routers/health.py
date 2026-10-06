"""GET /health and /v1/health: unauthenticated liveness only.

Deliberately minimal: it is exempt from the origin secret (so orchestration
probes work) and therefore reachable by anonymous internet callers, so it must
NOT disclose plugin, connection, or sidecar state. Those belong on the
authenticated admin surface: `GET /v1/admin/health` (services/system_health.py)
is the owner's summary for an uptime monitor. The version is safe to show
and helps confirm which build is live after a deploy.
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .. import __version__

router = APIRouter()


# Both paths return the same liveness. /health is the conventional root probe
# for load balancers / uptime monitors; /v1/health keeps the API namespace.
# HEAD is explicit because FastAPI does not derive it from GET, and uptime
# monitors (UptimeRobot among them) probe with HEAD: the status alone, no body.
@router.head("/health", include_in_schema=False)
@router.head("/v1/health", include_in_schema=False)
@router.get("/health", include_in_schema=False)
@router.get("/v1/health")
def health(request: Request) -> Response:
    if request.method == "HEAD":
        return Response(status_code=200)
    return JSONResponse({"status": "ok", "version": __version__})
