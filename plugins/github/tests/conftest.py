"""Fixtures for the GitHub plugin: the fake GitHub, the adapter, and the
adapter served by the real plugin runtime.

Most tests go through the runtime over `TestClient` (`perform(...)`), so the
JSON scope, the error mapping and the secret store are the ones the broker
actually sees. `scope_for()` builds the CallScope the broker would send for
an action, with the credential requirements taken from the manifest.
"""

import sys
from pathlib import Path

import pytest

# The plugin runtime is its own package at the repo root. Tests import it
# from source when it is not installed (same fallback as the broker suite).
_RUNTIME = Path(__file__).resolve().parents[3] / "plugin-runtime"
try:
    import aab_plugin_runtime  # noqa: F401
except ImportError:
    sys.path.insert(0, str(_RUNTIME))

import yaml  # noqa: E402
from aab_plugin_runtime import logging_setup  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from aab_plugin_github.adapter import MANIFEST_PATH, GitHubAdapter  # noqa: E402
from aab_plugin_github.api import GitHubAPI  # noqa: E402
from aab_plugin_github.main import create_app  # noqa: E402

from .fakes import (APP_ID, APP_SLUG, INSTALLATION_ID, PAT, PRIVATE_KEY_PEM,  # noqa: E402
                    Clock, FakeGitHub)

# Logging is configured once, at collection, as the plugin process does when
# it builds its app: serve() inside a test then leaves the handlers alone
# instead of swapping them under a running log capture.
logging_setup.configure("plugin-github")

PLUGIN_TOKEN = "github-plugin-token-0123456789abcdef"
MANIFEST = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
NEEDS = {a["name"]: a["target_permissions"] for a in MANIFEST["actions"]}
APP_CONFIG = {"app_id": APP_ID, "app_slug": APP_SLUG}


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def gh(clock):
    return FakeGitHub(clock)


@pytest.fixture()
def adapter(gh, clock):
    return GitHubAdapter(GitHubAPI(transport=gh.transport(), clock=clock), clock=clock)


@pytest.fixture()
def app(adapter, tmp_path):
    return create_app({"PLUGIN_TOKEN": PLUGIN_TOKEN,
                       "PLUGIN_SECRETS_KEY": Fernet.generate_key().decode(),
                       "PLUGIN_SECRETS_DIR": str(tmp_path / "secrets")}, adapter=adapter)


@pytest.fixture()
def client(app):
    # raise_server_exceptions=False: a 5xx must come back as a response, as
    # it would over the network, so the error mapping can be asserted.
    return TestClient(app, headers={"X-Plugin-Token": PLUGIN_TOKEN},
                      raise_server_exceptions=False)


def configure(client, config=None, secrets=None):
    r = client.post("/configure", json={"config": config or {}, "secrets": secrets or {}})
    assert r.status_code == 200, r.text
    return r


def install(client, installation_id: str = INSTALLATION_ID):
    start = client.post("/connect/start", json={"enabled_plugins": ["github"]})
    assert start.status_code == 200, start.text
    r = client.post("/connect/finish", json={"installation_id": installation_id,
                                             "state": start.json()["state"]})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def app_mode(client):
    """Configured with the App and installed: enforcement "target"."""
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    install(client)
    return client


@pytest.fixture()
def pat_mode(client):
    """No App, a PAT only: enforcement "proxy"."""
    configure(client, {}, {"pat": PAT})
    return client


def scope_for(action: str, *, repos=None, deny=(), allow=None, branch_deny=(),
              branch_allow=None, permissions=None) -> dict:
    """The CallScope the broker sends: credential requirements = the action's
    target_permissions (+ the capability's repo list), visibility per kind."""
    cred = {"permissions": dict(permissions if permissions is not None else NEEDS[action])}
    if repos is not None:
        cred["resources"] = {"repo": list(repos)}
    vis = {}
    if deny or allow is not None:
        vis["repo"] = {"deny": list(deny), "allow_only": None if allow is None else list(allow)}
    if branch_deny or branch_allow is not None:
        vis["branch"] = {"deny": list(branch_deny),
                         "allow_only": None if branch_allow is None else list(branch_allow)}
    return {"request_id": "test-request", "visibility": vis, "constraints": {},
            "credential": cred}


@pytest.fixture()
def perform(client):
    """perform(action, params, scope=None) -> the runtime's HTTP response."""
    def _perform(action, params=None, call_scope=None, **scope_kw):
        return client.post("/perform", json={
            "action": action, "params": params or {},
            "scope": call_scope if call_scope is not None else scope_for(action, **scope_kw)})
    return _perform


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def items(response) -> list[dict]:
    return data(response)["items"]
