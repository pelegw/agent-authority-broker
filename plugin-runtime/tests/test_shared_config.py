"""Shared config slots and the OAuth redirect URI on /connect/start.

Added for plugin-google (one OAuth client behind gmail, gcal, gdrive), but
generic: any manifest may mark config fields `shared: true`, and any
connection whose start() takes `redirect_uri` receives the broker's."""

import copy

import pytest
from fastapi.testclient import TestClient

from aab_plugin_runtime import serve

from .conftest import TOKEN, echo_mod


class AccountConnection(echo_mod.EchoConnection):
    """An OAuth-shaped connection: a shared slot and a redirect-aware start."""
    slot = "acct"

    def __init__(self):
        super().__init__()
        self.redirects = []

    def start(self, enabled_plugins, redirect_uri=None):
        self.redirects.append(redirect_uri)
        return {"kind": "oauth", "url": "https://consent.test/"}


def shared_adapter(pid: str, connection) -> echo_mod.EchoAdapter:
    a = echo_mod.EchoAdapter()
    m = copy.deepcopy(a.manifest)
    m["id"] = pid
    m["connection"] = {"kind": "none", "shared": "acct"}
    m["config_schema"] = [
        {"name": "client_secret", "type": "string", "secret": True, "shared": True},
        {"name": "own_secret", "type": "string", "secret": True}]
    a.manifest = m
    a.connection = connection
    return a


@pytest.fixture()
def pair(tmp_path, key):
    conn = AccountConnection()
    one, two = shared_adapter("one", conn), shared_adapter("two", conn)
    app = serve([one, two], TOKEN, tmp_path / "s", key)
    client = TestClient(app, headers={"X-Plugin-Token": TOKEN}, raise_server_exceptions=False)
    return app, client, one, two, conn


def test_shared_secret_goes_to_the_shared_slot_once(pair):
    app, client, one, two, _ = pair
    r = client.post("/configure", headers={"X-Plugin-Id": "one"},
                    json={"secrets": {"client_secret": "shared-s3cret", "own_secret": "mine"}})
    assert r.status_code == 200
    store = app.state.secret_store
    assert store.read_all("acct") == {"client_secret": "shared-s3cret"}
    assert store.read_all("one") == {"own_secret": "mine"}
    assert store.read_all("two") == {}
    # Both plugins' readers see the one shared value; own secrets stay own.
    assert one._secrets.get("client_secret") == "shared-s3cret"
    assert one._secrets.get("own_secret") == "mine"
    client.post("/configure", headers={"X-Plugin-Id": "two"}, json={"config": {}})
    assert two._secrets.get("client_secret") == "shared-s3cret"
    assert two._secrets.get("own_secret") is None
    assert "shared-s3cret" not in repr(two._secrets)


def test_shared_fields_without_a_matching_slot_refuse_to_boot(tmp_path, key):
    conn = AccountConnection()
    conn.slot = "elsewhere"
    with pytest.raises(RuntimeError, match="shared config fields"):
        serve([shared_adapter("one", conn)], TOKEN, tmp_path, key)
    a = shared_adapter("one", conn)
    a.connection = None
    with pytest.raises(RuntimeError, match="shared config fields"):
        serve([a], TOKEN, tmp_path, key)


def test_redirect_uri_reaches_a_start_that_takes_it(pair):
    _, client, _, _, conn = pair
    r = client.post("/connect/start", headers={"X-Plugin-Id": "one"}, json={
        "enabled_plugins": ["one"], "redirect_uri": "https://aab.test/oauth/callback/x"})
    assert r.json()["kind"] == "oauth"
    assert conn.redirects == ["https://aab.test/oauth/callback/x"]


def test_a_start_without_the_keyword_is_called_as_before(client):
    # The echo connection's start() takes only enabled_plugins.
    r = client.post("/connect/start", json={"enabled_plugins": ["echo"],
                                            "redirect_uri": "https://aab.test/oauth/callback/x"})
    assert r.json() == {"kind": "none", "enabled_plugins": ["echo"]}


def test_redirect_uri_length_is_bounded(client):
    r = client.post("/connect/start", json={"enabled_plugins": [],
                                            "redirect_uri": "https://x/" + "a" * 3000})
    assert r.status_code == 400
