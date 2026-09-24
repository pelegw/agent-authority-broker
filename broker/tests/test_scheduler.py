"""The scheduler: due scheduled actions fire once, races resolve, transient
failures retry, disabled plugins hold. Ported from WA_GW test_scheduled."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from broker import auth, background, db, engine, ledger
from broker.actions import queue, scheduler
from broker.authority import store
from broker.plugins import settings

from .conftest import cap, enable_plugin


def schedule(agent, text="later", room="r1"):
    r = engine.perform(agent.auth, "echo", "post_item", {"room": room, "text": text},
                       delay_seconds=120)
    assert r.body["status"] == "scheduled"
    return r.body["action_id"]


def make_due(*ids):
    with db.connect() as conn:
        for i in ids:
            conn.execute("UPDATE actions SET run_at = 1 WHERE id = ?", (i,))


@pytest.fixture()
def sched_agent(echo_local, make_agent):
    return make_agent([cap(["post_item"])])


def test_not_due_is_not_fired(sched_agent, echo_local):
    schedule(sched_agent)
    assert scheduler._tick() == 0 and echo_local.impl.calls == []


def test_due_action_fires_with_a_fresh_decision(sched_agent, echo_local):
    aid = schedule(sched_agent)
    make_due(aid)
    assert scheduler._tick() == 1
    assert queue.get_row(aid)["status"] == "done" and len(echo_local.impl.calls) == 1
    with db.connect() as conn:
        rows = [(r["kind"], r["decision"], r["actor_principal"]) for r in conn.execute(
            "SELECT * FROM decisions ORDER BY id")]
    assert rows == [("decision", "allow", None), ("decision", "allow", None),
                    ("outcome", None, None)]


def test_double_tick_delivers_exactly_once(sched_agent, echo_local):
    make_due(schedule(sched_agent))
    scheduler._tick()
    scheduler._tick()
    assert len(echo_local.impl.calls) == 1


def test_cancel_beats_scheduler(client, sched_agent, echo_local):
    aid = schedule(sched_agent)
    make_due(aid)
    assert client.delete(f"/v1/actions/{aid}", headers=sched_agent.headers).status_code == 200
    assert scheduler._tick() == 0 and echo_local.impl.calls == []


def test_rate_limited_batch_drains_over_ticks(echo_local, make_agent):
    a = make_agent([cap(["post_item"])], rate=1)
    ids = [schedule(a, text=f"t{i}") for i in range(3)]
    make_due(*ids)
    scheduler._tick()
    assert [queue.get_row(i)["status"] for i in ids].count("done") == 1
    assert [queue.get_row(i)["status"] for i in ids].count("scheduled") == 2
    ledger.rate_limiter.reset()          # the next minute
    scheduler._tick()
    assert [queue.get_row(i)["status"] for i in ids].count("done") == 2


def test_503_releases_back_to_scheduled(sched_agent, echo_local):
    aid = schedule(sched_agent)
    make_due(aid)
    echo_local.impl.fail_next = 503
    scheduler._tick()
    assert queue.get_row(aid)["status"] == "scheduled"
    scheduler._tick()
    assert queue.get_row(aid)["status"] == "done"


def test_502_fails_and_is_never_retried(sched_agent, echo_local):
    aid = schedule(sched_agent)
    make_due(aid)
    echo_local.impl.fail_next = 502
    scheduler._tick()
    assert queue.get_row(aid)["status"] == "failed"
    scheduler._tick()
    assert len(echo_local.impl.calls) == 1


def test_automatic_row_needs_allow_at_delivery(sched_agent, echo_local):
    aid = schedule(sched_agent)
    make_due(aid)
    store.set_status(sched_agent.grant_id, "revoked")
    scheduler._tick()
    r = queue.get_row(aid)
    assert r["status"] == "canceled" and echo_local.impl.calls == []
    assert "grant" in r["result"]["error"]


def test_automatic_row_needs_the_key_alive(sched_agent, echo_local):
    aid = schedule(sched_agent)
    make_due(aid)
    auth.disable_key(sched_agent.key_id)
    scheduler._tick()
    assert queue.get_row(aid)["status"] == "canceled" and echo_local.impl.calls == []


def test_disabled_plugin_holds_scheduled_rows(sched_agent, echo_local):
    aid = schedule(sched_agent)
    make_due(aid)
    settings.set_enabled("echo", False)
    assert scheduler._tick() == 0
    assert queue.get_row(aid)["status"] == "scheduled"
    enable_plugin()
    scheduler._tick()
    assert queue.get_row(aid)["status"] == "done"


def test_admin_cancel_only_from_scheduled(client, admin_headers, sched_agent, echo_local):
    aid = schedule(sched_agent)
    r = client.post(f"/v1/admin/actions/{aid}/cancel", headers=admin_headers)
    assert r.json() == {"id": aid, "status": "canceled"}
    again = client.post(f"/v1/admin/actions/{aid}/cancel", headers=admin_headers)
    assert again.status_code == 409


def test_done_action_cannot_be_canceled(client, sched_agent, echo_local):
    aid = schedule(sched_agent)
    make_due(aid)
    scheduler._tick()
    assert client.delete(f"/v1/actions/{aid}", headers=sched_agent.headers).status_code == 404


def test_scheduler_loop_survives_errors(monkeypatch, env):
    import asyncio

    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("db locked")

    monkeypatch.setattr(scheduler, "_tick", boom)

    async def run():
        loop = background.Loop(scheduler.scheduler_loop)     # as the lifespan runs it
        await asyncio.sleep(0.05)
        await loop.stop()

    asyncio.run(run())
    assert calls
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'scheduler.error'"
                            ).fetchone()[0] >= 1


def test_app_shutdown_waits_for_a_tick_in_flight(env, monkeypatch):
    # Regression: the lifespan cancelled the scheduler with a native
    # Task.cancel(), which returned while the tick's worker thread was still
    # running, so a tick (a delivery, even) could outlive the app; in the
    # suite it went on to open the next test's database mid-setup.
    from broker.main import app

    started, finished = threading.Event(), threading.Event()

    def slow_tick():
        started.set()
        time.sleep(0.3)
        finished.set()
        return 0

    monkeypatch.setattr(scheduler, "_tick", slow_tick)
    with TestClient(app):
        assert started.wait(5)
    assert finished.is_set()
