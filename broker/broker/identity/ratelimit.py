"""In-process throttle on failed owner-credential attempts, per client IP.

Password login, the setup token and the password-change endpoint all feed the
same limiter: after `MAX_FAILURES` failures inside `WINDOW_SECONDS` from one
IP, further attempts are refused with 429 before any password is checked.

In-process state is fine because the broker runs exactly one worker; it resets
on restart, which only ever helps a legitimate owner.
"""

import logging
import threading
import time
from collections import defaultdict, deque

from ..errors import PolicyError
from ..logging_setup import kv

log = logging.getLogger(__name__)

MAX_FAILURES = 5
WINDOW_SECONDS = 60

_lock = threading.Lock()
_failures: dict[str, deque[float]] = defaultdict(deque)


def _now() -> float:
    return time.monotonic()


def _prune(q: deque[float], now: float) -> None:
    while q and now - q[0] >= WINDOW_SECONDS:
        q.popleft()


def check(ip: str) -> None:
    """Raise 429 when this IP has used up its failures for the window."""
    now = _now()
    with _lock:
        q = _failures.get(ip)
        if q is None:
            return
        _prune(q, now)
        if not q:
            del _failures[ip]   # keep the map from growing with one-off IPs
            return
        if len(q) >= MAX_FAILURES:
            log.warning("owner credential attempts rate-limited %s",
                        kv(ip=ip, failures=len(q), window_seconds=WINDOW_SECONDS))
            raise PolicyError(429, "too many failed attempts; try again in a minute")


def record_failure(ip: str) -> None:
    now = _now()
    with _lock:
        q = _failures[ip]
        _prune(q, now)
        q.append(now)


def reset() -> None:
    """Forget all failures (tests; never called by the app)."""
    with _lock:
        _failures.clear()
