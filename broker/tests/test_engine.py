"""engine.perform: allow, draft, deny and scheduled paths; error mapping;
the engine's own post-filter."""

import time

import pytest

from broker import db, engine, hidden, notify
from broker.actions import queue
from broker.errors import PolicyError
from broker.plugins.adapter import CallScope, Result

from .conftest import cap


def _actions():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM actions")]


def test_allow_read_returns_plugin_data(echo, make_agent):
    a = make_agent([cap(["list_items"], selector={"room": ["r1"]})])
    r = engine.perform(a.auth, "echo", "list_items", {})
    assert r.status == 200
    assert [i["id"] for i in r.body["items"]] == ["i1", "i3"]


def test_allow_write_performs(echo, make_agent):
    a = make_agent([cap(["post_item"])])
    r = engine.perform(a.auth, "echo", "post_item", {"room": "r2", "text": "hi"})
    assert r.status == 200 and r.body["id"] in echo.impl.items
    assert echo.impl.calls[-1][2]["credential"] == {"permissions": {"items": "write"}}


def test_binary_result(echo, make_agent):
    a = make_agent([cap(["get_blob"])])
    r = engine.perform(a.auth, "echo", "get_blob", {"item_id": "i1"})
    assert (r.binary, r.mime) == (b"blob:i1", "application/x-echo")


def test_deny_raises_with_compact_error(echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    with pytest.raises(PolicyError) as e:
        engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "x"})
    assert e.value.body() == {"error": "not covered by any of your grants",
                              "code": "out_of_grant", "hint": "request_permission"}


def test_draft_queues_and_notifies(echo_local, make_agent, monkeypatch):
    got = []

    class Provider:
        def notify_action(self, item):
            got.append(item)

    monkeypatch.setattr(notify, "_PROVIDERS", [Provider()])
    a = make_agent([cap(["post_item"], mode="draft")])
    r = engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "hello"},
                       note="because")
    assert r.status == 202 and r.body["status"] == "pending_approval"
    [row] = _actions()
    assert (row["status"], row["id"], row["note"]) == ("pending", r.body["action_id"], "because")
    assert row["resource_label"] == "Room One"
    assert echo_local.impl.calls == []                  # nothing performed yet
    assert got[0]["summary"] == "Post to Room One: hello"
    assert got[0]["key_name"] == a.auth.name and got[0]["display_name"] == "Echo"


def test_notify_failure_never_breaks_the_draft(echo_local, make_agent, monkeypatch):
    class Broken:
        def notify_action(self, item):
            raise RuntimeError("channel down")

    monkeypatch.setattr(notify, "_PROVIDERS", [Broken()])
    a = make_agent([cap(["post_item"], mode="draft")])
    assert engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "x"}).status == 202
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'notify.failed'"
                            ).fetchone()[0] == 1


def test_scheduled_direct_call_queues_as_scheduled(echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    r = engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "x"},
                       delay_seconds=120)
    assert r.body["status"] == "scheduled"
    [row] = _actions()
    assert row["status"] == "scheduled" and row["approval_source"] == "automatic"
    assert row["run_at"] >= int(time.time()) + 119
    assert echo_local.impl.calls == []


def test_scheduling_validation(echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    for kw in ({"run_at": 1, "delay_seconds": 5}, {"delay_seconds": 0},
               {"delay_seconds": 5}, {"run_at": int(time.time()) + 90 * 86400}):
        with pytest.raises(PolicyError) as e:
            engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "x"}, **kw)
        assert e.value.status == 400, kw


def test_resolve_run_at():
    assert queue.resolve_run_at(None, None) is None
    assert queue.resolve_run_at(None, 60) >= int(time.time()) + 59


def test_adapter_404_is_the_generic_not_found(echo, make_agent):
    a = make_agent([cap(["get_item"])])
    hidden.add("echo", "item", "i2")
    with pytest.raises(PolicyError) as missing:
        engine.perform(a.auth, "echo", "get_item", {"item_id": "nope"})
    with pytest.raises(PolicyError) as hid:
        engine.perform(a.auth, "echo", "get_item", {"item_id": "i2"})
    assert missing.value.body() == hid.value.body() == {"error": "not found",
                                                        "code": "not_found"}


def test_room_hidden_item_is_404_via_the_plugin(echo, make_agent):
    # The item itself is not hidden, but its room is: the plugin enforces it.
    a = make_agent([cap(["get_item"])])
    hidden.add("echo", "room", "r1")
    with pytest.raises(PolicyError) as e:
        engine.perform(a.auth, "echo", "get_item", {"item_id": "i1"})
    assert e.value.status == 404


@pytest.mark.parametrize("status,code", [(503, "unavailable"), (502, "unknown_outcome"),
                                         (409, "conflict"), (400, "bad_request")])
def test_adapter_errors_map_to_the_contract(echo, make_agent, status, code):
    a = make_agent([cap(["post_item"])])
    echo.impl.fail_next = status
    with pytest.raises(PolicyError) as e:
        engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "x"})
    assert (e.value.status, e.value.code) == (status, code)


def test_outcome_rows_record_the_contract(echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    for status in (503, 502):
        echo_local.impl.fail_next = status
        with pytest.raises(PolicyError):
            engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "x"})
    with db.connect() as conn:
        outcomes = [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM decisions WHERE kind = 'outcome' ORDER BY id")]
    assert outcomes == ["unavailable", "unknown"]


def test_post_filter_drops_denied_rows_a_leaky_plugin_returns(echo, make_agent):
    a = make_agent([cap(["list_items", "get_item"])])
    hidden.add("echo", "item", "i3")
    echo.impl.leaky = True                         # the plugin ignores visibility
    r = engine.perform(a.auth, "echo", "list_items", {})
    assert "i3" not in [i["id"] for i in r.body["items"]]
    assert len(r.body["items"]) == 3
    with pytest.raises(PolicyError) as e:          # a single hidden object is a 404
        engine.perform(a.auth, "echo", "get_item", {"item_id": "i3"})
    assert e.value.status == 404


def test_post_filter_respects_allow_only_and_nested_lists():
    scope = CallScope("r", visibility={"item": {"deny": ["x"], "allow_only": ["a", "b"]}})
    data = {"page": {"items": [{"resource_ref": {"kind": "item", "id": i}} for i in "abcx"]},
            "other": [1, 2]}
    out = engine.post_filter(Result(data=data), scope, lambda k, i: ())
    assert [i["resource_ref"]["id"] for i in out.data["page"]["items"]] == ["a", "b"]
    assert out.data["other"] == [1, 2]
    # A malformed ref is not trusted.
    bad = engine.post_filter(Result(data=[{"resource_ref": {"kind": "item"}}]), scope,
                             lambda k, i: ())
    assert bad.data == []


def test_is_empty():
    assert engine.is_empty({"cursor": 3, "items": []})
    assert not engine.is_empty({"cursor": 3, "items": [1]})
    assert engine.is_empty(None) and not engine.is_empty({"x": 1})


def test_disabled_plugin_performs_nothing(echo_local, make_agent):
    from broker.plugins import settings
    a = make_agent([cap(["list_items"])])
    settings.set_enabled("echo", False)
    with pytest.raises(PolicyError) as e:
        engine.perform(a.auth, "echo", "list_items", {})
    assert e.value.status == 404 and echo_local.impl.calls == []
