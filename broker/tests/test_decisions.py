"""The hash-chained decision record: ordering, verification, tampering."""

import json

import pytest

from broker import db, decisions, engine
from broker.config import get_settings
from broker.errors import PolicyError

from .conftest import cap


def _rows():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM decisions ORDER BY id")]


def _signing(monkeypatch, key="k" * 32):
    monkeypatch.setenv("DECISION_SIGNING_KEY", key)
    get_settings.cache_clear()


def test_decision_row_precedes_the_side_effect(echo, make_agent, monkeypatch):
    a = make_agent([cap(["post_item"])])
    seen = []
    real = echo.impl.perform

    def spy(action, params, scope):
        seen.append([(r["kind"], r["decision"]) for r in _rows()])
        return real(action, params, scope)

    monkeypatch.setattr(echo.impl, "perform", spy)
    engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "hi"})
    assert seen == [[("decision", "allow")]]           # recorded before the plugin ran
    kinds = [(r["kind"], r["decision"], r["outcome"]) for r in _rows()]
    assert kinds == [("decision", "allow", None), ("outcome", None, "ok")]
    assert len({r["request_id"] for r in _rows()}) == 1


def test_denies_are_recorded_and_nothing_runs(echo_local, make_agent):
    a = make_agent([cap(["post_item"], selector={"room": ["r1"]})])
    with pytest.raises(PolicyError) as e:
        engine.perform(a.auth, "echo", "post_item", {"room": "r2", "text": "x"})
    assert e.value.status == 403
    [row] = _rows()
    assert (row["kind"], row["decision"], row["reason"]) == ("decision", "deny", "out_of_grant")
    assert row["key_name"] == a.auth.name and row["resource"] == "r2"
    assert echo_local.impl.calls == []


def test_params_are_never_stored_only_hashed(echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "very-private-text"})
    with db.connect() as conn:
        dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM decisions")])
    assert "very-private-text" not in dump
    assert _rows()[0]["params_hash"] == decisions.params_hash(
        {"room": "r1", "text": "very-private-text", "priority": "normal"})


def test_chain_links_and_verifies_unsigned(env):
    for i in range(3):
        decisions.record(request_id=f"r{i}", kind="decision", target="echo", action="x",
                         decision="allow")
    rows = _rows()
    assert rows[0]["prev_hash"] == decisions.GENESIS
    assert [r["prev_hash"] for r in rows[1:]] == [r["hash"] for r in rows[:-1]]
    assert all(r["signed"] == 0 for r in rows)
    assert decisions.verify() == {"ok": True, "checked": 3, "first_bad_id": None,
                                  "signed": False}


def test_signed_with_key(env, monkeypatch):
    _signing(monkeypatch)
    decisions.record(request_id="r", kind="decision", target="echo", action="x",
                     decision="allow")
    assert _rows()[0]["signed"] == 1
    assert decisions.verify() == {"ok": True, "checked": 1, "first_bad_id": None,
                                  "signed": True}
    # A different key cannot verify it.
    _signing(monkeypatch, "other-key")
    assert decisions.verify()["first_bad_id"] == 1


def test_signed_rows_fail_closed_without_the_key(env, monkeypatch):
    _signing(monkeypatch)
    decisions.record(request_id="r", kind="decision", target="echo", action="x")
    monkeypatch.delenv("DECISION_SIGNING_KEY")
    get_settings.cache_clear()
    assert decisions.verify()["ok"] is False


@pytest.mark.parametrize("column,value", [("reason", "edited"), ("decision", "allow"),
                                          ("grant_chain", '["forged"]'), ("key_id", 999)])
def test_tamper_is_detected_at_the_row(env, monkeypatch, column, value):
    _signing(monkeypatch)
    for i in range(4):
        decisions.record(request_id=f"r{i}", kind="decision", target="echo", action="x",
                         decision="deny", reason="out_of_grant")
    with db.connect() as conn:
        conn.execute(f"UPDATE decisions SET {column} = ? WHERE id = 3", (value,))
    assert decisions.verify() == {"ok": False, "checked": 3, "first_bad_id": 3,
                                  "signed": False}


def test_deleted_row_breaks_the_chain(env):
    for i in range(3):
        decisions.record(request_id=f"r{i}", kind="decision", target="echo", action="x")
    with db.connect() as conn:
        conn.execute("DELETE FROM decisions WHERE id = 2")
    assert decisions.verify()["first_bad_id"] == 3


def test_downgrade_to_unsigned_after_signed_is_bad(env, monkeypatch):
    _signing(monkeypatch)
    decisions.record(request_id="a", kind="decision", target="echo", action="x")
    monkeypatch.delenv("DECISION_SIGNING_KEY")
    get_settings.cache_clear()
    decisions.record(request_id="b", kind="decision", target="echo", action="x")
    _signing(monkeypatch)
    assert decisions.verify()["first_bad_id"] == 2


def test_verify_from_id(env):
    for i in range(4):
        decisions.record(request_id=f"r{i}", kind="decision", target="echo", action="x")
    out = decisions.verify(from_id=3)
    assert out["ok"] and out["checked"] == 2


def test_admin_list_filters_and_verify_route(client, admin_headers, echo_local, make_agent):
    a = make_agent([cap(["post_item"], selector={"room": ["r1"]})])
    engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "x"})
    with pytest.raises(PolicyError):
        engine.perform(a.auth, "echo", "post_item", {"room": "r2", "text": "x"})
    r = client.get("/v1/admin/decisions", params={"decision": "deny"}, headers=admin_headers)
    assert [d["reason"] for d in r.json()["items"]] == ["out_of_grant"]
    page = client.get("/v1/admin/decisions", params={"limit": 2, "key": a.key_id},
                      headers=admin_headers).json()
    assert len(page["items"]) == 2 and page["next_cursor"] is not None
    rest = client.get("/v1/admin/decisions", params={"cursor": page["next_cursor"]},
                      headers=admin_headers).json()
    assert len(rest["items"]) == 1
    v = client.get("/v1/admin/decisions/verify", headers=admin_headers).json()
    assert v["ok"] is True and v["checked"] == 3
    assert client.get("/v1/admin/decisions/verify").status_code == 401
