"""Fixtures for real target plugins, served through the plugin runtime.

The WhatsApp plugin is its own package (plugins/whatsapp, its own container
in production). These tests import it from source when it is not installed,
load its test doubles (plugins/whatsapp/tests/fakes.py) by path, and register
the real plugin app with the broker exactly as discovery does in production:
`Registry.discover` fetches `/manifests` over HTTP, pins the result against
the vendored broker/broker/targets/whatsapp/manifest.yaml, and wraps the
service in a `RemoteAdapter`. The HTTP hop is a TestClient bound to the
runtime app (the same `runtime_factory` the echo remote tests use), so no
socket is opened.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WHATSAPP_DIR = REPO / "plugins" / "whatsapp"
try:
    import aab_plugin_whatsapp  # noqa: F401
except ImportError:
    sys.path.insert(0, str(WHATSAPP_DIR))

WA_TOKEN = "whatsapp-plugin-token-for-broker-tests-0123"


def _load_fakes():
    spec = importlib.util.spec_from_file_location(
        "aab_whatsapp_test_fakes", WHATSAPP_DIR / "tests" / "fakes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fakes = _load_fakes()


def register_whatsapp(adapter, tmp_path, token: str = WA_TOKEN):
    """Serve `adapter` through the real runtime and let the registry discover
    it as service `whatsapp`. Mirrors tests.conftest.register_remote (which
    is fixed to the echo plugin and its service name)."""
    from aab_plugin_runtime import serve
    from cryptography.fernet import Fernet

    from broker.plugins.registry import get_registry
    from tests.conftest import runtime_factory

    runtime = serve([adapter], token, tmp_path / "wa-plugin-secrets",
                    Fernet.generate_key().decode())
    get_registry().discover({"whatsapp": ("http://plugin-whatsapp:8090", token)},
                            client_factory=runtime_factory(runtime))
    assert "whatsapp" in get_registry().entries(), get_registry().refused
    return runtime


@pytest.fixture()
def wa_disabled(env, owner, tmp_path):
    """The real WhatsApp plugin over a seeded archive and the fake sidecar,
    registered (pinned) but not yet enabled."""
    from aab_plugin_whatsapp.adapter import WhatsAppAdapter
    from aab_plugin_whatsapp.archive import Archive

    archive_path = tmp_path / "messages.db"
    fakes.seed_archive(archive_path)
    sidecar = fakes.FakeSidecar()
    adapter = WhatsAppAdapter(sidecar.client(), Archive(str(archive_path)))
    runtime = register_whatsapp(adapter, tmp_path)
    return types.SimpleNamespace(adapter=adapter, sidecar=sidecar, archive=archive_path,
                                 runtime=runtime)


@pytest.fixture()
def wa(wa_disabled):
    """The WhatsApp plugin, enabled and connected."""
    from tests.conftest import enable_plugin
    enable_plugin("whatsapp")
    return wa_disabled


def wa_cap(actions, **kw):
    """Shorthand for a whatsapp capability dict."""
    return {"target": "whatsapp", "actions": list(actions), **kw}
