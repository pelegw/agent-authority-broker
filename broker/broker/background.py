"""Background loops that stop cleanly: stopping one waits for the blocking
work it has in flight (the scheduler, the Telegram supervisor, its poll loop).

Why not `asyncio.create_task` + `Task.cancel()`: the loops do their blocking
work through `anyio.to_thread.run_sync`, which shields the worker thread from
anyio cancellation but not from a native `Task.cancel()`. A native cancel
returns at once while the thread keeps running, so at shutdown the scheduler
could still be delivering (or a Telegram tap still deciding) after the app
had stopped. In the test suite that orphaned thread went on to open the NEXT
test's database (`db.connect()` reads the path at call time) and collided
with its setup.

Cancelling through an anyio `CancelScope` honours the shield: `stop()`
returns only after the thread in flight has returned. A call made with
`abandon_on_cancel=True` (the Telegram long poll) is still dropped at once,
by design.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import anyio


class Loop:
    """One coroutine function running in its own task under a CancelScope.

    Construct it inside a running event loop. The scope exists before the
    task first runs, so a `stop()` that races the start still lands.
    """

    def __init__(self, fn: Callable[..., Awaitable[None]], *args) -> None:
        self._scope = anyio.CancelScope()
        self._task = asyncio.create_task(self._run(fn, *args))

    async def _run(self, fn: Callable[..., Awaitable[None]], *args) -> None:
        with self._scope:
            await fn(*args)

    def done(self) -> bool:
        return self._task.done()

    async def stop(self) -> None:
        """Cancel the loop and wait until it, and any thread it is blocked
        on, has finished. An exception the loop died with is re-raised."""
        self._scope.cancel()
        # Shielded twice over. The anyio shield: the caller may itself be
        # unwinding from an anyio cancellation (the supervisor's shutdown),
        # which must not cut this wait short. asyncio.shield: a native cancel
        # of the caller would otherwise be forwarded to the task it awaits,
        # i.e. exactly the native cancel this class exists to avoid.
        with anyio.CancelScope(shield=True):
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                if not self._task.done():
                    raise          # the caller was cancelled, not the loop
