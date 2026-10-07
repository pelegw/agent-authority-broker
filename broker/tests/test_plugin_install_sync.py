"""Hot plugins: the running broker applies the installer's GET /services
(services/plugin_install.py: reconcile_services, the job hooks, services_tick
and the lifespan), against the fake installer of test_plugin_install.py and
the echo plugin over the real plugin runtime.

What is pinned down here: a service the installer lists is registered and
discovered without a broker restart; one it stops listing is evicted (agents
get 404) only after a successful fetch; an installer that is off, down or
answering garbage (reserved names included) leaves the registry alone and is
logged once, not on every tick; a job seen ending (through the console's job
route or the broker's own polling) triggers the reconcile and, for an
install or upgrade, a new discovery of the service, retried every tick while
the new container boots; a failed upgrade's restored container stops being
served under the moved pin; the purge-then-reinstall sequence with no deploy
in between uses the installer's new token (an agent call succeeds); the
lifespan applies the list at boot; and no token reaches a log line."""

import logging
import types

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aab_plugin_runtime import serve
from broker.plugins import pins, registry
from broker.plugins.registry import get_registry
from broker.services import plugin_install

from .conftest import cap, enable_plugin
from .test_plugin_install import (ECHO_TEXT, INSTALLER_TOKEN, INSTALLER_URL, JOB_ID,
                                  FakeInstaller, echo_text, install)

TOKEN_A = "a1" * 32            # 64 hex: the shape of a generated plugin token
TOKEN_B = "b2" * 32
URL = "http://plugin-echo:8090"
ACT = "/v1/targets/echo/actions"


def item(service: str = "echo", token: str = TOKEN_A, url: str | None = None) -> dict:
    return {"service": service, "url": url or f"http://plugin-{service}:8090", "token": token}


@pytest.fixture()
def sync(env, monkeypatch, tmp_path, echo_impl):
    """The installer configured (the fake), echo an external plugin, and the
    echo container behind http://plugin-echo:8090: `plugin(token)` (re)starts
    it with that token, `plugin(None)` stops it (connection refused)."""
    from broker.config import get_settings
    monkeypatch.setenv("INSTALLER_URL", INSTALLER_URL)
    monkeypatch.setenv("INSTALLER_TOKEN", INSTALLER_TOKEN)
    get_settings.cache_clear()
    registry.reset_registry(registry.Registry(vendored_dirs=(tmp_path / "no-tree",)))
    fake = FakeInstaller()
    ns = types.SimpleNamespace(fake=fake, impl=echo_impl, app=None, factory_calls=0)

    def installer_client(base_url, headers, timeout):
        assert base_url == INSTALLER_URL
        if ns.installer_down:
            raise httpx.ConnectError("connection refused")
        return TestClient(fake.app, base_url=base_url, headers=headers)

    ns.installer_down = False
    monkeypatch.setattr(plugin_install, "client_factory", installer_client)
    counter = iter(range(1000))

    def plugin(token: str | None = TOKEN_A) -> None:
        ns.app = None if token is None else serve(
            [echo_impl], token, tmp_path / f"echo-secrets-{next(counter)}",
            Fernet.generate_key().decode())

    def container(base_url, headers, timeout):
        assert base_url == URL                     # the broker never goes anywhere else
        if ns.app is None:
            raise httpx.ConnectError("connection refused")
        return TestClient(ns.app, base_url=base_url, headers=headers,
                          raise_server_exceptions=False)

    ns.plugin = plugin
    plugin(TOKEN_A)
    get_registry().discover({}, client_factory=container)
    return ns


def pin(version: str = "0.1.0") -> None:
    pins.set("echo", echo_text(version) if version != "0.1.0" else ECHO_TEXT, by="owner")


def warnings_about_services(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if "installer services not applied" in r.getMessage()]


# ---- reconcile: register, evict ----------------------------------------------------------------

def test_a_listed_service_is_registered_without_a_restart(sync):
    pin()
    sync.fake.services = [item()]
    out = plugin_install.reconcile_services()
    assert out == {"services": ["echo"], "discovered": ["echo"], "removed": []}
    reg = get_registry()
    assert reg.service_of("echo") == "echo"
    assert reg.services()["echo"] == (URL, TOKEN_A)
    assert reg.plugin_rows()["echo"]["enabled"] == 0          # a new plugin starts disabled
    # Nothing changed: the next reconcile discovers nothing again.
    assert plugin_install.reconcile_services()["discovered"] == []


def test_a_service_the_installer_stops_listing_is_evicted(sync, client, make_agent):
    pin()
    sync.fake.services = [item()]
    plugin_install.reconcile_services()
    enable_plugin()
    agent = make_agent([cap(["list_items"])])
    assert client.post(f"{ACT}/list_items", json={"params": {}},
                       headers=agent.headers).status_code == 200
    sync.fake.services = []
    out = plugin_install.reconcile_services()
    assert out["removed"] == ["echo"] and out["services"] == []
    reg = get_registry()
    assert "echo" not in reg.entries() and "echo" not in reg.services()
    # Agents lose it at once (hidden == missing == 404); its row stays.
    r = client.post(f"{ACT}/list_items", json={"params": {}}, headers=agent.headers)
    assert r.status_code == 404
    assert "echo" in reg.plugin_rows()


def test_an_installer_that_is_down_changes_nothing_and_is_logged_once(sync, caplog):
    caplog.set_level(logging.INFO)
    pin()
    sync.fake.services = [item()]
    plugin_install.reconcile_services()
    sync.installer_down = True
    assert plugin_install.reconcile_services() is None
    assert plugin_install.reconcile_services() is None
    plugin_install.services_tick()
    reg = get_registry()
    assert reg.dynamic_services() == ["echo"] and reg.service_of("echo") == "echo"
    [line] = warnings_about_services(caplog)
    assert "installer_down" in line
    sync.installer_down = False
    assert plugin_install.reconcile_services() is not None
    assert "installer services applied again" in caplog.text


@pytest.mark.parametrize("body", [
    {"items": "nope"},
    {"nothing": []},
    {"items": [5]},
    {"items": [{"service": "echo"}]},
    {"items": [{**item(), "extra": 1}]},
    {"items": [item(), item()]},
    {"items": [item(service="Echo")]},
    {"items": [item(url="http://evil.example:8090")]},
    {"items": [item(url="http://user:" + TOKEN_B + "@plugin-echo:8090")]},
    {"items": [item(token="short")]},
    {"items": [item(token="has spaces in it, quite a few")]},
    {"items": [item(token=7)]},
    [item()],
])
def test_garbage_never_evicts_a_service(sync, caplog, body):
    caplog.set_level(logging.DEBUG)
    pin()
    sync.fake.services = [item()]
    plugin_install.reconcile_services()
    sync.fake.raw["/services"] = body
    assert plugin_install.reconcile_services() is None
    assert plugin_install.reconcile_services() is None
    reg = get_registry()
    assert reg.dynamic_services() == ["echo"] and reg.service_of("echo") == "echo"
    assert len(warnings_about_services(caplog)) == 1
    assert TOKEN_A not in caplog.text and TOKEN_B not in caplog.text


@pytest.mark.parametrize("name", ["whatsapp", "github", "google", "installer"])
def test_a_reserved_service_name_is_refused_and_logged_once(sync, caplog, name):
    """The installer can never redirect an in-tree service: a list naming
    one is refused whole, by the registry itself, and logged once."""
    caplog.set_level(logging.DEBUG)
    pin()
    sync.fake.services = [item(), item(service=name)]
    assert plugin_install.reconcile_services() is None
    assert plugin_install.reconcile_services() is None
    assert get_registry().dynamic_services() == []
    [line] = warnings_about_services(caplog)
    assert name in line and "a name the stack itself uses" in line
    with pytest.raises(ValueError, match="a name the stack itself uses"):
        get_registry().set_dynamic_services({name: (f"http://plugin-{name}:8090", TOKEN_A)})
    assert TOKEN_A not in caplog.text


def test_without_an_installer_nothing_is_asked(env, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin_install, "client_factory",
                        lambda *a: calls.append(a) or pytest.fail("no call expected"))
    assert plugin_install.reconcile_services() is None
    plugin_install.services_tick()
    assert calls == [] and get_registry().dynamic_services() == []


# ---- job ends ----------------------------------------------------------------------------------

def done_job(kind: str = "install", state: str = "done", **fields) -> dict:
    return {"id": JOB_ID, "kind": kind, "state": state, "service": "echo", "source": None,
            "ref": None, "commit": None, "purge": False, "created_at": 1, "started_at": 1,
            "finished_at": 2, "error": None, "log": [], "log_truncated": False, **fields}


def test_a_job_seen_done_through_the_route_triggers_the_reconcile(sync, client, admin_headers):
    pin()
    sync.fake.jobs[JOB_ID] = done_job()
    sync.fake.services = [item()]
    r = client.get(f"/v1/admin/plugins/install/jobs/{JOB_ID}", headers=admin_headers)
    assert r.status_code == 200 and r.json()["state"] == "done"
    # The route made no call of its own beyond the job: the loop applies it.
    assert sync.fake.calls("/services") == []
    plugin_install.services_tick()
    assert get_registry().service_of("echo") == "echo"
    assert len(sync.fake.calls("/services")) == 1
    # Seen again: acted on once only.
    client.get(f"/v1/admin/plugins/install/jobs/{JOB_ID}", headers=admin_headers)
    plugin_install.services_tick()
    assert len(sync.fake.calls("/services")) == 1


def test_the_broker_follows_the_jobs_it_submitted(sync, client, admin_headers):
    """No console polling: the broker's own tick polls the job it submitted
    and applies its end."""
    r = install(client, admin_headers)                   # pins echo, then the job (queued)
    assert r.status_code == 202, r.text
    plugin_install.services_tick()                       # queued: nothing to apply yet
    assert "echo" not in get_registry().entries()
    sync.fake.jobs[JOB_ID].update(state="done", service="echo")
    sync.fake.services = [item()]
    plugin_install.services_tick()
    assert get_registry().service_of("echo") == "echo"
    assert len(sync.fake.calls(f"/jobs/{JOB_ID}")) == 2


def test_an_upgrade_job_end_discovers_the_new_container(sync):
    pin()
    sync.fake.services = [item()]
    plugin_install.reconcile_services()
    assert get_registry().manifests()["echo"].version == "0.1.0"
    # The upgrade request moved the pin; the job replaced the container.
    pin("0.2.0")
    sync.impl.manifest = {**sync.impl.manifest, "version": "0.2.0"}
    sync.plugin(TOKEN_A)
    plugin_install.job_ended(done_job("upgrade"))
    plugin_install.services_tick()
    assert get_registry().manifests()["echo"].version == "0.2.0"


def test_a_failed_upgrade_stops_serving_under_the_moved_pin(sync):
    """The installer restored the old container (offering 0.1.0) but the pin
    already says 0.2.0: the plugin is no longer served (fail closed) and its
    offer waits for the owner's review, as after a restart."""
    pin()
    sync.fake.services = [item()]
    plugin_install.reconcile_services()
    pin("0.2.0")
    plugin_install.job_ended(done_job("upgrade", state="failed", error="build failed"))
    plugin_install.services_tick()
    reg = get_registry()
    assert "echo" not in reg.entries()
    assert "mismatch" in reg.offered["echo"]["reason"]


def test_a_booting_plugin_is_retried_every_tick(sync):
    pin()
    sync.plugin(None)                                    # started, not answering yet
    sync.fake.services = [item()]
    plugin_install.job_ended(done_job())
    plugin_install.services_tick()
    reg = get_registry()
    assert "echo" not in reg.entries() and reg.pending_services() == ["echo"]
    sync.plugin(TOKEN_A)
    plugin_install.services_tick()                       # seconds later, not 30
    assert reg.service_of("echo") == "echo" and reg.pending_services() == []


def test_a_remove_job_end_evicts(sync):
    pin()
    sync.fake.services = [item()]
    plugin_install.reconcile_services()
    sync.fake.services = []
    plugin_install.job_ended(done_job("remove"))
    plugin_install.services_tick()
    assert "echo" not in get_registry().entries()


def test_an_ended_job_waits_for_a_readable_installer(sync):
    pin()
    sync.fake.services = [item()]
    plugin_install.job_ended(done_job())
    sync.installer_down = True
    plugin_install.services_tick()
    assert "echo" not in get_registry().entries()
    sync.installer_down = False
    plugin_install.services_tick()
    assert get_registry().service_of("echo") == "echo"


# ---- the merge rule: the installer's values win for what it lists --------------------------

def test_purge_then_reinstall_without_a_deploy_uses_the_new_token(sync, client, make_agent,
                                                                  monkeypatch, caplog):
    """The broker's env still holds the token of the last deploy (TOKEN_A);
    a purge and a reinstall minted TOKEN_B, which the new container
    requires and the installer lists. The installer's value wins: the
    plugin is reached, and an agent call succeeds."""
    caplog.set_level(logging.DEBUG)
    pin()
    monkeypatch.setenv("PLUGIN_URL_ECHO", URL)
    monkeypatch.setenv("PLUGIN_TOKEN_ECHO", TOKEN_A)
    sync.plugin(TOKEN_B)
    reg = get_registry()
    reg.discover()                                       # boot: env only, refused (401)
    assert "echo" not in reg.entries() and reg.pending_services() == ["echo"]
    sync.fake.services = [item(token=TOKEN_B)]
    out = plugin_install.reconcile_services()
    assert out["discovered"] == ["echo"]
    assert reg.services()["echo"] == (URL, TOKEN_B) and reg.pending_services() == []
    enable_plugin()
    agent = make_agent([cap(["list_items"])])
    r = client.post(f"{ACT}/list_items", json={"params": {}}, headers=agent.headers)
    assert r.status_code == 200, r.text
    # The DEBUG line names the service and the source that won, never a value.
    [line] = [r.getMessage() for r in caplog.records
              if "installer's values win" in r.getMessage()]
    assert "service=echo" in line and "used=installer" in line and "differs=token" in line
    assert TOKEN_A not in caplog.text and TOKEN_B not in caplog.text


def test_a_removed_service_the_env_still_names_is_evicted_without_retry_warnings(
        sync, monkeypatch, caplog):
    """A `docker restart` after a remove: the broker's env still names echo
    (with its old token), the container is gone. Once the installer answers
    without echo, echo is evicted, logged once at INFO, and no 'will retry'
    warning follows on later rediscoveries or ticks."""
    caplog.set_level(logging.DEBUG)
    pin()
    monkeypatch.setenv("PLUGIN_URL_ECHO", URL)
    monkeypatch.setenv("PLUGIN_TOKEN_ECHO", TOKEN_A)
    sync.plugin(None)                                    # removed: nothing answers
    reg = get_registry()
    reg.discover()                                       # boot: env only
    assert reg.pending_services() == ["echo"]
    caplog.clear()
    sync.fake.services = []
    out = plugin_install.reconcile_services()
    assert out == {"services": [], "discovered": [], "removed": ["echo"]}
    assert "echo" not in reg.services() and reg.pending_services() == []
    for _ in range(3):                                   # the 30 s retry and the loop
        reg._next_discovery = 0
        reg.entries()
        plugin_install.services_tick()
    removed = [r for r in caplog.records if "plugin service removed" in r.getMessage()]
    assert len(removed) == 1 and removed[0].levelno == logging.INFO
    assert "service=echo" in removed[0].getMessage()
    assert "will retry" not in caplog.text
    assert TOKEN_A not in caplog.text


def test_a_reserved_env_service_survives_a_sync_that_does_not_list_it(sync, monkeypatch,
                                                                      caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("PLUGIN_URL_WHATSAPP", "http://plugin-whatsapp:8090")
    monkeypatch.setenv("PLUGIN_TOKEN_WHATSAPP", TOKEN_B)
    reg = get_registry()
    sync.fake.services = []
    for _ in range(2):
        out = plugin_install.reconcile_services()
        assert out["removed"] == []
    assert reg.services() == {"whatsapp": ("http://plugin-whatsapp:8090", TOKEN_B)}
    assert "plugin service removed" not in caplog.text


# ---- boot ----------------------------------------------------------------------------------------

def test_the_lifespan_applies_the_installers_services_at_boot(sync):
    from broker.main import app
    pin()
    sync.fake.services = [item()]
    with TestClient(app):
        assert get_registry().dynamic_services() == ["echo"]
        assert get_registry().service_of("echo") == "echo"


def test_the_lifespan_boots_with_the_installer_down(sync):
    from broker.main import app
    sync.installer_down = True
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert get_registry().dynamic_services() == []


def test_a_failing_boot_reconcile_never_stops_the_boot(sync, monkeypatch, caplog):
    from broker.main import app

    def broken():
        raise RuntimeError("a bug, with " + TOKEN_A)

    monkeypatch.setattr(plugin_install, "reconcile_services", broken)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
    assert "installer services not applied at boot error=RuntimeError" in caplog.text
    assert TOKEN_A not in caplog.text
