"""The github_app connection: configure, the install flow, status, disconnect."""

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aab_plugin_github.adapter import GitHubAdapter
from aab_plugin_github.api import GitHubAPI
from aab_plugin_github.main import create_app

from .conftest import APP_CONFIG, PLUGIN_TOKEN, configure, install
from .fakes import APP_SLUG, INSTALLATION_ID, OTHER_KEY_PEM, PAT, PRIVATE_KEY_PEM

GET_FILE = {"action": "get_file", "params": {"repo": "octo/a", "path": "README.md"},
            "scope": {"credential": {"permissions": {"contents": "read"}}}}


def start(client):
    r = client.post("/connect/start", json={"enabled_plugins": ["github"]})
    assert r.status_code == 200, r.text
    return r.json()


def finish(client, **body):
    return client.post("/connect/finish", json=body)


# ---- configure --------------------------------------------------------------------

def test_nothing_configured(client):
    s = client.get("/status").json()
    assert (s["connected"], s["mode"], s["enforcement"]) == (False, None, "proxy")
    assert client.post("/connect/start", json={}).status_code == 400


@pytest.mark.parametrize("config", [{"app_id": "abc"}, {"app_id": 12345},
                                    {"app_slug": "Has Spaces"}, {"app_slug": "a/b"},
                                    {"app_slug": "x?state=evil"}])
def test_bad_config_is_refused(client, config):
    r = client.post("/configure", json={"config": config, "secrets": {}})
    assert r.status_code == 400


def test_a_bad_private_key_is_refused_and_wiped(client, app):
    r = client.post("/configure", json={"config": APP_CONFIG,
                                        "secrets": {"private_key_pem": "not a key"}})
    assert r.status_code == 400 and "not a key" not in r.text
    assert "private_key_pem" not in app.state.secret_store.read_all("github")


def test_a_bad_pat_is_refused_and_wiped(client, app):
    r = client.post("/configure", json={"config": {}, "secrets": {"pat": "has spaces in it!!"}})
    assert r.status_code == 400 and "spaces" not in r.text
    assert "pat" not in app.state.secret_store.read_all("github")


def test_config_survives_a_container_restart(gh, clock, tmp_path):
    env = {"PLUGIN_TOKEN": PLUGIN_TOKEN, "PLUGIN_SECRETS_KEY": Fernet.generate_key().decode(),
           "PLUGIN_SECRETS_DIR": str(tmp_path / "secrets")}

    def boot():
        adapter = GitHubAdapter(GitHubAPI(transport=gh.transport(), clock=clock), clock=clock)
        return TestClient(create_app(env, adapter=adapter),
                          headers={"X-Plugin-Token": PLUGIN_TOKEN})

    first = boot()
    configure(first, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    install(first)
    # The broker does not re-send config after a plugin restart: app_id,
    # slug, key and installation must all come back from /secrets.
    second = boot()
    assert second.get("/status").json()["connected"] is True
    assert second.post("/perform", json=GET_FILE).status_code == 200


def test_the_key_can_come_from_a_file(gh, clock, tmp_path):
    key_file = tmp_path / "app.pem"
    key_file.write_text(PRIVATE_KEY_PEM)
    adapter = GitHubAdapter(GitHubAPI(transport=gh.transport(), clock=clock),
                            key_path=str(key_file), clock=clock)
    client = TestClient(create_app({"PLUGIN_TOKEN": PLUGIN_TOKEN,
                                    "PLUGIN_SECRETS_KEY": Fernet.generate_key().decode(),
                                    "PLUGIN_SECRETS_DIR": str(tmp_path / "s")},
                                   adapter=adapter),
                        headers={"X-Plugin-Token": PLUGIN_TOKEN})
    configure(client, APP_CONFIG)
    install(client)
    assert client.get("/status").json()["enforcement"] == "target"


def test_a_console_key_wins_over_the_file(gh, clock, tmp_path):
    bad = tmp_path / "bad.pem"
    bad.write_text("garbage")
    adapter = GitHubAdapter(GitHubAPI(transport=gh.transport(), clock=clock),
                            key_path=str(bad), clock=clock)
    adapter.bind_secrets(_Slot({"app_id": APP_CONFIG["app_id"],
                                "private_key_pem": PRIVATE_KEY_PEM}))
    assert adapter.connection.mode() == "app"
    assert adapter.connection._jwt()


class _Slot(dict):
    def set(self, name, value):
        if value in (None, ""):
            self.pop(name, None)
        else:
            self[name] = value

    def wipe(self):
        self.clear()


# ---- the install flow -----------------------------------------------------------------

def test_start_returns_the_install_url_with_a_state(client):
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    out = start(client)
    assert out["kind"] == "install"
    assert out["url"] == (f"https://github.com/apps/{APP_SLUG}/installations/new"
                          f"?state={out['state']}")
    assert len(out["state"]) >= 30


def test_start_needs_a_slug(client):
    configure(client, {"app_id": APP_CONFIG["app_id"]}, {"private_key_pem": PRIVATE_KEY_PEM})
    assert client.post("/connect/start", json={}).status_code == 400


def test_finish_verifies_the_installation_with_the_app_jwt(client, gh):
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    s = start(client)
    r = finish(client, installation_id=INSTALLATION_ID, state=s["state"])
    assert r.status_code == 200
    assert r.json()["account"] == "octo"                  # lowercased from "Octo"
    assert gh.calls("GET", f"/app/installations/{INSTALLATION_ID}")
    status = client.get("/status").json()
    assert (status["connected"], status["enforcement"]) == (True, "target")


def test_state_is_required_single_use_and_expires(client, clock):
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    s = start(client)
    assert finish(client, installation_id=INSTALLATION_ID).status_code == 400   # missing
    # The failed attempt consumed the nonce: even the right one is now refused.
    assert finish(client, installation_id=INSTALLATION_ID, state=s["state"]).status_code == 400
    s = start(client)
    assert finish(client, installation_id=INSTALLATION_ID, state="guess").status_code == 400
    s = start(client)
    clock.advance(601)
    assert finish(client, installation_id=INSTALLATION_ID, state=s["state"]).status_code == 400
    s = start(client)
    assert finish(client, installation_id=INSTALLATION_ID, state=s["state"]).status_code == 200
    assert finish(client, installation_id=INSTALLATION_ID, state=s["state"]).status_code == 400
    assert client.get("/status").json()["connected"] is True


@pytest.mark.parametrize("inst", ["abc", "1/../2", "", "9" * 21])
def test_finish_refuses_a_malformed_installation_id(client, gh, inst):
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    s = start(client)
    assert finish(client, installation_id=inst, state=s["state"]).status_code == 400
    assert not gh.calls("GET", "/app/")


def test_finish_refuses_an_installation_github_does_not_know(client):
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    s = start(client)
    r = finish(client, installation_id="424242", state=s["state"])
    assert r.status_code == 400
    assert client.get("/status").json()["connected"] is False


def test_finish_with_the_wrong_key_is_not_connected(client):
    configure(client, APP_CONFIG, {"private_key_pem": OTHER_KEY_PEM})
    s = start(client)
    r = finish(client, installation_id=INSTALLATION_ID, state=s["state"])
    assert r.status_code == 503                    # GitHub refused the App JWT
    assert client.get("/status").json()["connected"] is False


def test_finish_refuses_an_installation_of_another_app(client, gh):
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    s = start(client)
    original = gh._get_installation

    def other_app(*args):
        body = original(*args).json()
        return httpx.Response(200, json={**body, "app_id": 999})
    gh._get_installation = other_app
    assert finish(client, installation_id=INSTALLATION_ID, state=s["state"]).status_code == 400


def test_pat_mode_has_nothing_to_install(pat_mode):
    assert start(pat_mode) == {"kind": "none"}
    assert finish(pat_mode).json() == {"ok": True, "mode": "pat"}


def test_the_code_parameter_is_ignored(client):
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    s = start(client)
    r = finish(client, installation_id=INSTALLATION_ID, state=s["state"], code="user-code")
    assert r.status_code == 200


# ---- status -----------------------------------------------------------------------

def test_status_when_github_is_down_keeps_mode_and_enforcement(app_mode, gh):
    gh.fail("GET", r"/app/installations/", "connect")
    s = app_mode.get("/status").json()
    assert (s["connected"], s["healthy"], s["enforcement"]) == (True, False, "target")


def test_status_after_an_uninstall_is_not_connected(app_mode, gh):
    del gh.installations[INSTALLATION_ID]
    s = app_mode.get("/status").json()
    assert s["connected"] is False and "reconnect" in s["health"]


def test_status_in_pat_mode(pat_mode, gh):
    s = pat_mode.get("/status").json()
    assert (s["connected"], s["healthy"], s["mode"], s["enforcement"]) == (
        True, True, "pat", "proxy")
    gh.pat = "ghp_rotated"                   # GitHub no longer accepts ours
    s = pat_mode.get("/status").json()
    assert (s["connected"], s["healthy"]) == (False, False)


def test_status_with_an_invalid_key_file(gh, clock, tmp_path):
    bad = tmp_path / "bad.pem"
    bad.write_text("garbage")
    adapter = GitHubAdapter(GitHubAPI(transport=gh.transport(), clock=clock),
                            key_path=str(bad), clock=clock)
    adapter.bind_secrets(_Slot({"app_id": APP_CONFIG["app_id"]}))
    s = adapter.status()
    assert s["connected"] is False and "invalid" in s["health"]


# ---- disconnect -------------------------------------------------------------------

def test_disconnect_forgets_the_installation_and_every_token(app_mode, gh, app):
    assert app_mode.post("/perform", json=GET_FILE).status_code == 200
    minted = len(gh.token_requests())
    assert app_mode.post("/disconnect").json() == {"ok": True}
    assert app.state.secret_store.read_all("github_app") == {}
    s = app_mode.get("/status").json()
    assert s["connected"] is False
    r = app_mode.post("/perform", json=GET_FILE)
    assert r.status_code == 503
    assert len(gh.token_requests()) == minted          # the cached token is gone too
    # The App's own config stays: reconnecting needs no new key upload.
    assert app.state.secret_store.read_all("github")["private_key_pem"] == PRIVATE_KEY_PEM
    install(app_mode)
    assert app_mode.post("/perform", json=GET_FILE).status_code == 200


def test_disconnect_in_pat_mode_wipes_the_pat(pat_mode, app):
    pat_mode.post("/disconnect")
    assert "pat" not in app.state.secret_store.read_all("github")
    assert pat_mode.get("/status").json()["connected"] is False
    assert PAT not in str(app.state.secret_store.read_all("github"))


def test_installation_state_cannot_be_planted_through_configure(client, app):
    # /configure writes only the config slot; the installation lives in a
    # separate slot that only the connect flow writes.
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM,
                                   "installation_id": INSTALLATION_ID})
    assert app.state.secret_store.read_all("github_app") == {}
    assert client.get("/status").json()["connected"] is False
