"""The notify seam, the in-process adapter's error mapping, and app boot."""

import pytest
from fastapi.testclient import TestClient

from broker import db, notify
from broker.plugins.adapter import AdapterError, CallScope, InProcessAdapter

from .conftest import echo_manifest


def _audit(action):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = ?",
                                              (action,))]


def test_fan_out_reaches_every_provider(env, monkeypatch):
    got = []

    class P:
        def notify_action(self, a):
            got.append(("action", a["id"]))

        def notify_grant_request(self, g):
            got.append(("grant", g["id"]))

    monkeypatch.setattr(notify, "_PROVIDERS", [P(), P()])
    notify.notify_action({"id": "a1"})
    notify.notify_grant_request({"id": "g1"})
    assert got == [("action", "a1"), ("action", "a1"), ("grant", "g1"), ("grant", "g1")]


def test_a_failing_provider_is_audited_and_swallowed(env, monkeypatch):
    got = []

    class Broken:
        def notify_action(self, a):
            raise RuntimeError("token=SECRET")

    class Ok:
        def notify_action(self, a):
            got.append(a["id"])

    monkeypatch.setattr(notify, "_PROVIDERS", [Broken(), Ok()])
    notify.notify_action({"id": "a1"})
    assert got == ["a1"]                                  # the next provider still ran
    [row] = _audit("notify.failed")
    assert "SECRET" not in row["detail"]                  # only the exception type


def test_provider_selection_failure_is_non_fatal(env, monkeypatch):
    def boom():
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(notify, "_providers", boom)
    notify.notify_grant_request({"id": "g"})
    assert _audit("notify.failed")


def test_no_providers_by_default(env):
    assert notify._providers() == []


def test_in_process_adapter_maps_errors(echo_impl):
    a = InProcessAdapter(echo_impl, echo_manifest())

    def boom(*args):
        raise ValueError("internal detail")

    echo_impl.fail_next = 503
    with pytest.raises(AdapterError) as e:
        a.perform("post_item", {"room": "r1", "text": "x"}, CallScope("r"))
    assert e.value.status == 503
    echo_impl.perform = boom
    with pytest.raises(AdapterError) as e:
        a.perform("list_items", {}, CallScope("r"))
    assert e.value.status == 502 and "internal detail" not in e.value.message
    a.configure({}, {"api_secret": "hunter2"})
    assert "hunter2" not in repr(a._secrets)


def test_in_process_scope_is_a_json_copy(echo_impl):
    a = InProcessAdapter(echo_impl, echo_manifest())
    scope = CallScope("rid", visibility={"room": {"deny": ["r1"], "allow_only": None}})
    a.perform("list_items", {}, scope)
    seen = echo_impl.calls[-1][2]
    assert seen == scope.to_json() and seen is not scope.to_json()


def test_app_boot_runs_registry_discovery_and_scheduler(env, monkeypatch):
    from broker.actions import scheduler
    from broker.main import app
    from broker.plugins import registry
    discovered = []
    monkeypatch.setattr(scheduler, "_tick", lambda: 0)
    monkeypatch.setattr(registry.Registry, "discover",
                        lambda self, *a, **k: discovered.append(1))
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
    assert discovered == [1]
