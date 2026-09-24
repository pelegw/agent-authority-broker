"""Fixtures for the WhatsApp plugin: a seeded archive, the scripted sidecar,
the adapter, and the adapter served by the real plugin runtime.

Most tests go through the runtime over `TestClient` (`perform(...)`), so the
JSON scope, the error mapping and the binary encoding are the ones the broker
actually sees.
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

from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from aab_plugin_runtime import serve  # noqa: E402
from aab_plugin_whatsapp.adapter import WhatsAppAdapter  # noqa: E402
from aab_plugin_whatsapp.archive import Archive  # noqa: E402

from .fakes import FakeSidecar, seed_archive  # noqa: E402

PLUGIN_TOKEN = "whatsapp-plugin-token-0123456789abcdef"


@pytest.fixture()
def archive_path(tmp_path):
    return tmp_path / "messages.db"


@pytest.fixture()
def archive(archive_path):
    seed_archive(archive_path)
    return Archive(str(archive_path))


@pytest.fixture()
def sidecar():
    return FakeSidecar()


@pytest.fixture()
def adapter(archive, sidecar):
    return WhatsAppAdapter(sidecar.client(), archive)


@pytest.fixture()
def app(adapter, tmp_path):
    return serve([adapter], PLUGIN_TOKEN, tmp_path / "secrets", Fernet.generate_key().decode())


@pytest.fixture()
def client(app):
    # raise_server_exceptions=False: a 5xx must come back as a response, as
    # it would over the network, so the error mapping can be asserted.
    return TestClient(app, headers={"X-Plugin-Token": PLUGIN_TOKEN},
                      raise_server_exceptions=False)


def scope(deny=(), allow_only=None, contact_deny=()) -> dict:
    """A CallScope as the broker sends it (chat visibility, optional contact denies)."""
    vis = {"chat": {"deny": list(deny), "allow_only": allow_only}}
    if contact_deny:
        vis["contact"] = {"deny": list(contact_deny), "allow_only": None}
    return {"request_id": "test-request", "visibility": vis, "constraints": {},
            "credential": {}}


@pytest.fixture()
def perform(client):
    """perform(action, params, scope=None) -> the runtime's HTTP response."""
    def _perform(action, params=None, call_scope=None):
        return client.post("/perform", json={"action": action, "params": params or {},
                                             "scope": call_scope or scope()})
    return _perform


def items(response) -> list[dict]:
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"]
