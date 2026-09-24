"""The service as the container runs it: built from env, token-gated, its
manifest intact."""

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aab_plugin_github import main
from aab_plugin_github.adapter import MANIFEST_PATH, GitHubAdapter

from .conftest import PLUGIN_TOKEN


@pytest.fixture()
def environ(tmp_path):
    return {"PLUGIN_TOKEN": PLUGIN_TOKEN, "PLUGIN_SECRETS_KEY": Fernet.generate_key().decode(),
            "PLUGIN_SECRETS_DIR": str(tmp_path / "secrets")}


def test_create_app_from_env(environ):
    c = TestClient(main.create_app(environ), headers={"X-Plugin-Token": PLUGIN_TOKEN})
    manifests = c.get("/manifests").json()["manifests"]
    assert [m["id"] for m in manifests] == ["github"]
    s = c.get("/status").json()
    assert s["connected"] is False and s["enforcement"] == "proxy"


def test_the_key_path_comes_from_env(environ, tmp_path):
    environ["GITHUB_APP_PRIVATE_KEY_PATH"] = str(tmp_path / "app.pem")
    assert main.build_adapter(environ).connection._key_path == str(tmp_path / "app.pem")
    assert main.build_adapter({}).connection._key_path is None


def test_boot_refuses_without_a_token(environ):
    environ["PLUGIN_TOKEN"] = ""
    with pytest.raises(RuntimeError):
        main.create_app(environ)


def test_every_endpoint_requires_the_plugin_token(environ):
    anon = TestClient(main.create_app(environ), raise_server_exceptions=False)
    for method, path in [("GET", "/manifests"), ("GET", "/status"), ("POST", "/perform"),
                         ("POST", "/configure"), ("POST", "/connect/start"),
                         ("POST", "/connect/finish"), ("POST", "/disconnect")]:
        assert anon.request(method, path, json={}).status_code == 401, path


def test_there_is_no_qr_flow(environ):
    c = TestClient(main.create_app(environ), headers={"X-Plugin-Token": PLUGIN_TOKEN})
    assert c.get("/connect/qr.png").status_code == 404


def test_the_manifest_and_the_code_agree():
    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["id"] == "github" and manifest["connection"]["kind"] == "github_app"
    names = {a["name"] for a in manifest["actions"]}
    assert names == set(GitHubAdapter()._actions)
    assert all(a.get("target_permissions") for a in manifest["actions"])
    secrets = {f["name"] for f in manifest["config_schema"] if f.get("secret")}
    assert secrets == {"private_key_pem", "pat"}


def test_a_manifest_the_code_does_not_implement_refuses_to_start(tmp_path):
    m = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    m["actions"].append({**m["actions"][0], "name": "force_push"})
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(m))
    with pytest.raises(RuntimeError, match="mismatch"):
        GitHubAdapter(manifest_path=path)
