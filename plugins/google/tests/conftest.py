"""Fixtures for the Google plugin: the fake Google, a controllable clock, the
three adapters over one connection, and all of it served by the real plugin
runtime.

Most tests go through the runtime over `TestClient` (`perform(...)`), so the
JSON scope, the error mapping and the binary encoding are the ones the
broker actually sees. `scope_for(plugin, action, ...)` builds a CallScope
exactly as the broker would: the credential requirements are the action's
own `target_permissions` from the packaged manifest.
"""

import sys
from pathlib import Path

import pytest
import yaml

# The plugin runtime is its own package at the repo root. Tests import it
# from source when it is not installed (same fallback as the broker suite).
_RUNTIME = Path(__file__).resolve().parents[3] / "plugin-runtime"
try:
    import aab_plugin_runtime  # noqa: F401
except ImportError:
    sys.path.insert(0, str(_RUNTIME))

from aab_plugin_runtime import logging_setup  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from aab_plugin_google.main import build_adapters  # noqa: E402
from aab_plugin_runtime import serve  # noqa: E402

from . import fake_google as fg  # noqa: E402

# Logging is configured once, at collection, as the plugin process does when
# it builds its app: serve() inside a test then leaves the handlers alone
# instead of swapping them under a running log capture.
logging_setup.configure("plugin-google")

PLUGIN_TOKEN = "google-plugin-token-0123456789abcdef"
NOW = 1_790_000_000.0                  # a fixed "now" (2026-09-21)
REDIRECT = "http://localhost:8080/oauth/callback/google"
MANIFESTS = Path(__file__).resolve().parents[1] / "aab_plugin_google" / "manifests"


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def manifest(plugin: str) -> dict:
    return yaml.safe_load((MANIFESTS / f"{plugin}.yaml").read_text(encoding="utf-8"))


def requirements(plugin: str, action: str) -> dict:
    act = next(a for a in manifest(plugin)["actions"] if a["name"] == action)
    return {"permissions": dict(sorted(act.get("target_permissions", {}).items()))}


def scope_for(plugin: str, action: str, visibility=None, constraints=None) -> dict:
    """A CallScope as the broker sends it for `action`."""
    return {"request_id": "test-request", "visibility": visibility or {},
            "constraints": constraints or {}, "credential": requirements(plugin, action)}


def vis(deny=(), allow=None) -> dict:
    return {"deny": list(deny), "allow_only": None if allow is None else list(allow)}


@pytest.fixture()
def google():
    return fg.FakeGoogle(now=NOW)


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def adapters(google, clock):
    return build_adapters(transport=google.transport(), clock=clock)


@pytest.fixture()
def connection(adapters):
    return adapters[0].connection


@pytest.fixture()
def app(adapters, tmp_path):
    return serve(adapters, PLUGIN_TOKEN, tmp_path / "secrets", Fernet.generate_key().decode())


@pytest.fixture()
def client(app):
    # raise_server_exceptions=False: a 5xx must come back as a response, as
    # it would over the network, so the error mapping can be asserted.
    return TestClient(app, headers={"X-Plugin-Token": PLUGIN_TOKEN},
                      raise_server_exceptions=False)


def configure(client, plugin: str = "gmail") -> None:
    r = client.post("/configure", headers={"X-Plugin-Id": plugin}, json={
        "config": {"client_id": fg.CLIENT_ID}, "secrets": {"client_secret": fg.CLIENT_SECRET}})
    assert r.status_code == 200, r.text


def connect(client, google, enabled=("gmail", "gcal", "gdrive")) -> dict:
    """Configure the OAuth client and run the whole consent round trip."""
    configure(client)
    google.expected_redirect = REDIRECT
    start = client.post("/connect/start", headers={"X-Plugin-Id": "gmail"},
                        json={"enabled_plugins": list(enabled), "redirect_uri": REDIRECT})
    assert start.status_code == 200, start.text
    state = _query(start.json()["url"])["state"]
    fin = client.post("/connect/finish", headers={"X-Plugin-Id": "gmail"},
                      json={"code": fg.AUTH_CODE, "state": state})
    assert fin.status_code == 200, fin.text
    return fin.json()


def _query(url: str) -> dict:
    from urllib.parse import parse_qs, urlsplit
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


@pytest.fixture()
def connected(client, google):
    connect(client, google)
    return client


@pytest.fixture()
def perform(connected):
    """perform(plugin, action, params, visibility=None, constraints=None) -> response."""
    def _perform(plugin, action, params=None, visibility=None, constraints=None):
        return connected.post("/perform", headers={"X-Plugin-Id": plugin}, json={
            "action": action, "params": params or {},
            "scope": scope_for(plugin, action, visibility, constraints)})
    return _perform


def items(response) -> list[dict]:
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"]
