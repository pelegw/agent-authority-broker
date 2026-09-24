"""The service as the container runs it: built from env, token-gated, its
manifest intact, and nothing configurable over the network."""

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aab_plugin_whatsapp import main
from aab_plugin_whatsapp.adapter import MANIFEST_PATH, WhatsAppAdapter
from aab_plugin_whatsapp.archive import Archive

from .conftest import PLUGIN_TOKEN
from .fakes import SIDECAR_TOKEN, SIDECAR_URL, seed_archive


@pytest.fixture()
def environ(tmp_path):
    seed_archive(tmp_path / "messages.db")
    return {"PLUGIN_TOKEN": PLUGIN_TOKEN, "PLUGIN_SECRETS_KEY": Fernet.generate_key().decode(),
            "PLUGIN_SECRETS_DIR": str(tmp_path / "secrets"), "SIDECAR_URL": SIDECAR_URL,
            "SIDECAR_TOKEN": SIDECAR_TOKEN, "MESSAGES_DB": str(tmp_path / "messages.db")}


def test_create_app_from_env(environ):
    c = TestClient(main.create_app(environ), headers={"X-Plugin-Token": PLUGIN_TOKEN})
    manifests = c.get("/manifests").json()["manifests"]
    assert [m["id"] for m in manifests] == ["whatsapp"]
    # Reads work from env alone (the archive path came from MESSAGES_DB).
    r = c.post("/perform", json={"action": "list_chats", "params": {}, "scope": {}})
    assert len(r.json()["data"]["items"]) == 3


def test_build_adapter_uses_env_values(environ):
    a = main.build_adapter(environ)
    assert a.sidecar.base_url == SIDECAR_URL and a.archive.path == environ["MESSAGES_DB"]


def test_defaults_are_the_compose_values():
    a = main.build_adapter({"SIDECAR_TOKEN": SIDECAR_TOKEN})
    assert a.sidecar.base_url == "http://whatsapp-sidecar:8081"
    assert a.archive.path == "/data/messages.db"


@pytest.mark.parametrize("missing", ["PLUGIN_TOKEN", "SIDECAR_TOKEN"])
def test_boot_refuses_without_a_token(environ, missing):
    environ[missing] = ""
    with pytest.raises((RuntimeError, ValueError)):
        main.create_app(environ)


def test_every_endpoint_requires_the_plugin_token(environ):
    anon = TestClient(main.create_app(environ), raise_server_exceptions=False)
    for method, path in [("GET", "/manifests"), ("GET", "/status"), ("POST", "/perform"),
                         ("GET", "/connect/qr.png"), ("POST", "/configure")]:
        assert anon.request(method, path, json={}).status_code == 401, path


def test_configure_cannot_redirect_the_plugin(client, adapter, sidecar):
    # There is no network path from /configure to the sidecar URL or the
    # archive path: config is accepted and ignored.
    r = client.post("/configure", json={"config": {"sidecar_url": "http://evil.test",
                                                   "messages_db": "/etc/passwd"},
                                        "secrets": {}})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert adapter.sidecar.base_url == "http://whatsapp-sidecar.test:8081"
    assert adapter.archive.path.endswith("messages.db")
    client.post("/perform", json={"action": "send_message",
                                  "params": {"to": "972501111111", "text": "x"}, "scope": {}})
    assert sidecar.sent == [("972501111111@s.whatsapp.net", "x")]


def test_tokens_never_appear_in_responses(client, sidecar):
    for method, path, body in [("GET", "/manifests", None), ("GET", "/status", None),
                               ("POST", "/connect/start", {}), ("POST", "/disconnect", None)]:
        r = client.request(method, path, json=body)
        assert SIDECAR_TOKEN not in r.text and PLUGIN_TOKEN not in r.text


def test_manifest_is_the_packaged_file(client):
    served = client.get("/manifests").json()["manifests"][0]
    assert served == yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert served["config_schema"] == []
    assert served["connection"] == {"kind": "sidecar_qr", "enforcement": "proxy"}


def test_every_manifest_action_is_implemented(adapter):
    assert {a["name"] for a in adapter.manifest["actions"]} == set(adapter._actions)


def test_manifest_adapter_mismatch_refuses_to_boot(tmp_path, sidecar, archive):
    m = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    m["actions"].append({"name": "delete_chat", "side_effect": "destructive"})
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(m), encoding="utf-8")
    with pytest.raises(RuntimeError, match="mismatch"):
        WhatsAppAdapter(sidecar.client(), archive, manifest_path=path)


def test_unexpected_failure_is_502_without_internals(client, adapter, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret-internal-detail")
    monkeypatch.setattr(adapter.archive, "list_chats", boom)
    r = client.post("/perform", json={"action": "list_chats", "params": {}, "scope": {}})
    assert r.status_code == 502 and "secret-internal-detail" not in r.text


def test_archive_failure_is_503(client, adapter, monkeypatch):
    import sqlite3

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(adapter.archive, "chat_is_visible", locked)
    r = client.post("/perform", json={"action": "get_media",
                                      "params": {"chat": "972502222222@s.whatsapp.net",
                                                 "message_id": "B1"}, "scope": {}})
    assert r.status_code == 503


def test_archive_object_is_read_only_by_construction(archive):
    assert not any(name.startswith(("insert", "update", "delete", "write"))
                   for name in dir(Archive))
