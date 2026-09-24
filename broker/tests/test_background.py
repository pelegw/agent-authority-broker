"""background.Loop: stopping a loop waits for the thread it has in flight.

The regression behind it: the lifespan cancelled its loops with a native
Task.cancel(), which returns while an anyio worker thread is still running,
so a scheduler tick outlived the app (and, in the suite, opened the next
test's database)."""

import asyncio
import threading
import time

import anyio.to_thread
import pytest

from broker import background


def _slow_thread_loop(started: threading.Event, finished: threading.Event, seconds=0.3):
    def work():
        started.set()
        time.sleep(seconds)
        finished.set()

    async def loop():
        while True:
            await anyio.to_thread.run_sync(work)
    return loop


async def _until(event: threading.Event, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not event.is_set():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.005)


def test_stop_waits_for_the_thread_in_flight():
    started, finished = threading.Event(), threading.Event()

    async def scenario():
        loop = background.Loop(_slow_thread_loop(started, finished))
        await _until(started)
        await loop.stop()
        assert finished.is_set()          # the thread returned before stop() did
        assert loop.done()

    asyncio.run(scenario())


def test_a_stop_racing_the_start_still_lands():
    ran = []

    async def forever():
        ran.append(1)
        await asyncio.Event().wait()

    async def scenario():
        loop = background.Loop(forever)
        await loop.stop()                 # before the task ever ran
        assert loop.done()

    asyncio.run(scenario())
    assert len(ran) <= 1


def test_stop_reraises_the_error_a_loop_died_with():
    async def crashes():
        raise RuntimeError("crashed")

    async def scenario():
        loop = background.Loop(crashes)
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="crashed"):
            await loop.stop()

    asyncio.run(scenario())


def test_cancelling_the_stopper_does_not_abandon_the_thread():
    # A native cancel of whoever awaits stop() must not be forwarded to the
    # loop's task (that would be the abandoning native cancel again).
    started, finished = threading.Event(), threading.Event()

    async def scenario():
        loop = background.Loop(_slow_thread_loop(started, finished))
        await _until(started)
        stopper = asyncio.create_task(loop.stop())
        await asyncio.sleep(0.02)
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        while not loop.done():
            await asyncio.sleep(0.005)
        assert finished.is_set()          # the loop wound down, thread included

    asyncio.run(scenario())


def test_a_loop_blocked_in_its_own_shielded_stop_still_waits():
    # The supervisor stops its poll loop from its own `finally` while being
    # cancelled itself: the inner stop must still wait for the inner thread.
    started, finished = threading.Event(), threading.Event()

    async def supervisor():
        inner = background.Loop(_slow_thread_loop(started, finished))
        try:
            await asyncio.Event().wait()
        finally:
            await inner.stop()

    async def scenario():
        outer = background.Loop(supervisor)
        await _until(started)
        await outer.stop()
        assert finished.is_set()

    asyncio.run(scenario())
