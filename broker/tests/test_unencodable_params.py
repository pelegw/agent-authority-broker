"""Agent input that cannot cross the plugin API as UTF-8 JSON (a lone
surrogate from a JSON escape, NaN, Infinity) is a recorded 400
`invalid_params`: never a 500, never a plugin call, and never a budget
reservation (a 500 after reserving used to leave a dangling "reserved" row
in capacity_ledger)."""

import pytest

from broker import db, decisions, engine, mcp_tools
from broker.errors import PolicyError
from broker.plugins.adapter import AdapterError, CallScope, encodable
from broker.plugins.registry import get_registry

from .conftest import cap, register_remote

ACT = "/v1/targets/echo/actions"

BAD_BODIES = [
    b'{"params": {"room": "r1", "text": "bad \\ud800"}}',           # declared string
    b'{"params": {"room": "r\\udfff", "text": "x"}}',               # the selector param
    b'{"params": {"room": "r1", "text": "x", "tags": ["\\ud83d"]}}', # inside a list
    b'{"params": {"room": "r1", "text": "x", "priority": NaN}}',    # not JSON at all
    b'{"params": {"room": "r1", "text": "x", "tags": [Infinity]}}',
]


def counts() -> dict:
    with db.connect() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("capacity_ledger", "ledger_grants", "actions")}


def last_decision() -> dict:
    with db.connect() as conn:
        return dict(conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 1").fetchone())


@pytest.mark.parametrize("body", BAD_BODIES)
@pytest.mark.parametrize("as_draft", [False, True])
def test_rest_write_is_400_with_no_reservation(client, echo, make_agent, body, as_draft):
    a = make_agent([cap(["post_item"], budget={"per_day": 5})])
    if as_draft:
        body = body[:-1] + b', "as_draft": true}'
    before, calls = counts(), len(echo.impl.calls)
    r = client.post(f"{ACT}/post_item", content=body,
                    headers={**a.headers, "Content-Type": "application/json"})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "invalid_params"
    assert counts() == before                       # no ledger row, no queued action
    assert len(echo.impl.calls) == calls            # the plugin never saw it
    d = last_decision()
    assert (d["decision"], d["reason"]) == ("deny", "invalid_params")
    assert len(d["params_hash"]) == 64              # recorded, hash computed safely


def test_valid_params_still_reserve(client, echo, make_agent):
    # Control: the same call with good params does take (and commit) a slot.
    a = make_agent([cap(["post_item"], budget={"per_day": 5})])
    r = client.post(f"{ACT}/post_item", json={"params": {"room": "r1", "text": "fine"}},
                    headers=a.headers)
    assert r.status_code == 200 and counts()["capacity_ledger"] == 1


def test_mcp_path_is_the_same_400(echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    before = counts()
    with pytest.raises(PolicyError) as e:
        mcp_tools.dispatch(a.auth, "echo_post_item", {"room": "r1", "text": "bad \ud800"})
    assert (e.value.status, e.value.code) == (400, "invalid_params")
    assert counts() == before


def test_unencodable_note_is_refused_too(echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    with pytest.raises(PolicyError) as e:
        engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "x"},
                       as_draft=True, note="why \udfff")
    assert e.value.status == 400 and counts()["actions"] == 0


def test_long_poll_open_is_400(echo_local, make_agent):
    a = make_agent([cap(["watch"])])
    with pytest.raises(PolicyError) as e:
        engine.open_poll(a.auth, "echo", "watch", {"cursor": float("nan")})
    assert (e.value.status, e.value.code) == (400, "invalid_params")


def test_a_queued_row_with_bad_params_is_canceled_not_crashed(client, echo_local, make_agent,
                                                             admin_headers):
    # A row stored before this rule (or by any other path) must not become a
    # 500 with a dangling reservation at approval time either.
    a = make_agent([cap(["post_item"], mode="draft")])
    action_id = client.post(f"{ACT}/post_item", json={"params": {"room": "r1", "text": "x"}},
                            headers=a.headers).json()["action_id"]
    with db.connect() as conn:
        conn.execute("UPDATE actions SET params = ? WHERE id = ?",
                     ('{"room": "r1", "text": "bad \\ud800"}', action_id))
    before = counts()
    r = client.post(f"/v1/admin/actions/{action_id}/approve", headers=admin_headers)
    assert r.status_code == 400
    assert counts()["capacity_ledger"] == before["capacity_ledger"]
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM actions WHERE id = ?",
                            (action_id,)).fetchone()[0] == "canceled"
    assert echo_local.impl.calls == []


def test_remote_transport_refuses_before_the_network(vendored_echo, echo_impl, tmp_path):
    """Defence in depth under the engine's check: the transport itself turns
    an unencodable body into a 400, with nothing sent."""
    register_remote(echo_impl, tmp_path)
    adapter = get_registry().adapter("echo")
    sent = []
    real = adapter._factory
    adapter._factory = lambda *a: sent.append(a) or real(*a)
    for params in ({"room": "r1", "text": "\ud800"}, {"room": "r1", "n": float("inf")}):
        with pytest.raises(AdapterError) as e:
            adapter.perform("post_item", params, CallScope("rid"))
        assert e.value.status == 400
    with pytest.raises(AdapterError) as e:
        adapter.normalize("room", "r\ud800")
    assert e.value.status == 400
    assert sent == [] and echo_impl.calls == []


def test_in_process_transport_applies_the_same_rule(echo_local):
    adapter = get_registry().adapter("echo")
    with pytest.raises(AdapterError) as e:
        adapter.perform("post_item", {"room": "r1", "text": "\ud800"}, CallScope("rid"))
    assert e.value.status == 400 and echo_local.impl.calls == []


def test_encodable_matches_what_httpx_sends():
    assert encodable({"text": "שלום ✓ 😀", "n": 1.5, "l": [None, True]})
    for bad in ({"t": "\ud800"}, {"n": float("nan")}, {"n": float("-inf")}, {"o": object()}):
        assert not encodable(bad)
    # The safe hash of a refused body is stable (same input, same hash).
    assert engine.safe_params_hash({"t": "\ud800"}) == engine.safe_params_hash({"t": "\ud800"})
    # ...and for normal params it is exactly the decision record's own hash.
    assert engine.safe_params_hash({"a": 1}) == decisions.params_hash({"a": 1})
