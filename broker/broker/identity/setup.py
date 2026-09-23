"""One-time owner setup, authorized by SETUP_TOKEN.

`scripts/init_secrets.py` writes a random SETUP_TOKEN into `.env`. Whoever
presents it first (before any owner exists) creates the owner account. After
that the token is inert even if it stays in `.env`: completion is recorded in
`app_config.setup_completed`, and the existence of any principal is checked as
well, so either signal alone closes setup.

Order of checks is deliberate: "already done" (409) first, so a finished
deployment answers the same way whatever is presented; then the token; and
only then the username/password rules, so an unauthenticated caller learns
nothing about them.
"""

import secrets

from .. import db
from ..audit import audit
from ..config import get_settings
from ..errors import PolicyError
from . import principals, ratelimit

SETUP_COMPLETED = "setup_completed"


def is_completed() -> bool:
    return db.get_config(SETUP_COMPLETED) == "1" or principals.any_exists()


def run(setup_token: str, username: str, password: str, ip: str) -> principals.Principal:
    """Create the owner, or raise PolicyError. Never logs the token or password."""
    if is_completed():
        raise PolicyError(409, "setup has already been completed", "setup_completed")
    expected = get_settings().setup_token
    if not expected:
        raise PolicyError(403, "setup is disabled: no SETUP_TOKEN is configured",
                          "setup_disabled")
    ratelimit.check(ip)
    if not setup_token:
        raise PolicyError(401, "setup token required", "unauthorized")
    # Compare as bytes: compare_digest refuses non-ASCII str, and a TypeError
    # here would be a 500 instead of a clean refusal.
    if not secrets.compare_digest(setup_token.encode("utf-8", "surrogateescape"),
                                  expected.encode("utf-8")):
        ratelimit.record_failure(ip)
        audit("anonymous", "auth.setup_failed", detail={"ip": ip}, result="denied")
        raise PolicyError(403, "invalid setup token", "forbidden")
    try:
        owner = principals.create_owner(username, password)
    except principals.OwnerExists as e:
        raise PolicyError(409, "setup has already been completed", "setup_completed") from e
    db.set_config(SETUP_COMPLETED, "1")
    audit(owner.username, "auth.setup", owner.username, {"ip": ip},
          actor_principal=owner.id, actor_via="setup")
    return owner
