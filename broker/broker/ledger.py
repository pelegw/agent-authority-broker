"""Budgets: the per-key per-minute limiter and the per-grant capacity ledger.

Two independent limits apply to every write the engine performs (reads skip
the ledger entirely):

  * `rate_per_min` of the key: an in-process sliding-minute counter, ported
    from WA_GW `policy.RateLimiter`. One uvicorn worker only; a second
    worker would get its own uncoordinated counter.
  * `budget` of each capability along the grant chain: `capacity_ledger`
    holds one row per performed (or in-flight) call and `ledger_grants` one
    charge per grant in the chain, so a parent grant's `per_day` bounds its
    whole delegated subtree. Checked and reserved inside `BEGIN IMMEDIATE`,
    so REST, MCP and scheduler threads cannot overspend together.

Reservation lifecycle: `reserve` -> the plugin is called -> `commit` on
success, `release` on a definite non-delivery (503, 4xx). On a 502 (unknown
outcome) the reservation is simply left `reserved`: it keeps counting for 24
hours, like a committed call, because the action may well have happened.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from . import db
from .authority import store
from .errors import PolicyError

DAY = 86400
MINUTE = 60


class RateLimiter:
    """In-process per-key sliding-minute counter."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: dict[int, list[float]] = {}

    def check(self, key_id: int, per_min: int) -> bool:
        """Record one attempt; False when the key is over its budget."""
        now = time.monotonic()
        with self._lock:
            window = [t for t in self._events.get(key_id, []) if now - t < MINUTE]
            if len(window) >= per_min:
                self._events[key_id] = window
                return False
            window.append(now)
            self._events[key_id] = window
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


rate_limiter = RateLimiter()


@dataclass(frozen=True)
class GrantLimit:
    """The budget one grant in the chain puts on this call: the capability
    of that grant covering the action, and the actions it spans (charges for
    any of them count against it)."""
    grant_id: str
    actions: frozenset[str]
    per_day: int | None = None
    per_minute: int | None = None


def limits_for(chain_ids: Iterable[str], target: str, action: str) -> list[GrantLimit]:
    """Budget of every grant in the chain for (target, action). When a grant
    has several capabilities covering the action, the tightest budget of each
    field applies (conservative)."""
    out = []
    for gid in chain_ids:
        g = store.get(gid)
        if g is None:
            continue
        caps = [c for c in g.capabilities if c.target == target and action in c.actions]
        if not caps:
            continue
        acts = frozenset().union(*(c.actions for c in caps))
        day = [c.budget["per_day"] for c in caps if "per_day" in c.budget]
        minute = [c.budget["per_minute"] for c in caps if "per_minute" in c.budget]
        out.append(GrantLimit(gid, acts, min(day) if day else None,
                              min(minute) if minute else None))
    return out


def _used(conn, grant_id: str, target: str, actions: frozenset[str], since: int) -> int:
    marks = ",".join("?" * len(actions))
    return conn.execute(
        "SELECT COUNT(*) FROM capacity_ledger l JOIN ledger_grants g ON g.ledger_id = l.id"
        f" WHERE g.grant_id = ? AND l.target = ? AND l.action IN ({marks})"
        " AND l.state IN ('reserved', 'committed') AND l.ts >= ?",
        (grant_id, target, *sorted(actions), since)).fetchone()[0]


def reserve(auth, chain_ids: Iterable[str], target: str, action: str,
            limits: list[GrantLimit] | None = None) -> int:
    """Reserve one call or raise 429 naming what is exhausted."""
    chain_ids = list(chain_ids)
    if limits is None:
        limits = limits_for(chain_ids, target, action)
    now = int(time.time())
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for lim in limits:
                for field, window, n in (("per_day", DAY, lim.per_day),
                                         ("per_minute", MINUTE, lim.per_minute)):
                    if n is not None and _used(conn, lim.grant_id, target, lim.actions,
                                               now - window) >= n:
                        raise PolicyError(
                            429, f"budget exhausted: grant {lim.grant_id} allows {n} "
                                 f"{field.replace('_', ' ')} for {target}.{action}",
                            "budget_exhausted", extra={"grant_id": lim.grant_id,
                                                       "budget": field})
            ledger_id = conn.execute(
                "INSERT INTO capacity_ledger (ts, key_id, target, action, state)"
                " VALUES (?, ?, ?, ?, 'reserved')",
                (now, auth.key_id, target, action)).lastrowid
            conn.executemany("INSERT OR IGNORE INTO ledger_grants (ledger_id, grant_id)"
                             " VALUES (?, ?)", [(ledger_id, g) for g in chain_ids])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    if not rate_limiter.check(auth.key_id, auth.rate_per_min):
        release(ledger_id)
        raise PolicyError(429, f"rate limit: {auth.rate_per_min} calls/minute for this key",
                          "rate_limited")
    return ledger_id


def _set_state(ledger_id: int, state: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE capacity_ledger SET state = ? WHERE id = ? AND state = 'reserved'",
                     (state, ledger_id))


def commit(ledger_id: int) -> None:
    _set_state(ledger_id, "committed")


def release(ledger_id: int) -> None:
    """A definite non-delivery: the reservation stops counting."""
    _set_state(ledger_id, "released")


def remaining(chain_ids: Iterable[str], target: str, action: str) -> dict[str, dict]:
    """{grant_id: {per_day?: left, per_minute?: left}} for get_my_access."""
    now = int(time.time())
    out: dict[str, dict] = {}
    with db.connect() as conn:
        for lim in limits_for(chain_ids, target, action):
            entry = {}
            if lim.per_day is not None:
                entry["per_day"] = max(0, lim.per_day - _used(
                    conn, lim.grant_id, target, lim.actions, now - DAY))
            if lim.per_minute is not None:
                entry["per_minute"] = max(0, lim.per_minute - _used(
                    conn, lim.grant_id, target, lim.actions, now - MINUTE))
            if entry:
                out[lim.grant_id] = entry
    return out
