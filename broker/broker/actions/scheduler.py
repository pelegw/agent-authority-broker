"""Fires due scheduled actions (`status = scheduled`, `run_at <= now`).

Ported from WA_GW scheduler.py: an asyncio task started in the app lifespan,
the blocking work offloaded to a thread, clean cancellation on shutdown.

Each due row is claimed scheduled -> sending atomically before delivery, so
an overlapping tick or a racing cancel can never double-deliver. Transient
failures (429, 503) release the row back to `scheduled` inside
`deliver_claimed` and the next tick retries, so a burst scheduled for one
instant drains at the key's normal rate instead of being lost. Rows whose
plugin is disabled are skipped (held) until it is enabled again.
"""

import asyncio
import contextlib
import time

import anyio.to_thread

from .. import db
from ..audit import audit
from ..errors import PolicyError
from ..plugins.registry import get_registry
from ..runtime_settings import runtime_settings
from . import deliver, queue


def _tick() -> int:
    """One pass; returns how many rows were claimed."""
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
    claimed = 0
    for action_id in due:
        if not deliver.claim(action_id, "sending", now, from_status="scheduled"):
            continue      # lost the race: canceled, or another tick took it
        claimed += 1
        # Failures are recorded and the row released/parked inside
        # deliver_claimed; one bad row must not stop the batch.
        with contextlib.suppress(PolicyError):
            deliver.deliver_claimed(action_id, queue.get_row(action_id),
                                    release_status="scheduled", actor="scheduler")
    return claimed


async def scheduler_loop() -> None:
    while True:
        try:
            await anyio.to_thread.run_sync(_tick)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            audit("system", "scheduler.error", detail={"error": type(exc).__name__},
                  result="error")
        # Read in a thread: a console edit applies from the next tick.
        tick = await anyio.to_thread.run_sync(lambda: runtime_settings().scheduler_tick_seconds)
        await asyncio.sleep(tick)
