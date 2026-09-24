"""Queued actions: agent CRUD, owner approve/reject, atomic claims, held
rows, and the delivery re-check. Ported in spirit from WA_GW test_send_drafts."""

import threading
import time

import pytest

from broker import auth, db, engine, hidden
from broker.actions import deliver, queue
from broker.authority import store
from broker.plugins import settings

from .conftest import cap, enable_plugin


def draft(agent, room="r1", text="hello", **kw):
    r = engine.perform(agent.auth, "echo", "post_item", {"room": room, "text": text}, **kw)
    assert r.status == 202
    return r.body["action_id"]


def row(action_id):
    return queue.get_row(action_id)


@pytest.fixture()
def drafter(echo_local, make_agent):
    return make_agent([cap(["post_item"], mode="draft")])


# ------------------------------------------------------------ agent side

def test_agent_crud_lifecycle(client, drafter):
    aid = draft(drafter)
    got = client.get(f"/v1/actions/{aid}", headers=drafter.headers).json()
    assert got["status"] == "pending" and got["target"] == "echo"
    assert "params" not in got                          # no echoed inputs
    listed = client.get("/v1/actions", headers=drafter.headers).json()
    assert [a["id"] for a in listed["items"]] == [aid]
    assert client.delete(f"/v1/actions/{aid}", headers=drafter.headers).json() == {
        "id": aid, "status": "canceled"}
    assert client.delete(f"/v1/actions/{aid}", headers=drafter.headers).status_code == 404


def test_actions_are_isolated_per_key(client, drafter, make_agent):
    aid = draft(drafter)
    other = make_agent([cap(["post_item"], mode="draft")])
    assert client.get(f"/v1/actions/{aid}", headers=other.headers).status_code == 404
    assert client.delete(f"/v1/actions/{aid}", headers=other.headers).status_code == 404
    assert client.get("/v1/actions", headers=other.headers).json()["items"] == []
    assert row(aid)["status"] == "pending"


def test_list_pagination(client, drafter):
    ids = [draft(drafter, text=f"t{i}") for i in range(3)]
    page = client.get("/v1/actions?limit=2", headers=drafter.headers).json()
    assert [a["id"] for a in page["items"]] == ids[:0:-1]
    rest = client.get(f"/v1/actions?limit=2&cursor={page['next_cursor']}",
                      headers=drafter.headers).json()
    assert [a["id"] for a in rest["items"]] == [ids[0]] and rest["next_cursor"] is None


# ------------------------------------------------------------ owner decisions

def test_approve_delivers_and_records_the_human(client, admin_headers, drafter, echo_local,
                                                owner):
    aid = draft(drafter)
    r = client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers)
    assert r.status_code == 200 and r.json()["status"] == "done"
    done = row(aid)
    assert (done["status"], done["approval_source"]) == ("done", "human")
    assert (done["decided_by_principal"], done["decided_via"]) == (owner.username, "token")
    assert done["result"]["data"]["id"] in echo_local.impl.items
    with db.connect() as conn:
        actors = [(r["kind"], r["actor_principal"]) for r in conn.execute(
            "SELECT kind, actor_principal FROM decisions ORDER BY id")]
    # The draft decision, then the human's delivery decision and its outcome.
    assert actors == [("decision", None), ("decision", owner.username),
                      ("outcome", owner.username)]


def test_double_approve_delivers_once(client, admin_headers, drafter, echo_local):
    aid = draft(drafter)
    assert client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers).status_code == 200
    again = client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers)
    assert again.status_code == 409 and "done" in again.json()["error"]
    assert len(echo_local.impl.calls) == 1


def test_claim_is_atomic_under_a_race(drafter):
    aid = draft(drafter)
    wins, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        wins.append(deliver.claim(aid, "sending", int(time.time())))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1


def test_reject_then_approve_conflicts(client, admin_headers, drafter, echo_local, owner):
    aid = draft(drafter)
    r = client.post(f"/v1/admin/actions/{aid}/reject", headers=admin_headers)
    assert r.json() == {"id": aid, "status": "rejected"}
    assert row(aid)["decided_by_principal"] == owner.username
    assert client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers).status_code == 409
    assert echo_local.impl.calls == []


def test_unknown_action_is_404(client, admin_headers, echo_local):
    assert client.post("/v1/admin/actions/nope/approve", headers=admin_headers).status_code == 404


def test_approve_with_future_run_at_parks_as_scheduled(client, admin_headers, drafter,
                                                       echo_local):
    aid = draft(drafter, delay_seconds=600)
    r = client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers).json()
    assert r["status"] == "scheduled"
    assert row(aid)["approval_source"] == "human" and echo_local.impl.calls == []


# ------------------------------------------------------------ held when disabled

def test_disabled_plugin_holds_pending_rows(client, admin_headers, drafter, echo_local):
    aid = draft(drafter)
    settings.set_enabled("echo", False)
    r = client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "held"
    assert row(aid)["status"] == "pending"
    listed = client.get("/v1/admin/actions", headers=admin_headers).json()["items"]
    assert listed[0]["held"] is True
    # Held rows do not expire while the plugin is off.
    queue.sweep(int(time.time()) + 10 * 86400)
    assert row(aid)["status"] == "pending"
    enable_plugin()
    assert client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers).status_code == 200


# ------------------------------------------------------------ delivery re-check

def test_human_approved_row_needs_the_key_alive(client, admin_headers, drafter, echo_local):
    aid = draft(drafter)
    auth.disable_key(drafter.key_id)
    r = client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers)
    assert r.status_code == 403
    assert row(aid)["status"] == "canceled" and echo_local.impl.calls == []


def test_human_approved_row_needs_the_resource_visible(client, admin_headers, drafter,
                                                       echo_local):
    aid = draft(drafter, room="r2")
    hidden.add("echo", "room", "r2")
    r = client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers)
    assert r.status_code == 404 and r.json() == {"error": "not found", "code": "not_found"}
    assert row(aid)["status"] == "canceled" and echo_local.impl.calls == []


def test_human_approval_stands_in_for_a_revoked_grant(client, admin_headers, drafter,
                                                      echo_local):
    aid = draft(drafter)
    store.set_status(drafter.grant_id, "revoked")
    r = client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers)
    assert r.status_code == 200 and len(echo_local.impl.calls) == 1


def test_503_on_approve_returns_to_pending(client, admin_headers, drafter, echo_local):
    aid = draft(drafter)
    echo_local.impl.fail_next = 503
    r = client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers)
    assert r.status_code == 503 and row(aid)["status"] == "pending"
    assert client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers).status_code == 200


def test_502_on_approve_fails_and_is_never_retried(client, admin_headers, drafter, echo_local):
    aid = draft(drafter)
    echo_local.impl.fail_next = 502
    r = client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers)
    assert r.status_code == 502
    failed = row(aid)
    assert failed["status"] == "failed" and failed["result"]["status"] == 502
    assert client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers).status_code == 409


# ------------------------------------------------------------ expiry

def test_stale_pending_expire_and_dangling_sending_fail(drafter):
    old, stuck = draft(drafter), draft(drafter)
    with db.connect() as conn:
        conn.execute("UPDATE actions SET expires_at = 1 WHERE id = ?", (old,))
        conn.execute("UPDATE actions SET status = 'sending', decided_at = 1 WHERE id = ?",
                     (stuck,))
    queue.sweep()
    assert row(old)["status"] == "expired"
    assert row(stuck)["status"] == "failed"


def test_human_approval_never_reaches_a_hidden_resource(client, admin_headers, drafter,
                                                        echo_local):
    # Grant revoked AND resource hidden: the human path must still 404.
    aid = draft(drafter, room="r2")
    store.set_status(drafter.grant_id, "revoked")
    hidden.add("echo", "room", "r2")
    r = client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers)
    assert r.status_code == 404 and r.json() == {"error": "not found", "code": "not_found"}
    assert row(aid)["status"] == "canceled" and echo_local.impl.calls == []
