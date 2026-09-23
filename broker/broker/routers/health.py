"""GET /health and /v1/health: unauthenticated liveness only.

Deliberately minimal: it is exempt from the origin secret (so orchestration
probes work) and therefore reachable by anonymous internet callers, so it must
NOT disclose plugin, connection, or sidecar state. Those belong on the
authenticated admin surface. The version is safe to show and helps confirm
which build is live after a deploy.
"""

from fastapi import APIRouter

from .. import __version__

router = APIRouter()


# Both paths return the same liveness. /health is the conventional root probe
# for load balancers / uptime monitors; /v1/health keeps the API namespace.
@router.get("/health", include_in_schema=False)
@router.get("/v1/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}
