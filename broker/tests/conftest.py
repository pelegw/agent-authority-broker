"""Shared fixtures: a fresh temp database per test, a TestClient, the owner
and admin credentials, the `echo` plugin (in-process and over the plugin
runtime), and an agent-key factory.

`env` is the root fixture. Owner fixtures moved here from identity/conftest.py
in phase 3 so every suite can act as the owner; names are unchanged.
"""

import logging
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# The plugin runtime is its own package at the repo root. Tests import it
# from source when it is not installed, so `pip install -e broker[dev]`
# alone is enough to run the suite.
_RUNTIME = Path(__file__).resolve().parents[2] / "plugin-runtime"
if _RUNTIME.is_dir() and str(_RUNTIME) not in sys.path:
    try:
        import aab_plugin_runtime  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(_RUNTIME))

# Logging is configured once, at collection, as broker.main does at import.
# Configuring it later (the first `from broker.main import app` inside a
# test, or a plugin app's serve()) would swap the root handlers under a
# test's running log capture; after this, those calls are no-ops.
from broker import logging_setup  # noqa: E402

logging_setup.configure("broker")

OWNER_USERNAME = "owner"
OWNER_PASSWORD = "correct horse battery staple"
CSRF_HEADERS = {"X-Requested-With": "aab-console"}
PLUGIN_TOKEN = "echo-plugin-token-0123456789abcdef"

# Exposure settings a developer's shell might carry; tests must start from the
# local, non-public default regardless.
_EXPOSURE_VARS = (
    "ORIGIN_SECRET", "ORIGIN_SECRET_HEADER", "CF_ACCESS_ENABLED",
    "CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "CF_ACCESS_ALLOWED_EMAILS",
    "ALLOW_INSECURE_ADMIN", "TRUST_CF_CONNECTING_IP", "DECISION_SIGNING_KEY",
    "INSTALLER_URL", "INSTALLER_TOKEN",       # the installer is off unless a test turns it on
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Point the app at a fresh per-test database and reset cached settings
    and every process-global the engine keeps (registry, rate limiter,
    notification providers)."""
    for var in _EXPOSURE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BROKER_DB", str(tmp_path / "broker.db"))
    # TestClient sends Host: testserver; the MCP transport must accept it.
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,testserver")
    from broker.config import get_settings
    get_settings.cache_clear()
    from broker import db, ledger, notify
    from broker.plugins.registry import reset_registry
    from broker.services import plugin_install
    db.init()
    reset_registry()
    plugin_install.reset_sync()
    ledger.rate_limiter.reset()
    monkeypatch.setattr(notify, "_PROVIDERS", [])
    yield
    reset_registry()
    plugin_install.reset_sync()
    get_settings.cache_clear()


@pytest.fixture()
def client(env):
    from broker.main import app
    return TestClient(app)


@pytest.fixture()
def logs_to_stderr(monkeypatch):
    """For tests that run the `aab` CLI and the broker in this one process:
    the CLI's stdout is its JSON output, and in production the broker's log
    lines are another process's. The CLI sends its own logging to stderr
    (cli/aab.py), but here logging was set up for the broker first."""
    for handler in logging.getLogger().handlers:
        if hasattr(handler, "stream_name"):          # logging_setup.ConsoleHandler
            monkeypatch.setattr(handler, "stream_name", "stderr")


# ---- owner ---------------------------------------------------------------------

@pytest.fixture()
def owner(env):
    """The owner principal, created directly. Yields id/username/password."""
    from broker.identity import principals
    p = principals.create_owner(OWNER_USERNAME, OWNER_PASSWORD)
    return types.SimpleNamespace(id=p.id, username=p.username, password=OWNER_PASSWORD)


@pytest.fixture()
def admin_token(owner):
    """A freshly minted aab_admin_ token (plaintext) for the owner."""
    from broker.identity import admin_tokens
    return admin_tokens.create(owner.id, "test")["token"]


@pytest.fixture()
def admin_headers(admin_token):
    """Authorization header carrying `admin_token`."""
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture()
def session_client(client, owner):
    """The shared TestClient, logged in as the owner (session cookie in its jar)."""
    r = client.post("/auth/login", json={"username": owner.username,
                                         "password": owner.password})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture()
def admin_ctx(owner):
    """An AdminContext for calling admin services directly."""
    from broker.deps import AdminContext
    return AdminContext(owner.id, owner.username, "token", "test-token-id")


# ---- the echo plugin -----------------------------------------------------------

ECHO_DIR = Path(__file__).resolve().parent / "fixtures" / "echo"


def echo_manifest():
    from broker.plugins.manifest import load_manifest
    return load_manifest(ECHO_DIR / "manifest.yaml")


def enable_plugin(plugin_id: str = "echo", connected: bool = True) -> None:
    from broker.plugins import settings
    settings.set_enabled(plugin_id, True)
    settings.set_health(plugin_id, {"connected": connected, "healthy": True}, connected)


@pytest.fixture()
def echo_impl():
    from tests.fixtures.echo.adapter import EchoAdapter
    return EchoAdapter()


def register_inprocess(impl):
    from broker.plugins.registry import get_registry
    assert get_registry().register_in_process(impl, echo_manifest())


def runtime_factory(runtime_app):
    """A RemoteAdapter client factory that talks to the runtime app in
    process (TestClient is an httpx.Client), so no socket is opened."""
    def factory(base_url, headers, timeout):
        return TestClient(runtime_app, base_url=base_url, headers=headers,
                          raise_server_exceptions=False)
    return factory


def register_remote(impl, tmp_path, token: str = PLUGIN_TOKEN):
    from aab_plugin_runtime import serve
    from cryptography.fernet import Fernet

    from broker.plugins.registry import get_registry
    runtime = serve([impl], token, tmp_path / "plugin-secrets", Fernet.generate_key().decode())
    get_registry().discover({"echosvc": ("http://plugin-echo", token)},
                            client_factory=runtime_factory(runtime))
    assert "echo" in get_registry().entries()
    return runtime


@pytest.fixture()
def vendored_echo(env):
    """Make the registry find echo's vendored manifest under tests/fixtures."""
    from broker.plugins import registry
    registry.reset_registry(registry.Registry(vendored_dirs=(ECHO_DIR.parent,)))


@pytest.fixture(params=["inprocess", "remote"])
def echo(request, env, owner, echo_impl, tmp_path, vendored_echo):
    """The echo plugin, enabled and connected, over BOTH transports: tests
    using this fixture run once in-process and once through the plugin
    runtime over HTTP (RemoteAdapter -> serve())."""
    if request.param == "inprocess":
        register_inprocess(echo_impl)
    else:
        register_remote(echo_impl, tmp_path)
    enable_plugin()
    return types.SimpleNamespace(impl=echo_impl, transport=request.param)


@pytest.fixture()
def echo_local(env, owner, echo_impl, vendored_echo):
    """The echo plugin in-process only (for tests where transport is moot)."""
    register_inprocess(echo_impl)
    enable_plugin()
    return types.SimpleNamespace(impl=echo_impl, transport="inprocess")


# ---- agent keys -------------------------------------------------------------------

@pytest.fixture()
def make_agent(owner):
    """make_agent(caps, role="full", rate=60, denies=None, parent=None) ->
    namespace(key_id, plaintext, headers, auth, grant_id). `caps` are
    capability dicts; a root key gets them as an active root grant."""
    from broker import auth
    from broker.authority import store
    from broker.authority.capability import from_json, normalize_all
    from broker.plugins.registry import get_registry

    counter = iter(range(1, 10_000))

    def make(caps=None, role="full", rate=60, denies=None, name=None, expires_at=None):
        new = auth.create_key(owner.id, name or f"agent-{next(counter)}", role, rate,
                              expires_at, denies=denies)
        grant_id = None
        if caps:
            normalized = normalize_all([from_json(c) for c in caps], get_registry().manifests())
            grant_id = store.insert_root_grant(owner.id, new.key_id, normalized, "active",
                                               "test", None, owner.username,
                                               decided_via="token").id
        return types.SimpleNamespace(
            key_id=new.key_id, plaintext=new.plaintext, grant_id=grant_id,
            headers={"Authorization": f"Bearer {new.plaintext}"},
            auth=auth.authenticate_bearer(f"Bearer {new.plaintext}"))
    return make


def cap(actions, **kw):
    """Shorthand for an echo capability dict."""
    return {"target": "echo", "actions": list(actions), **kw}


# ---- broker secrets and Telegram ---------------------------------------------------

TELEGRAM_TOKEN = "123456789:AAtesttokentesttokentesttokentest01"
TELEGRAM_CHAT = "4242"
TELEGRAM_USER = "4242"      # private chat: chat id == user id


@pytest.fixture(autouse=True)
def _reset_telegram_globals(monkeypatch):
    """Telegram keeps in-process state (pending link code, cached bot
    username, poll health) and the broker secrets key may be in a
    developer's shell; start every test from none of it."""
    monkeypatch.delenv("BROKER_SECRETS_KEY", raising=False)
    from broker.config import get_settings
    get_settings.cache_clear()
    yield
    from broker.notify import telegram as tg
    tg._link = None
    tg._bot_username = None
    tg._poll.update(running=False, last_ok_at=None, last_error=None, consecutive_errors=0)


@pytest.fixture()
def secrets_key(env, monkeypatch):
    """A fresh BROKER_SECRETS_KEY for the broker-side secret store."""
    from cryptography.fernet import Fernet

    from broker.config import get_settings
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BROKER_SECRETS_KEY", key)
    get_settings.cache_clear()
    return key


@pytest.fixture()
def fake_telegram(env, owner, secrets_key, monkeypatch):
    """Telegram fully live (token stored, enabled, owner's chat linked) with
    the low-level HTTP ops replaced by a recording double. `inject(update)`
    queues updates that `_get_updates` drains FIFO (the WA_GW pattern)."""
    from broker import crypto, db
    from broker.notify import telegram as tg
    crypto.put(crypto.BROKER_SLOT, tg.TOKEN_NAME, TELEGRAM_TOKEN)
    db.set_config(tg.CFG_BOT_ID, TELEGRAM_TOKEN.split(":")[0])
    db.set_config(tg.CFG_ENABLED, "1")
    db.set_config(tg.CFG_CHAT, TELEGRAM_CHAT)
    db.set_config(tg.CFG_USER, TELEGRAM_USER)
    db.set_config(tg.CFG_PRINCIPAL, owner.id)

    rec = {"sent": [], "answered": [], "edited": [], "api": [], "_updates": []}

    def send_message(text, keyboard=None):
        rec["sent"].append({"text": text, "keyboard": keyboard})
        return {"message_id": len(rec["sent"])}

    def get_updates(offset, timeout):
        out, rec["_updates"] = rec["_updates"], []
        return out

    def api(method, _http_timeout=15.0, **payload):
        rec["api"].append(method)          # no network (deleteWebhook etc.)
        return {}

    monkeypatch.setattr(tg, "_api", api)
    monkeypatch.setattr(tg, "_api_send_message", send_message)
    monkeypatch.setattr(tg, "_answer_callback",
                        lambda cb_id, text="": rec["answered"].append({"cb": cb_id, "text": text}))
    monkeypatch.setattr(tg, "_edit_message",
                        lambda mid, text: rec["edited"].append({"mid": mid, "text": text}))
    monkeypatch.setattr(tg, "_get_updates", get_updates)
    monkeypatch.setattr(tg, "_get_me", lambda: "aab_test_bot")
    rec["inject"] = lambda u: rec["_updates"].append(u)
    return rec
