"""The plugin service as the broker sees it: three manifests, one shared
connection, the shared OAuth config slot, boot-time refusals, env wiring."""

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aab_plugin_google.adapters import GmailAdapter
from aab_plugin_google.client import GoogleClient
from aab_plugin_google.connection import GoogleOAuthConnection
from aab_plugin_google.main import build_adapters, create_app
from aab_plugin_runtime import SecretStore, serve

from . import fake_google as fg
from .conftest import MANIFESTS, PLUGIN_TOKEN, REDIRECT, configure


def test_one_service_hosts_three_manifests(client):
    body = client.get("/manifests").json()
    assert [m["id"] for m in body["manifests"]] == ["gmail", "gcal", "gdrive"]
    assert all(m["connection"] == {"kind": "google_oauth", "shared": "google",
                                   "enforcement": "target"} for m in body["manifests"])


def test_the_three_adapters_share_one_connection_and_client():
    a = build_adapters()
    assert a[0].connection is a[1].connection is a[2].connection
    assert a[0].client is a[1].client is a[2].client


def test_client_secret_is_stored_once_in_the_shared_slot(client, app, tmp_path):
    configure(client, plugin="gcal")                  # entered on any of the three
    files = sorted(p.name for p in (tmp_path / "secrets").iterdir())
    assert files == ["google.secrets"]
    store: SecretStore = app.state.secret_store
    assert store.read_all("google")["client_secret"] == fg.CLIENT_SECRET
    assert store.read_all("gcal") == {} and store.read_all("gmail") == {}
    # ...and every hosted plugin's connect flow sees it.
    r = client.post("/connect/start", headers={"X-Plugin-Id": "gdrive"},
                    json={"enabled_plugins": ["gdrive"], "redirect_uri": REDIRECT})
    assert r.status_code == 200


def test_clearing_the_shared_secret_clears_it_for_all(client, app):
    configure(client, plugin="gmail")
    client.post("/configure", headers={"X-Plugin-Id": "gdrive"},
                json={"secrets": {"client_secret": None}})
    assert "client_secret" not in app.state.secret_store.read_all("google")


def test_shared_fields_need_a_matching_connection_slot(tmp_path):
    a = GmailAdapter(GoogleOAuthConnection(), GoogleClient(None))
    a.connection.slot = "other"
    with pytest.raises(RuntimeError, match="shared config fields"):
        serve([a], PLUGIN_TOKEN, tmp_path, Fernet.generate_key().decode())


def test_manifest_action_mismatch_refuses_to_boot(tmp_path):
    data = yaml.safe_load((MANIFESTS / "gmail.yaml").read_text(encoding="utf-8"))
    data["actions"].append({"name": "forward", "side_effect": "write"})
    path = tmp_path / "gmail.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RuntimeError, match="action mismatch"):
        GmailAdapter(GoogleOAuthConnection(), None, manifest_path=path)


def test_create_app_reads_only_generic_env(tmp_path):
    env = {"PLUGIN_TOKEN": PLUGIN_TOKEN, "PLUGIN_SECRETS_KEY": Fernet.generate_key().decode(),
           "PLUGIN_SECRETS_DIR": str(tmp_path)}
    c = TestClient(create_app(env), headers={"X-Plugin-Token": PLUGIN_TOKEN})
    assert len(c.get("/manifests").json()["manifests"]) == 3
    with pytest.raises(RuntimeError, match="empty"):
        create_app({**env, "PLUGIN_TOKEN": ""})


def test_every_endpoint_requires_the_plugin_token(app):
    c = TestClient(app)
    assert c.get("/manifests").status_code == 401
    assert c.post("/perform", json={}).status_code == 401


def test_qr_is_404(client):
    assert client.get("/connect/qr.png", headers={"X-Plugin-Id": "gmail"}).status_code == 404


def test_unknown_resource_kind_is_400(client):
    r = client.post("/normalize", headers={"X-Plugin-Id": "gmail"},
                    json={"kind": "folder", "value": "x"})
    assert r.status_code == 400
