"""The broker's half of the plugin installer (services/plugin_install.py,
routers/admin_install.py), against a fake installer served in process.

What is pinned down here: the routes and their guard; the review card built
from the installer's inspect answer; pins written BEFORE the installer is
asked to install or upgrade (the fake checks it at the moment it is called)
and put back when it refuses; unpins AFTER a remove was accepted; every
mutation audited under the owner; an installer that is off or down answers
503 saying so; installer refusals relayed with their status and code; job
and installed passthrough; and INSTALLER_TOKEN, sent as a header, in no
response, audit row or log line."""

import json
import logging

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from broker import db
from broker.plugins import pins, registry
from broker.plugins.registry import get_registry
from broker.services import plugin_install

from .conftest import ECHO_DIR, PLUGIN_TOKEN, cap, enable_plugin, runtime_factory

INSTALLER_URL = "http://aab-installer:8070"
INSTALLER_TOKEN = "installer-test-token-feedfacecafebeef0123456789abcdef"
SOURCE = "github.com/acme/aab-plugin-echo"
V1, V2 = "a" * 40, "b" * 40
JOB_ID = "c" * 32
ECHO_TEXT = (ECHO_DIR / "manifest.yaml").read_text(encoding="utf-8")
REAL_CALL = plugin_install._call            # before any test patches it


def echo_text(version: str = "0.1.0", **changes) -> str:
    data = yaml.safe_load(ECHO_TEXT)
    data.update(version=version, **changes)
    return yaml.safe_dump(data, sort_keys=False)


DESCRIPTOR = {"schema": 1, "service": "echo", "plugins": ["echo"],
              "manifests": ["aab_plugin_echo/manifest.yaml"], "runtime": "0.3",
              "build": {"dockerfile": "Dockerfile"}, "volumes": {"echo_data": "/data"},
              "environment": {"ECHO_DB": "/data/echo.db"}, "env_passthrough": ["TZ"]}


class FakeInstaller:
    """The installer's HTTP contract (installer/aab_installer/app.py) in
    memory. `refs` maps ref -> (commit, manifest text); `fail[path]` makes a
    route answer (status, code); `seen` records every call with the pins
    that existed when it arrived."""

    def __init__(self):
        self.refs = {"v0.1.0": (V1, echo_text("0.1.0")), "v0.2.0": (V2, echo_text("0.2.0"))}
        self.records: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.fail: dict[str, tuple[int, str]] = {}
        self.seen: list[dict] = []
        self.descriptor = dict(DESCRIPTOR)
        self.app = self._build()

    def _job(self, kind, **fields):
        job = {"id": JOB_ID, "kind": kind, "state": "queued", "purge": False,
               "created_at": 1, "started_at": None, "finished_at": None, "error": None,
               "log": [], "log_truncated": False, "service": None, "source": None,
               "ref": None, "commit": None, **fields}
        self.jobs[job["id"]] = job
        return job

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.middleware("http")
        async def guard(request: Request, call_next):
            body = await request.body()
            self.seen.append({"method": request.method, "path": request.url.path,
                              "token": request.headers.get("x-installer-token"),
                              "body": json.loads(body) if body else None,
                              "pins": {p["plugin_id"]: p["version"] for p in pins.all()}})
            if request.headers.get("x-installer-token") != INSTALLER_TOKEN:
                return JSONResponse({"error": "unauthorized", "code": "unauthorized"}, 401)
            if request.url.path in self.fail:
                status, code = self.fail[request.url.path]
                return JSONResponse({"error": f"simulated {code}", "code": code}, status)
            return await call_next(request)

        @app.post("/inspect")
        def inspect(body: dict):
            commit, text = self.refs[body["ref"]]
            svc = self.descriptor["service"]
            return {"source": body["source"], "ref": body["ref"], "commit": commit,
                    "descriptor": self.descriptor,
                    "manifests": [{"plugin": "echo", "path": "aab_plugin_echo/manifest.yaml",
                                   "version": yaml.safe_load(text)["version"], "text": text}],
                    "installed": self.records.get(svc)}

        @app.post("/install", status_code=202)
        def install(body: dict):
            return self._job("install", source=body["source"], ref=body["ref"],
                             commit=body["commit"])

        @app.post("/upgrade", status_code=202)
        def upgrade(body: dict):
            return self._job("upgrade", **body)

        @app.post("/remove", status_code=202)
        def remove(body: dict):
            return self._job("remove", service=body["service"], purge=body["purge"])

        @app.get("/jobs/{job_id}")
        def job(job_id: str):
            if job_id not in self.jobs:
                return JSONResponse({"error": "no such job", "code": "not_found"}, 404)
            return self.jobs[job_id]

        @app.get("/installed")
        def installed():
            return {"items": list(self.records.values())}

        return app

    def record(self, ref="v0.1.0", commit=V1, plugins=("echo",)):
        self.records["echo"] = {"service": "echo", "source": SOURCE, "ref": ref,
                                "commit": commit, "plugins": list(plugins),
                                "volumes": ["echo_secrets", "echo_data"],
                                "installed_at": 1, "updated_at": 1}

    def calls(self, path=None):
        return [c for c in self.seen if path is None or c["path"] == path]


@pytest.fixture()
def external(env, tmp_path):
    """echo is not vendored: an external plugin, pinned from the database."""
    registry.reset_registry(registry.Registry(vendored_dirs=(tmp_path / "no-tree",)))


@pytest.fixture()
def configured(env, monkeypatch):
    from broker.config import get_settings
    monkeypatch.setenv("INSTALLER_URL", INSTALLER_URL)
    monkeypatch.setenv("INSTALLER_TOKEN", INSTALLER_TOKEN)
    get_settings.cache_clear()


@pytest.fixture()
def fake(configured, external, monkeypatch):
    f = FakeInstaller()

    def factory(base_url, headers, timeout):
        assert base_url == INSTALLER_URL
        f.timeouts.append(timeout)
        return TestClient(f.app, base_url=base_url, headers=headers)

    f.timeouts = []
    monkeypatch.setattr(plugin_install, "client_factory", factory)
    return f


def come_up(echo_impl, tmp_path):
    """The installed service as discovery sees it: plugin-echo on net_echo,
    service `echo` (what the descriptor names), offering echo's manifest."""
    from aab_plugin_runtime import serve
    from cryptography.fernet import Fernet
    runtime = serve([echo_impl], PLUGIN_TOKEN, tmp_path / "echo-secrets",
                    Fernet.generate_key().decode())
    get_registry().discover({"echo": ("http://plugin-echo:8090", PLUGIN_TOKEN)},
                            client_factory=runtime_factory(runtime))
    return runtime


def audit_rows(action=None):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    return [dict(r) for r in rows if action is None or r["action"] == action]


def inspect(client, headers, ref="v0.1.0"):
    return client.post("/v1/admin/plugins/install/inspect", headers=headers,
                       json={"source": SOURCE, "ref": ref})


def install(client, headers, ref="v0.1.0", commit=V1):
    return client.post("/v1/admin/plugins/install", headers=headers,
                       json={"source": SOURCE, "ref": ref, "commit": commit})


# ---- status, and an installer that is off or down -------------------------------------

def test_status_when_the_installer_is_off(client, admin_headers, external):
    r = client.get("/v1/admin/plugins/install/status", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False and body["reachable"] is False
    assert "INSTALLER_ENABLED=true" in body["error"]
    assert "INSTALLER_ENABLED=true" in body["enable_lines"]
    assert any(line.startswith("INSTALLER_ALLOWED_SOURCES=") for line in body["enable_lines"])
    assert body["manual_command"] == "docker compose $(scripts/compose-files.sh) up -d"
    assert "INSTALLER_ALLOWED_SOURCES" in body["allowlist_hint"]


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/v1/admin/plugins/install/inspect", {"source": SOURCE, "ref": "v0.1.0"}),
    ("POST", "/v1/admin/plugins/install", {"source": SOURCE, "ref": "v0.1.0", "commit": V1}),
    ("POST", "/v1/admin/plugins/echo/upgrade", {"source": SOURCE, "ref": "v0.2.0",
                                                "commit": V2}),
    ("POST", "/v1/admin/plugins/echo/remove", {"purge": False}),
    ("GET", f"/v1/admin/plugins/install/jobs/{JOB_ID}", None),
    ("GET", "/v1/admin/plugins/installed", None),
])
def test_an_installer_that_is_off_answers_503_saying_so(client, admin_headers, external,
                                                        method, path, body):
    r = client.request(method, path, headers=admin_headers, json=body)
    assert r.status_code == 503, r.text
    assert r.json()["code"] == "installer_off"
    assert "installer is off" in r.json()["error"] and "INSTALLER_ENABLED" in r.json()["error"]
    assert pins.all() == []


def test_a_url_without_a_token_is_off_too(client, admin_headers, external, monkeypatch):
    from broker.config import get_settings
    monkeypatch.setenv("INSTALLER_URL", INSTALLER_URL)
    get_settings.cache_clear()
    r = client.get("/v1/admin/plugins/installed", headers=admin_headers)
    assert r.status_code == 503 and "INSTALLER_TOKEN is empty" in r.json()["error"]
    status = client.get("/v1/admin/plugins/install/status", headers=admin_headers).json()
    assert status["configured"] is False and "INSTALLER_TOKEN" in status["error"]


def test_status_when_configured_and_reachable(client, admin_headers, fake):
    fake.record()
    body = client.get("/v1/admin/plugins/install/status", headers=admin_headers).json()
    assert body["configured"] is True and body["reachable"] is True
    assert body["installed"] == 1 and body["error"] is None
    assert INSTALLER_TOKEN not in json.dumps(body) and INSTALLER_URL not in json.dumps(body)
    assert fake.calls("/installed")[0]["token"] == INSTALLER_TOKEN


def down(exc_type):
    def factory(base_url, headers, timeout):
        def handler(request):
            raise exc_type("simulated", request=request)
        return httpx.Client(base_url=base_url, headers=headers,
                            transport=httpx.MockTransport(handler))
    return factory


def test_an_unreachable_installer_answers_503_saying_it_is_down(client, admin_headers,
                                                                configured, external,
                                                                monkeypatch):
    monkeypatch.setattr(plugin_install, "client_factory", down(httpx.ConnectError))
    r = inspect(client, admin_headers)
    assert r.status_code == 503 and r.json()["code"] == "installer_down"
    assert "down or unreachable" in r.json()["error"]
    status = client.get("/v1/admin/plugins/install/status", headers=admin_headers).json()
    assert status["configured"] is True and status["reachable"] is False
    assert "down or unreachable" in status["error"]
    assert install(client, admin_headers).status_code == 503
    assert pins.all() == []


def test_a_slow_inspect_is_retryable_but_a_lost_install_answer_is_unknown(
        client, admin_headers, fake, monkeypatch):
    real = plugin_install.client_factory
    timeout_on = {"path": "/inspect"}

    def factory(base_url, headers, timeout):
        def handler(request):
            if request.url.path == timeout_on["path"]:
                raise httpx.ReadTimeout("simulated", request=request)
            with real(base_url, headers, timeout) as c:
                resp = c.request(request.method, request.url.path, content=request.content,
                                 headers={"Content-Type": "application/json"})
            return httpx.Response(resp.status_code, content=resp.content,
                                  headers=resp.headers)
        return httpx.Client(base_url=base_url, headers=headers,
                            transport=httpx.MockTransport(handler))

    monkeypatch.setattr(plugin_install, "client_factory", factory)
    r = inspect(client, admin_headers)
    assert r.status_code == 503 and r.json()["code"] == "installer_down"
    timeout_on["path"] = "/install"
    r = install(client, admin_headers)
    assert r.status_code == 502 and r.json()["code"] == "unknown_outcome"
    # The job may exist: the pin it needs stays.
    assert pins.record("echo")["commit"] == V1
    row = audit_rows("plugin.install")[-1]
    assert row["result"] == "error" and json.loads(row["detail"])["pins_restored"] == []


def test_a_token_the_installer_refuses_is_a_503_not_a_sign_out(client, admin_headers, fake,
                                                              monkeypatch):
    from broker.config import get_settings
    monkeypatch.setenv("INSTALLER_TOKEN", "a-different-token-0123456789abcdef")
    get_settings.cache_clear()
    r = client.get("/v1/admin/plugins/installed", headers=admin_headers)
    assert r.status_code == 503 and r.json()["code"] == "installer_token_mismatch"
    assert "INSTALLER_TOKEN" in r.json()["error"]


# ---- inspect: the review card ---------------------------------------------------------------

def test_inspect_returns_the_review(client, admin_headers, fake):
    r = inspect(client, admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["source"], body["ref"], body["commit"], body["service"]) == (
        SOURCE, "v0.1.0", V1, "echo")
    assert body["upgrade"] is False and body["installed"] is None and body["problems"] == []
    assert body["descriptor"]["volumes"] == {"echo_data": "/data"}
    assert body["descriptor"]["env_passthrough"] == ["TZ"]
    [item] = body["plugins"]
    assert item["id"] == "echo" and item["valid"] is True and item["blocked"] is None
    assert item["version"] == "0.1.0" and item["pinned"] is None
    acts = {a["name"]: a for a in item["summary"]["actions"]}
    assert acts["post_item"]["side_effect"] == "write"
    assert acts["post_item"]["modes"] == ["direct", "draft"]
    assert item["summary"]["secret_config"] == ["api_secret"]
    assert item["diff"]["from_version"] is None and item["diff"]["to_version"] == "0.1.0"
    # The manifest text stays in the broker; the token never leaves it.
    assert "text" not in item and INSTALLER_TOKEN not in r.text
    [call] = fake.calls("/inspect")
    assert call["token"] == INSTALLER_TOKEN and call["body"] == {"source": SOURCE,
                                                                 "ref": "v0.1.0"}
    assert fake.timeouts == [plugin_install.INSPECT_TIMEOUT]
    assert pins.all() == [] and audit_rows("plugin.pin") == []      # inspect changes nothing


def test_inspect_flags_an_invalid_manifest(client, admin_headers, fake):
    fake.refs["v0.1.0"] = (V1, echo_text("0.1.0", actions=[]))
    item = inspect(client, admin_headers).json()["plugins"][0]
    assert item["valid"] is False and item["error"] and item["summary"] is None
    assert inspect(client, admin_headers).json()["problems"]


def test_inspect_of_an_upgrade_shows_the_diff_against_the_pin(client, admin_headers, fake):
    pins.set("echo", echo_text("0.1.0"), SOURCE, "v0.1.0", V1, "owner")
    fake.record()
    body = inspect(client, admin_headers, "v0.2.0").json()
    assert body["upgrade"] is True and body["installed"]["ref"] == "v0.1.0"
    item = body["plugins"][0]
    assert item["pinned"]["version"] == "0.1.0"
    assert (item["diff"]["from_version"], item["diff"]["to_version"]) == ("0.1.0", "0.2.0")


def test_a_package_whose_manifests_disagree_with_its_descriptor_is_refused(client, admin_headers,
                                                                          fake):
    fake.descriptor = {**DESCRIPTOR, "plugins": ["echo", "other"]}
    r = inspect(client, admin_headers)
    assert r.status_code == 502 and r.json()["code"] == "installer_error"


@pytest.mark.parametrize("status,code", [(400, "bad_request"), (403, "source_not_allowed"),
                                         (422, "invalid_package"), (502, "clone_failed"),
                                         (503, "unavailable")])
def test_installer_refusals_are_relayed_with_status_and_code(client, admin_headers, fake,
                                                             status, code):
    fake.fail["/inspect"] = (status, code)
    r = inspect(client, admin_headers)
    assert r.status_code == status and r.json()["code"] == code
    assert f"simulated {code}" in r.json()["error"]


def test_an_installer_crash_is_a_502(client, admin_headers, fake):
    fake.fail["/inspect"] = (500, "internal")
    r = inspect(client, admin_headers)
    assert r.status_code == 502 and r.json()["code"] == "internal"


def test_inspect_bodies_are_strict(client, admin_headers, fake):
    for body in ({"source": SOURCE}, {"source": SOURCE, "ref": "v0.1.0", "extra": 1},
                 {"source": "", "ref": "v0.1.0"}, {"source": SOURCE, "ref": "x" * 65}):
        r = client.post("/v1/admin/plugins/install/inspect", headers=admin_headers, json=body)
        assert r.status_code == 422, body
    assert fake.calls() == []


# ---- install: pin first, then ask -------------------------------------------------------------

def test_install_pins_before_it_asks_and_audits(client, admin_headers, owner, fake):
    r = install(client, admin_headers)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["job"]["id"] == JOB_ID and body["job"]["kind"] == "install"
    assert body["service"] == "echo" and body["pinned"] == ["echo"]
    # At the moment the installer was asked, the pin already existed.
    [call] = fake.calls("/install")
    assert call["pins"] == {"echo": "0.1.0"}
    assert call["body"] == {"source": SOURCE, "ref": "v0.1.0", "commit": V1}
    assert fake.calls("/inspect")[0]["pins"] == {}             # inspected again, unpinned
    rec = pins.record("echo")
    assert (rec["source"], rec["ref"], rec["commit"], rec["pinned_by"]) == (
        SOURCE, "v0.1.0", V1, owner.username)
    pin_row, install_row = audit_rows("plugin.pin")[0], audit_rows("plugin.install")[0]
    assert pin_row["id"] < install_row["id"]
    assert install_row["actor"] == owner.username and install_row["actor_via"] == "token"
    assert install_row["actor_principal"] == owner.id and install_row["result"] == "ok"
    detail = json.loads(install_row["detail"])
    assert (detail["source"], detail["ref"], detail["commit"], detail["job"]) == (
        SOURCE, "v0.1.0", V1, JOB_ID)
    assert detail["plugins"] == ["echo"]


def test_the_service_that_comes_up_is_registered_disabled(client, admin_headers, fake,
                                                          echo_impl, tmp_path):
    assert install(client, admin_headers).status_code == 202
    come_up(echo_impl, tmp_path)                              # the job's container, discovered
    assert get_registry().service_of("echo") == "echo"
    view = client.get("/v1/admin/plugins/echo", headers=admin_headers).json()
    assert view["enabled"] is False                           # the owner still enables it


def test_install_refuses_a_commit_other_than_the_reviewed_one(client, admin_headers, fake):
    r = install(client, admin_headers, commit=V2)
    assert r.status_code == 409 and r.json()["code"] == "commit_changed"
    assert pins.all() == [] and fake.calls("/install") == []
    assert audit_rows("plugin.install")[0]["result"] == "error"


def test_install_refuses_an_invalid_manifest_before_pinning(client, admin_headers, fake):
    fake.refs["v0.1.0"] = (V1, echo_text("0.1.0", actions=[]))
    r = install(client, admin_headers)
    assert r.status_code == 400 and r.json()["code"] == "invalid_manifest"
    assert pins.all() == [] and fake.calls("/install") == []


def test_install_refuses_an_installed_service(client, admin_headers, fake):
    fake.record()
    r = install(client, admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "already_installed"
    assert pins.all() == [] and fake.calls("/install") == []


def test_install_refuses_an_id_the_broker_tree_owns(client, admin_headers, configured,
                                                    vendored_echo, monkeypatch):
    f = FakeInstaller()
    monkeypatch.setattr(plugin_install, "client_factory",
                        lambda b, h, t: TestClient(f.app, base_url=b, headers=h))
    item = inspect(client, admin_headers).json()["plugins"][0]
    assert "ships with the broker" in item["blocked"]
    r = install(client, admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    assert pins.all() == [] and f.calls("/install") == []


@pytest.mark.parametrize("status,code", [(409, "busy"), (403, "source_not_allowed")])
def test_a_refused_install_puts_the_pins_back(client, admin_headers, fake, status, code):
    fake.fail["/install"] = (status, code)
    r = install(client, admin_headers)
    assert r.status_code == status and r.json()["code"] == code
    assert pins.all() == []
    row = audit_rows("plugin.install")[-1]
    assert row["result"] == "error"
    assert json.loads(row["detail"])["pins_restored"] == ["echo"]


def test_install_bodies_are_strict(client, admin_headers, fake):
    for body in ({"source": SOURCE, "ref": "v0.1.0"},
                 {"source": SOURCE, "ref": "v0.1.0", "commit": "abc"},
                 {"source": SOURCE, "ref": "v0.1.0", "commit": V1, "service": "x"}):
        r = client.post("/v1/admin/plugins/install", headers=admin_headers, json=body)
        assert r.status_code == 422, body
    assert pins.all() == []


# ---- upgrade: re-pin, then ask ----------------------------------------------------------------

def upgrade(client, headers, service="echo", ref="v0.2.0", commit=V2):
    return client.post(f"/v1/admin/plugins/{service}/upgrade", headers=headers,
                       json={"source": SOURCE, "ref": ref, "commit": commit})


def test_upgrade_repins_before_it_asks(client, admin_headers, owner, fake):
    pins.set("echo", echo_text("0.1.0"), SOURCE, "v0.1.0", V1, owner.username)
    fake.record()
    r = upgrade(client, admin_headers)
    assert r.status_code == 202, r.text
    assert r.json()["job"]["kind"] == "upgrade" and r.json()["unpinned"] == []
    [call] = fake.calls("/upgrade")
    assert call["pins"] == {"echo": "0.2.0"}
    assert call["body"] == {"service": "echo", "source": SOURCE, "ref": "v0.2.0", "commit": V2}
    assert pins.record("echo")["version"] == "0.2.0" and pins.record("echo")["commit"] == V2
    row = audit_rows("plugin.upgrade")[0]
    detail = json.loads(row["detail"])
    assert row["result"] == "ok" and detail["from"] == {"ref": "v0.1.0", "commit": V1}
    assert json.loads(audit_rows("plugin.pin")[-1]["detail"])["previous"] == "0.1.0"


def test_a_refused_upgrade_restores_the_previous_pin(client, admin_headers, owner, fake):
    pins.set("echo", echo_text("0.1.0"), SOURCE, "v0.1.0", V1, owner.username)
    before = pins.snapshot("echo")
    fake.record()
    fake.fail["/upgrade"] = (409, "busy")
    r = upgrade(client, admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "busy"
    assert pins.snapshot("echo") == before                    # byte for byte, provenance too
    assert pins.get("echo").version == "0.1.0"


def test_a_refused_upgrade_keeps_the_running_plugin_served(client, admin_headers, owner, fake,
                                                           echo_impl, tmp_path, make_agent):
    pins.set("echo", ECHO_TEXT, SOURCE, "v0.1.0", V1, owner.username)
    come_up(echo_impl, tmp_path)
    enable_plugin()
    agent = make_agent([cap(["list_items"])])
    fake.record()
    fake.fail["/upgrade"] = (503, "unavailable")
    assert upgrade(client, admin_headers).status_code == 503
    url = "/v1/targets/echo/actions/list_items"
    assert client.post(url, json={"params": {}}, headers=agent.headers).status_code == 200


@pytest.mark.parametrize("service,setup,status,code", [
    ("other", "record", 409, "service_changed"),
    ("echo", None, 404, "not_installed"),
    ("Bad!", "record", 400, "bad_request"),
])
def test_upgrade_refusals(client, admin_headers, fake, service, setup, status, code):
    if setup:
        fake.record()
    r = upgrade(client, admin_headers, service=service)
    assert r.status_code == status and r.json()["code"] == code
    assert pins.all() == [] and fake.calls("/upgrade") == []


def test_upgrade_from_another_source_is_refused(client, admin_headers, fake):
    fake.record()
    fake.records["echo"]["source"] = "github.com/someone-else/aab-plugin-echo"
    r = upgrade(client, admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "source_changed"
    assert pins.all() == []


def test_upgrade_unpins_ids_the_new_version_no_longer_hosts(client, admin_headers, owner, fake):
    pins.set("echo", echo_text("0.1.0"), SOURCE, "v0.1.0", V1, owner.username)
    pins.set("echoold", echo_text("0.1.0").replace("id: echo", "id: echoold"), SOURCE,
             "v0.1.0", V1, owner.username)
    fake.record(plugins=("echo", "echoold"))
    r = upgrade(client, admin_headers)
    assert r.status_code == 202 and r.json()["unpinned"] == ["echoold"]
    assert pins.record("echoold") is None and pins.record("echo")["version"] == "0.2.0"


# ---- remove: ask, then unpin -------------------------------------------------------------------

def test_remove_asks_then_unpins_and_audits(client, admin_headers, owner, fake, echo_impl,
                                            tmp_path, make_agent):
    pins.set("echo", ECHO_TEXT, SOURCE, "v0.1.0", V1, owner.username)
    come_up(echo_impl, tmp_path)
    enable_plugin()
    agent = make_agent([cap(["list_items"])])
    fake.record()
    r = client.post("/v1/admin/plugins/echo/remove", headers=admin_headers, json={"purge": True})
    assert r.status_code == 202, r.text
    assert r.json()["unpinned"] == ["echo"] and r.json()["job"]["kind"] == "remove"
    # The installer was asked while the pin still stood; it is gone after.
    [call] = fake.calls("/remove")
    assert call["pins"] == {"echo": "0.1.0"} and call["body"] == {"service": "echo",
                                                                  "purge": True}
    assert pins.record("echo") is None
    url = "/v1/targets/echo/actions/list_items"
    assert client.post(url, json={"params": {}}, headers=agent.headers).status_code == 404
    assert get_registry().plugin_rows()["echo"]["enabled"] == 0
    unpin_row, remove_row = audit_rows("plugin.unpin")[0], audit_rows("plugin.remove")[0]
    assert unpin_row["id"] < remove_row["id"]
    detail = json.loads(remove_row["detail"])
    assert remove_row["actor"] == owner.username and remove_row["result"] == "ok"
    assert detail["purge"] is True and detail["unpinned"] == ["echo"]
    assert detail["job"] == JOB_ID and detail["source"] == SOURCE


def test_remove_without_a_body_keeps_the_volumes(client, admin_headers, fake):
    fake.record()
    r = client.post("/v1/admin/plugins/echo/remove", headers=admin_headers)
    assert r.status_code == 202, r.text
    assert fake.calls("/remove")[0]["body"] == {"service": "echo", "purge": False}


def test_a_refused_remove_keeps_the_pins(client, admin_headers, owner, fake):
    pins.set("echo", ECHO_TEXT, SOURCE, "v0.1.0", V1, owner.username)
    fake.record()
    fake.fail["/remove"] = (409, "busy")
    r = client.post("/v1/admin/plugins/echo/remove", headers=admin_headers, json={})
    assert r.status_code == 409 and r.json()["code"] == "busy"
    assert pins.record("echo") is not None
    assert audit_rows("plugin.remove")[0]["result"] == "error"


def test_remove_of_a_service_that_is_not_installed_is_404(client, admin_headers, fake):
    r = client.post("/v1/admin/plugins/echo/remove", headers=admin_headers, json={})
    assert r.status_code == 404 and r.json()["code"] == "not_installed"
    assert fake.calls("/remove") == []


# ---- job and installed passthrough -------------------------------------------------------------

def test_job_and_installed_are_passed_through(client, admin_headers, fake):
    fake.record()
    assert install(client, admin_headers).status_code == 409    # installed already: no job
    fake.records.clear()
    job = install(client, admin_headers).json()["job"]
    fake.jobs[job["id"]].update(state="done", log=["$ docker compose up", "exit 0"])
    r = client.get(f"/v1/admin/plugins/install/jobs/{job['id']}", headers=admin_headers)
    assert r.status_code == 200 and r.json()["state"] == "done"
    assert r.json()["log"] == ["$ docker compose up", "exit 0"]
    fake.record()
    r = client.get("/v1/admin/plugins/installed", headers=admin_headers)
    assert r.status_code == 200
    assert [i["service"] for i in r.json()["items"]] == ["echo"]
    assert r.json()["items"][0]["source"] == SOURCE


def test_unknown_and_malformed_job_ids_are_404(client, admin_headers, fake):
    r = client.get(f"/v1/admin/plugins/install/jobs/{'d' * 32}", headers=admin_headers)
    assert r.status_code == 404 and r.json()["code"] == "not_found"        # relayed
    before = len(fake.calls())
    for bad in ("x", "D" * 32, "c" * 31, "..%2Finstalled"):
        r = client.get(f"/v1/admin/plugins/install/jobs/{bad}", headers=admin_headers)
        assert r.status_code == 404, bad
    assert len(fake.calls()) == before                      # never sent to the installer


def test_installed_is_not_taken_for_a_plugin_named_installed(client, admin_headers, fake):
    # Route order: the install router precedes GET /v1/admin/plugins/{plugin}.
    r = client.get("/v1/admin/plugins/installed", headers=admin_headers)
    assert r.status_code == 200 and r.json() == {"items": []}


# ---- the guard and the token --------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/admin/plugins/install/status"),
    ("POST", "/v1/admin/plugins/install/inspect"), ("POST", "/v1/admin/plugins/install"),
    ("GET", f"/v1/admin/plugins/install/jobs/{JOB_ID}"), ("GET", "/v1/admin/plugins/installed"),
    ("POST", "/v1/admin/plugins/echo/upgrade"), ("POST", "/v1/admin/plugins/echo/remove"),
])
def test_install_routes_need_the_owner(client, fake, make_agent, method, path):
    assert client.request(method, path).status_code == 401
    agent = make_agent()
    assert client.request(method, path, headers=agent.headers).status_code == 401
    assert fake.calls() == []


def test_the_installer_token_is_in_no_response_audit_row_or_log_line(client, admin_headers,
                                                                     owner, fake, caplog):
    caplog.set_level(logging.DEBUG)
    texts = [inspect(client, admin_headers).text, install(client, admin_headers).text,
             client.get(f"/v1/admin/plugins/install/jobs/{JOB_ID}", headers=admin_headers).text,
             client.get("/v1/admin/plugins/install/status", headers=admin_headers).text]
    fake.record()
    texts.append(upgrade(client, admin_headers).text)
    texts.append(client.post("/v1/admin/plugins/echo/remove", headers=admin_headers,
                             json={}).text)
    fake.fail["/inspect"] = (403, "source_not_allowed")
    texts.append(inspect(client, admin_headers).text)
    assert all(c["token"] == INSTALLER_TOKEN for c in fake.calls())
    for text in (*texts, json.dumps(audit_rows()), caplog.text):
        assert INSTALLER_TOKEN not in text
    assert "plugin install requested" in caplog.text


def test_boot_names_the_installer_token_but_never_shows_it(configured, caplog):
    from broker import main
    caplog.set_level(logging.INFO)
    main.log_boot("wal")
    assert INSTALLER_TOKEN not in caplog.text
    assert "secrets_set=installer_token" in caplog.text and "installer=true" in caplog.text


def test_the_settings_repr_hides_the_installer_token(configured):
    from broker.config import get_settings
    assert INSTALLER_TOKEN not in repr(get_settings())
    assert get_settings().installer_token == INSTALLER_TOKEN


def test_pins_snapshot_and_restore_are_exact(env):
    assert pins.snapshot("echo") is None
    pins.set("echo", ECHO_TEXT, SOURCE, "v0.1.0", V1, "owner")
    snap = pins.snapshot("echo")
    assert snap["manifest_yaml"] == ECHO_TEXT and snap["commit"] == V1
    pins.set("echo", echo_text("0.2.0"), SOURCE, "v0.2.0", V2, "owner")
    pins.restore("echo", snap)
    assert pins.snapshot("echo") == snap
    pins.restore("echo", None)
    assert pins.record("echo") is None
    pins.restore("Bad-Id", snap)                             # junk ids never touch the table
    assert pins.all() == []



def test_every_refused_mutation_is_audited_even_before_anything_is_pinned(client, admin_headers,
                                                                         owner, fake):
    fake.fail["/inspect"] = (403, "source_not_allowed")
    assert install(client, admin_headers).status_code == 403
    assert upgrade(client, admin_headers).status_code == 403
    fake.fail.clear()
    assert client.post("/v1/admin/plugins/echo/remove", headers=admin_headers,
                       json={}).status_code == 404
    rows = {r["action"]: r for r in audit_rows()}
    for action in ("plugin.install", "plugin.upgrade", "plugin.remove"):
        row = rows[action]
        assert row["result"] == "error" and row["actor"] == owner.username, action
        assert row["actor_via"] == "token"
    assert json.loads(rows["plugin.install"]["detail"])["code"] == "source_not_allowed"
    assert rows["plugin.install"]["resource"] == SOURCE         # no service known yet
    assert json.loads(rows["plugin.remove"]["detail"])["code"] == "not_installed"
    assert pins.all() == []


def test_an_unexpected_failure_after_pinning_puts_the_pins_back(admin_ctx, fake, monkeypatch):
    """Fail closed: a crash between the pin and the installer's answer (here,
    in the call itself) leaves no pin behind and is audited as an error."""
    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(plugin_install, "_call",
                        lambda method, path, *a, **kw: boom() if path == "/install"
                        else REAL_CALL(method, path, *a, **kw))
    with pytest.raises(RuntimeError):
        plugin_install.install(admin_ctx, SOURCE, "v0.1.0", V1)
    assert pins.all() == []
    row = audit_rows("plugin.install")[-1]
    assert row["result"] == "error"
    assert json.loads(row["detail"])["code"] == "internal"
    assert json.loads(row["detail"])["pins_restored"] == ["echo"]

