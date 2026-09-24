"""The plugin runtime: token gate, manifests, secret store, error mapping,
connect dispatch, and multi-adapter services."""

import base64
import hmac
import os

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aab_plugin_runtime import AdapterError, SecretStore, SecretsUnreadable, from_env, serve
from aab_plugin_runtime import app as app_mod

from .conftest import TOKEN, echo_mod

# ------------------------------------------------------------ token gate


def test_every_endpoint_requires_the_token(app):
    anon = TestClient(app, raise_server_exceptions=False)
    for method, path in [("GET", "/manifests"), ("GET", "/status"), ("POST", "/configure"),
                         ("POST", "/normalize"), ("POST", "/resolve"), ("POST", "/label"),
                         ("POST", "/perform"), ("POST", "/connect/start"),
                         ("GET", "/connect/qr.png"), ("POST", "/connect/finish"),
                         ("POST", "/disconnect")]:
        r = anon.request(method, path, json={})
        assert r.status_code == 401, (method, path)
        assert r.json() == {"error": "unauthorized"}


def test_wrong_token_is_401(app):
    bad = TestClient(app, headers={"X-Plugin-Token": TOKEN + "x"})
    assert bad.get("/manifests").status_code == 401


def test_token_compare_is_constant_time(app, monkeypatch):
    seen = []
    real = hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(app_mod.hmac, "compare_digest", spy)
    TestClient(app, headers={"X-Plugin-Token": "nope"}).get("/manifests")
    assert seen == [(b"nope", TOKEN.encode())]


def test_empty_token_refuses_to_boot(tmp_path, key, echo):
    with pytest.raises(RuntimeError, match="token is empty"):
        serve([echo], "  ", tmp_path, key)


def test_request_id_is_echoed(client):
    r = client.get("/manifests", headers={"X-Request-Id": "req-1"})
    assert r.headers["x-request-id"] == "req-1"


# ------------------------------------------------------------ manifests / status


def test_manifests_lists_every_hosted_manifest(client, echo):
    body = client.get("/manifests").json()
    assert [m["id"] for m in body["manifests"]] == ["echo"]
    assert body["manifests"][0]["version"] == echo.manifest["version"]


def test_multi_adapter_service_needs_plugin_header(tmp_path, key):
    a = echo_mod.EchoAdapter()
    b = echo_mod.EchoAdapter()
    b.manifest = {**b.manifest, "id": "echotwo"}
    c = TestClient(serve([a, b], TOKEN, tmp_path, key), headers={"X-Plugin-Token": TOKEN})
    assert sorted(m["id"] for m in c.get("/manifests").json()["manifests"]) == ["echo", "echotwo"]
    assert c.get("/status").status_code == 400
    assert c.get("/status", headers={"X-Plugin-Id": "echotwo"}).status_code == 200
    assert c.get("/status", headers={"X-Plugin-Id": "nope"}).status_code == 404


def test_duplicate_ids_refuse_to_boot(tmp_path, key):
    with pytest.raises(RuntimeError, match="duplicated"):
        serve([echo_mod.EchoAdapter(), echo_mod.EchoAdapter()], TOKEN, tmp_path, key)


def test_status_includes_connection(client):
    body = client.get("/status").json()
    assert body["connected"] is True and body["connection"]["kind"] == "none"


# ------------------------------------------------------------ secret store


def test_configure_stores_secrets_write_only(client, app, echo, tmp_path):
    r = client.post("/configure", json={"config": {"greeting": "hi"},
                                        "secrets": {"api_secret": "s3cr3t-value"}})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "s3cr3t-value" not in r.text
    assert echo.greeting == "hi"
    assert client.get("/status").json()["api_secret_set"] is True
    # Encrypted at rest: the plaintext is nowhere on disk.
    for p in (tmp_path / "secrets").iterdir():
        assert b"s3cr3t-value" not in p.read_bytes()
    # No endpoint reads it back.
    for method, path in [("GET", "/manifests"), ("GET", "/status")]:
        assert "s3cr3t-value" not in client.request(method, path).text


def test_secret_store_round_trip_and_delete(tmp_path, key):
    s = SecretStore(tmp_path, key)
    s.write("echo", {"a": "1", "b": "2"})
    assert s.read_all("echo") == {"a": "1", "b": "2"}
    s.write("echo", {"a": None})
    assert s.reader("echo").get("a") is None and s.reader("echo").get("b") == "2"
    assert "2" not in repr(s.reader("echo"))
    s.wipe("echo")
    assert s.read_all("echo") == {}


def test_secret_slot_rejects_path_names(tmp_path, key):
    s = SecretStore(tmp_path, key)
    with pytest.raises(ValueError):
        s.write("../evil", {"a": "1"})


def test_boot_fails_closed_without_key_when_store_has_content(tmp_path, key, echo):
    SecretStore(tmp_path, key).write("echo", {"a": "1"})
    with pytest.raises(RuntimeError, match="refusing to start"):
        serve([echo], TOKEN, tmp_path, None)


def test_boot_without_key_on_empty_store_is_allowed_but_cannot_store(tmp_path, echo):
    c = TestClient(serve([echo], TOKEN, tmp_path, None), headers={"X-Plugin-Token": TOKEN})
    r = c.post("/configure", json={"secrets": {"api_secret": "x"}})
    assert r.status_code == 503


def test_wrong_key_reads_as_reconnect_required(tmp_path, key):
    SecretStore(tmp_path, key).write("echo", {"api_secret": "x"})
    other = Fernet.generate_key().decode()
    adapter = echo_mod.EchoAdapter()
    c = TestClient(serve([adapter], TOKEN, tmp_path, other),
                   headers={"X-Plugin-Token": TOKEN}, raise_server_exceptions=False)
    c.post("/configure", json={"config": {}})
    body = c.get("/status").json()
    assert body == {"connected": False, "healthy": False, "health": "reconnect required"}
    with pytest.raises(SecretsUnreadable):
        SecretStore(tmp_path, other).read_all("echo")


def test_secret_files_are_private(tmp_path, key):
    s = SecretStore(tmp_path / "d", key)
    s.write("echo", {"a": "1"})
    if os.name == "posix":
        assert (tmp_path / "d" / "echo.secrets").stat().st_mode & 0o077 == 0


def test_from_env_reads_only_its_own_values(tmp_path, key, echo):
    app = from_env([echo], {"PLUGIN_TOKEN": TOKEN, "PLUGIN_SECRETS_KEY": key,
                            "PLUGIN_SECRETS_DIR": str(tmp_path)})
    assert TestClient(app, headers={"X-Plugin-Token": TOKEN}).get("/manifests").status_code == 200
    with pytest.raises(RuntimeError):
        from_env([echo], {"PLUGIN_SECRETS_DIR": str(tmp_path)})


# ------------------------------------------------------------ resources


def test_normalize_resolve_label_ancestors(client):
    assert client.post("/normalize", json={"kind": "room", "value": " R1 "}).json() == {"id": "r1"}
    assert client.post("/normalize", json={"kind": "room", "value": "x"}).status_code == 400
    items = client.post("/resolve", json={"kind": "room", "query": "two"}).json()["items"]
    assert items == [{"id": "r2", "label": "Room Two", "kind": "room"}]
    assert client.post("/resolve", json={"kind": "folder", "query": "a1x",
                                         "relation": "ancestors"}).json() == {
        "ancestors": ["a1", "a", "root"]}
    assert client.post("/label", json={"kind": "room", "ids": ["r1", "zz"]}).json() == {
        "labels": {"r1": "Room One"}}


def test_bad_body_is_400(client):
    assert client.post("/normalize", json={"kind": "room"}).status_code == 400
    assert client.post("/perform", json={"action": "x", "extra": 1}).status_code == 400


# ------------------------------------------------------------ perform + error mapping


def test_perform_json_and_scope(client, echo):
    r = client.post("/perform", headers={"X-Request-Id": "rid-7"},
                    json={"action": "list_items", "params": {"room": "r1"},
                          "scope": {"visibility": {"item": {"deny": ["i3"]}}}})
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["data"]["items"]] == ["i1"]
    assert echo.calls[-1][2]["request_id"] == "rid-7"


def test_perform_binary_is_base64(client):
    r = client.post("/perform", json={"action": "get_blob", "params": {"item_id": "i1"}})
    body = r.json()
    assert base64.b64decode(body["binary_b64"]) == b"blob:i1"
    assert body["mime"] == "application/x-echo"


def test_denied_get_is_404_like_missing(client):
    hidden = client.post("/perform", json={"action": "get_item", "params": {"item_id": "i1"},
                                           "scope": {"visibility": {"room": {"deny": ["r1"]}}}})
    missing = client.post("/perform", json={"action": "get_item",
                                            "params": {"item_id": "nope"}})
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()


@pytest.mark.parametrize("status", [400, 403, 404, 409, 503, 502])
def test_adapter_error_status_passthrough(client, echo, status):
    echo.fail_next = status
    r = client.post("/perform", json={"action": "post_item",
                                      "params": {"room": "r1", "text": "x"}})
    assert r.status_code == status


def test_503_means_not_performed_and_502_means_performed(client, echo):
    before = len(echo.items)
    echo.fail_next = 503
    client.post("/perform", json={"action": "post_item", "params": {"room": "r1", "text": "x"}})
    assert len(echo.items) == before
    echo.fail_next = 502
    client.post("/perform", json={"action": "post_item", "params": {"room": "r1", "text": "x"}})
    assert len(echo.items) == before + 1


def test_unexpected_exception_is_502_without_internals(client, echo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret-internal-detail")
    monkeypatch.setattr(echo, "perform", boom)
    r = client.post("/perform", json={"action": "list_items"})
    assert r.status_code == 502
    assert "secret-internal-detail" not in r.text


def test_out_of_range_status_becomes_502():
    assert AdapterError(200, "x").status == 502
    assert AdapterError(404, "x").status == 404


# ------------------------------------------------------------ connect


def test_connect_endpoints_dispatch_to_connection(client, echo):
    assert client.post("/connect/start", json={"enabled_plugins": ["echo"]}).json() == {
        "kind": "none", "enabled_plugins": ["echo"]}
    assert client.get("/connect/qr.png").status_code == 404
    assert client.post("/disconnect").json() == {"ok": True}
    assert client.get("/status").json()["connected"] is False
    assert client.post("/connect/finish", json={"code": "c", "state": "s"}).json() == {"ok": True}
    assert echo.connection.connected is True


def test_qr_png_is_not_cacheable(tmp_path, key):
    a = echo_mod.EchoAdapter()
    a.connection.qr_png = lambda: b"\x89PNG"
    c = TestClient(serve([a], TOKEN, tmp_path, key), headers={"X-Plugin-Token": TOKEN})
    r = c.get("/connect/qr.png")
    assert r.content == b"\x89PNG" and r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"] == "image/png"


def test_plugin_without_connection_is_404_on_connect(tmp_path, key):
    a = echo_mod.EchoAdapter()
    a.connection = None
    c = TestClient(serve([a], TOKEN, tmp_path, key), headers={"X-Plugin-Token": TOKEN})
    assert c.post("/connect/start", json={}).status_code == 404


def test_bind_secrets_gives_a_slot(tmp_path, key):
    a = echo_mod.EchoAdapter()
    got = []
    a.bind_secrets = got.append
    serve([a], TOKEN, tmp_path, key)
    got[0].set("refresh", "rt")
    assert got[0].get("refresh") == "rt" and "rt" not in repr(got[0])
