"""The agent REST surface: status codes, compact bodies, long-poll, resolve,
and /v1/me."""

import json
import threading
import time

import pytest

from broker import hidden
from broker.config import get_settings
from broker.plugins import settings

from .conftest import cap, enable_plugin

ACT = "/v1/targets/echo/actions"


def test_perform_200_returns_only_plugin_data(client, echo, make_agent):
    a = make_agent([cap(["post_item"])])
    r = client.post(f"{ACT}/post_item", json={"params": {"room": "r1", "text": "unique-text"}},
                    headers=a.headers)
    assert r.status_code == 200
    assert set(r.json()) == {"id"} and "unique-text" not in r.text   # no echoed input


def test_perform_202_pending_and_scheduled(client, echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    r = client.post(f"{ACT}/post_item", json={"params": {"room": "r1", "text": "x"},
                                              "as_draft": True}, headers=a.headers)
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"
    assert set(r.json()) == {"status", "action_id"}
    r = client.post(f"{ACT}/post_item", json={"params": {"room": "r1", "text": "x"},
                                              "delay_seconds": 300}, headers=a.headers)
    assert r.status_code == 202 and r.json()["status"] == "scheduled"


@pytest.mark.parametrize("setup,status,code", [
    ("out_of_grant", 403, "out_of_grant"), ("unknown_action", 404, "not_found"),
    ("disabled", 404, "not_found"), ("not_connected", 503, "not_connected"),
    ("fail503", 503, "unavailable"), ("fail502", 502, "unknown_outcome"),
    ("budget", 429, "budget_exhausted"), ("bad_params", 400, "invalid_params")])
def test_error_statuses_are_compact(client, echo, make_agent, setup, status, code):
    a = make_agent([cap(["post_item"], selector={"room": ["r1"]}, budget={"per_day": 0})
                    if setup == "budget" else cap(["post_item"], selector={"room": ["r1"]})])
    action, params = "post_item", {"room": "r1", "text": "x"}
    if setup == "out_of_grant":
        params["room"] = "r2"
    elif setup == "unknown_action":
        action = "nope"
    elif setup == "disabled":
        settings.set_enabled("echo", False)
    elif setup == "not_connected":
        enable_plugin(connected=False)
    elif setup.startswith("fail"):
        echo.impl.fail_next = int(setup[4:])
    elif setup == "bad_params":
        params["text"] = ""
    r = client.post(f"{ACT}/{action}", json={"params": params}, headers=a.headers)
    assert r.status_code == status, r.text
    body = r.json()
    assert body["code"] == code and set(body) <= {"error", "code", "hint", "grant_id",
                                                  "budget"}


def test_401_without_a_key_and_admin_tokens_are_not_agent_keys(client, echo_local,
                                                               admin_headers):
    for headers in ({}, admin_headers, {"Authorization": "Bearer aab_nope"}):
        r = client.post(f"{ACT}/list_items", json={"params": {}}, headers=headers)
        assert r.status_code == 401 and r.json()["code"] == "unauthorized"


def test_unknown_body_fields_are_rejected_without_echo(client, echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    r = client.post(f"{ACT}/list_items", json={"params": {}, "sneaky": "value-xyz"},
                    headers=a.headers)
    assert r.status_code == 422 and "value-xyz" not in r.text
    assert r.json()["code"] == "invalid_request"


def test_binary_action_returns_bytes(client, echo, make_agent):
    a = make_agent([cap(["get_blob"])])
    r = client.post(f"{ACT}/get_blob", json={"params": {"item_id": "i2"}}, headers=a.headers)
    assert r.content == b"blob:i2" and r.headers["content-type"] == "application/x-echo"


def test_list_targets(client, echo_local, make_agent):
    a = make_agent([cap(["list_items", "get_item"])])
    body = client.get("/v1/targets", headers=a.headers).json()
    assert body == {"items": [{"id": "echo", "display_name": "Echo",
                               "description": body["items"][0]["description"],
                               "actions": ["get_item", "list_items"]}]}
    settings.set_enabled("echo", False)
    assert client.get("/v1/targets", headers=a.headers).json() == {"items": []}


# ------------------------------------------------------------ long-poll

@pytest.fixture()
def fast_poll(monkeypatch):
    monkeypatch.setenv("LONG_POLL_INTERVAL_SECONDS", "0.05")
    get_settings.cache_clear()


def test_long_poll_bootstrap_and_immediate_result(client, echo, make_agent, fast_poll):
    a = make_agent([cap(["watch"])])
    boot = client.get(f"{ACT}/watch", headers=a.headers).json()
    assert boot == {"cursor": 4, "items": []}
    r = client.get(f"{ACT}/watch?cursor=2&wait=5", headers=a.headers).json()
    assert [i["id"] for i in r["items"]] == ["i3", "i4"]


def test_long_poll_waits_then_returns_empty(client, echo_local, make_agent, fast_poll):
    a = make_agent([cap(["watch"])])
    t0 = time.monotonic()
    r = client.get(f"{ACT}/watch?cursor=4&wait=1", headers=a.headers)
    assert r.json() == {"cursor": 4, "items": []} and time.monotonic() - t0 >= 0.9


def test_long_poll_returns_early_when_something_arrives(client, echo_local, make_agent,
                                                        fast_poll):
    a = make_agent([cap(["watch"])])

    def later():
        time.sleep(0.3)
        echo_local.impl.seq += 1
        echo_local.impl.items["i9"] = {"id": "i9", "room": "r2", "folder": "b",
                                       "sender": "s1", "text": "new", "seq": echo_local.impl.seq}

    threading.Thread(target=later).start()
    t0 = time.monotonic()
    r = client.get(f"{ACT}/watch?cursor=4&wait=10", headers=a.headers).json()
    assert [i["id"] for i in r["items"]] == ["i9"] and time.monotonic() - t0 < 5


def test_long_poll_records_one_decision_and_one_outcome(client, echo_local, make_agent,
                                                        fast_poll):
    from broker import db
    a = make_agent([cap(["watch"])])
    client.get(f"{ACT}/watch?cursor=4&wait=1", headers=a.headers)
    with db.connect() as conn:
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM decisions ORDER BY id")]
    assert kinds == ["decision", "outcome"]


def test_long_poll_only_for_long_poll_actions(client, echo_local, make_agent):
    a = make_agent([cap(["list_items", "watch"])])
    r = client.get(f"{ACT}/list_items", headers=a.headers)
    assert r.status_code == 400
    assert client.get(f"{ACT}/watch?cursor=abc", headers=a.headers).status_code == 400
    b = make_agent([cap(["list_items"])])
    assert client.get(f"{ACT}/watch", headers=b.headers).status_code == 403


def test_long_poll_respects_visibility(client, echo_local, make_agent, fast_poll):
    a = make_agent([cap(["watch"], selector={"room": ["r1"]})])
    r = client.get(f"{ACT}/watch?cursor=0", headers=a.headers).json()
    assert [i["id"] for i in r["items"]] == ["i1", "i3"]


# ------------------------------------------------------------ resolve

def test_resolve_respects_visibility(client, echo, make_agent):
    a = make_agent([cap(["list_items"], selector={"room": ["r1", "r2", "r3"]})])
    hidden.add("echo", "room", "r2")
    r = client.get("/v1/targets/echo/resolve?kind=room&q=room", headers=a.headers).json()
    assert [i["id"] for i in r["items"]] == ["r1", "r3"]       # r2 hidden, r4 not granted


def test_resolve_needs_a_grant_on_the_target(client, echo_local, make_agent):
    a = make_agent()
    r = client.get("/v1/targets/echo/resolve?kind=room&q=room", headers=a.headers)
    assert r.status_code == 403
    b = make_agent([cap(["list_items"])])
    assert client.get("/v1/targets/echo/resolve?kind=item&q=x",
                      headers=b.headers).status_code == 400     # item is not resolvable
    assert client.get("/v1/targets/nope/resolve?kind=room",
                      headers=b.headers).status_code == 404


# ------------------------------------------------------------ /v1/me

def test_me_reports_access_but_never_hidden_lists(client, echo_local, make_agent):
    a = make_agent([cap(["list_items", "post_item"], selector={"room": ["r1", "r2"]},
                        budget={"per_day": 5})],
                   denies={"echo": {"room": ["r4"]}})
    hidden.add("echo", "room", "r2", label="Secret Room")
    body = client.get("/v1/me", headers=a.headers).json()
    text = json.dumps(body)
    assert "r2" not in text and "Secret Room" not in text and "r4" not in text
    [c] = body["targets"]["echo"]["capabilities"]
    assert c["selector"] == {"room": ["r1"]}
    assert c["remaining"] == {a.grant_id: {"per_day": 5}}
    assert body["targets"]["echo"]["enforced_where"]["room"] == "proxy"
    assert (body["role"], body["depth"], body["delegated"]) == ("full", 0, False)


def test_me_drops_a_capability_whose_selector_is_all_hidden(client, echo_local, make_agent):
    a = make_agent([cap(["list_items"], selector={"room": ["r2"]})])
    hidden.add("echo", "room", "r2")
    assert client.get("/v1/me", headers=a.headers).json()["targets"]["echo"][
        "capabilities"] == []
