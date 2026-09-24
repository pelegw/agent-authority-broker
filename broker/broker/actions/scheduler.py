"""Fires due scheduled actions (`status = scheduled`, `run_at <= now`).

Ported from WA_GW scheduler.py: an asyncio task started in the app lifespan,
the blocking work offloaded to a thread, clean cancellation on shutdown.

Each due row is claimed scheduled -> sending atomically before delivery, so
an overlapping tick or a racing cancel can never double-deliver. Transient
failures (429, 503) release the row back to `scheduled` inside
`deliver_claimed` and the next tick retries, so a burst scheduled for one
instant drains at the key's normal rate instead of being lost. Rows whose
plugin is disabled are skipped (held) until it is enabled again.

Request ids: a tick runs under its own `sched-<hex>` id and each delivery
under a fresh one, so a delivery's decision and outcome rows and its log
lines (the plugin's included) share an id no other delivery has. A tick
logs a summary only when it found something due: an idle tick is silent.
"""

import asyncio
import logging
import time

import anyio.to_thread

from .. import db
from ..audit import audit
from ..errors import PolicyError
from ..logging_setup import bind, current_request_id, kv, new_request_id
from ..plugins.registry import get_registry
from ..runtime_settings import runtime_settings
from . import deliver, queue

log = logging.getLogger(__name__)
PREFIX = "sched-"


def _tick() -> int:
    """One pass; returns how many rows were claimed."""
    with bind(new_request_id(PREFIX)):
        return _tick_inner()


def _tick_inner() -> int:
    now = int(time.time())
    queue.sweep(now)
    enabled = get_registry().enabled_plugins()
    if not enabled:
        return 0
    marks = ",".join("?" * len(enabled))
    with db.connect() as conn:
        due = [r["id"] for r in conn.execute(
            "SELECT id FROM actions WHERE status = 'scheduled' AND run_at <= ?"
            f" AND target IN ({marks}) ORDER BY run_at", (now, *enabled)).fetchall()]
    claimed = failed = 0
    tick = current_request_id()
    for action_id in due:
        if not deliver.claim(action_id, "sending", now, from_status="scheduled"):
            continue      # lost the race: canceled, or another tick took it
        claimed += 1
        # Failures are recorded and the row released/parked inside
        # deliver_claimed; one bad row must not stop the batch.
        with bind(new_request_id(PREFIX)):
            log.info("scheduled action due %s", kv(action_id=action_id, tick=tick))
            try:
                deliver.deliver_claimed(action_id, queue.get_row(action_id),
                                        release_status="scheduled", actor="scheduler")
            except PolicyError:
                failed += 1
    if due:
        log.info("scheduler tick %s", kv(due=len(due), claimed=claimed,
                                         not_delivered=failed))
    return claimed


async def scheduler_loop() -> None:
    log.info("scheduler started")
    while True:
        try:
            await anyio.to_thread.run_sync(_tick)
        except asyncio.CancelledError:
            log.info("scheduler stopped")
            raise
        except Exception as exc:
            audit("system", "scheduler.error", detail={"error": type(exc).__name__},
                  result="error")
            # The type only, as in the audit row: an exception's text can
            # carry data from the row it was handling.
            log.error("scheduler tick failed %s", kv(error=type(exc).__name__))
        # Read in a thread: a console edit applies from the next tick.
        tick = await anyio.to_thread.run_sync(lambda: runtime_settings().scheduler_tick_seconds)
        await asyncio.sleep(tick)
