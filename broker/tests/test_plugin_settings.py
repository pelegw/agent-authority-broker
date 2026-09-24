"""Plugin admin: config validation, secrets relayed and never stored,
enable/disable/health, connect relay, and the OAuth callback page."""

import json

import pytest
import yaml

from broker import db
from broker.config import get_settings
from broker.plugins import settings
from broker.plugins.adapter import AdapterError
from broker.plugins.manifest import Manifest
from broker.plugins.registry import get_registry

from .conftest import CSRF_HEADERS, ECHO_DIR, register_inprocess

SECRET = "sekret-value-XYZ-123"


@pytest.fixture()
def disabled_echo(vendored_echo, owner, echo_impl):
    register_inprocess(echo_impl)
    return echo_impl


def _db_bytes() -> bytes:
    with db.connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    with open(get_settings().broker_db, "rb") as fh:
        return fh.read()


def _all_rows_text() -> str:
    with db.connect() as conn:
        out = []
        for t in ("plugins", "plugin_secrets", "audit_log", "decisions", "app_config"):
            out += [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
    return json.dumps(out, default=str)


def test_list_and_get_show_schema_not_secrets(client, admin_headers, disabled_echo):
    listed = client.get("/v1/admin/plugins", headers=admin_headers).json()
    [p] = listed["items"]
    assert (p["id"], p["enabled"], p["service"]) == ("echo", False, "inprocess")
    assert {f["name"]: f["secret"] for f in p["config_schema"]} == {
        "greeting": False, "api_secret": True}
    assert p["config"] == {"greeting": "hello"}         # defaults; never secrets
    assert client.get("/v1/admin/plugins/nope", headers=admin_headers).status_code == 404


def test_patch_validates_against_config_schema(client, admin_headers, disabled_echo):
    url = "/v1/admin/plugins/echo"
    assert client.patch(url, json={"config": {"nope": 1}},
                        headers=admin_headers).status_code == 400
    assert client.patch(url, json={"config": {"greeting": 5}},
                        headers=admin_headers).status_code == 400
    assert client.patch(url, json={"config": {"api_secret": 5}},
                        headers=admin_headers).status_code == 400
    r = client.patch(url, json={"config": {"greeting": "hey"}}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["config"] == {"greeting": "hey"}
    r = client.patch(url, json={"config": {"greeting": None}}, headers=admin_headers)
    assert r.json()["config"] == {"greeting": "hello"}  # reset to default


def test_secrets_are_relayed_and_never_persisted(client, admin_headers, echo, owner):
    r = client.patch("/v1/admin/plugins/echo", json={"config": {"api_secret": SECRET}},
                     headers=admin_headers)
    assert r.status_code == 200 and SECRET not in r.text
    # The plugin got it ...
    health = client.post("/v1/admin/plugins/echo/health", headers=admin_headers).json()
    assert health["last_health"]["api_secret_set"] is True
    # ... and the broker kept nothing: not in any row, not in the file.
    assert SECRET not in _all_rows_text()
    assert SECRET.encode() not in _db_bytes()
    with db.connect() as conn:
        detail = json.loads(conn.execute("SELECT detail FROM audit_log WHERE action ="
                                         " 'plugin.config'").fetchone()["detail"])
    assert detail == {"fields": [], "secret_fields": ["api_secret"]}


def test_secret_patch_fails_cleanly_when_plugin_refuses(client, admin_headers, disabled_echo,
                                                        monkeypatch):
    def refuse(config, secrets):
        raise AdapterError(503, "down")
    monkeypatch.setattr(disabled_echo, "configure", refuse)
    r = client.patch("/v1/admin/plugins/echo", json={"config": {"api_secret": SECRET,
                                                                "greeting": "x"}},
                     headers=admin_headers)
    assert r.status_code == 503
    assert settings.effective_config(get_registry().manifests()["echo"],
                                     get_registry().plugin_rows()["echo"]["config"]) == {
        "greeting": "hello"}


def test_enable_configures_and_runs_health(client, admin_headers, disabled_echo, owner):
    client.patch("/v1/admin/plugins/echo", json={"config": {"greeting": "yo"}},
                 headers=admin_headers)
    r = client.post("/v1/admin/plugins/echo/enable", headers=admin_headers)
    body = r.json()
    assert r.status_code == 200 and body["enabled"] is True and body["connected"] is True
    assert body["last_health"]["healthy"] is True
    assert disabled_echo.greeting == "yo"               # config relayed on enable
    assert get_registry().enabled_plugins() == ["echo"]
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM audit_log WHERE action = 'plugin.enable'").fetchone()
    assert (row["actor"], row["actor_principal"], row["actor_via"]) == (
        owner.username, owner.id, "token")


def test_enable_refused_when_configure_fails(client, admin_headers, disabled_echo, monkeypatch):
    def bad(config, secrets):
        raise AdapterError(400, "greeting too rude")
    monkeypatch.setattr(disabled_echo, "configure", bad)
    r = client.post("/v1/admin/plugins/echo/enable", headers=admin_headers)
    assert r.status_code == 400 and "greeting too rude" in r.json()["error"]
    assert get_registry().enabled_plugins() == []


def test_unhealthy_but_enabled_is_legitimate(client, admin_headers, disabled_echo):
    disabled_echo.connection.connected = False          # e.g. WhatsApp before pairing
    body = client.post("/v1/admin/plugins/echo/enable", headers=admin_headers).json()
    assert body["enabled"] is True and body["connected"] is False


def test_health_failure_keeps_last_connected(client, admin_headers, echo_local, monkeypatch):
    def down():
        raise AdapterError(503, "plugin service unreachable")
    monkeypatch.setattr(echo_local.impl, "status", down)
    body = client.post("/v1/admin/plugins/echo/health", headers=admin_headers).json()
    assert body["last_health"]["healthy"] is False and body["connected"] is True


@pytest.mark.parametrize("reported", ["proxy", "target", "mixed"])
def test_health_failure_keeps_the_last_reported_enforcement(client, admin_headers, echo_local,
                                                            monkeypatch, reported):
    # A refresh that fails says nothing about how the credential enforces:
    # the last value the plugin itself reported must survive it.
    echo_local.impl.enforcement = reported
    client.post("/v1/admin/plugins/echo/health", headers=admin_headers)
    assert get_registry().last_health("echo")["enforcement"] == reported

    def down():
        raise AdapterError(503, "plugin service unreachable")
    monkeypatch.setattr(echo_local.impl, "status", down)
    body = client.post("/v1/admin/plugins/echo/health", headers=admin_headers).json()
    assert body["last_health"] == {"healthy": False, "error": "plugin service unreachable",
                                   "status": 503, "enforcement": reported}
    assert body["connected"] is True
    # Twice in a row: still kept (the failure record carries it forward).
    client.post("/v1/admin/plugins/echo/health", headers=admin_headers)
    assert get_registry().last_health("echo")["enforcement"] == reported


def test_health_failure_invents_no_enforcement(echo_local):
    # Nothing reported before (or garbage): nothing is kept, and the policy
    # then fails closed to proxy for a non-empty record.
    for previous in ({"connected": True}, {"enforcement": "banana"}):
        settings.set_health("echo", previous, True)
        stored = settings.set_health_failure("echo", "down", 503)
        assert stored == {"healthy": False, "error": "down", "status": 503}
        assert get_registry().last_health("echo") == stored


def test_disable(client, admin_headers, echo_local):
    body = client.post("/v1/admin/plugins/echo/disable", headers=admin_headers).json()
    assert body["enabled"] is False and get_registry().enabled_plugins() == []


def test_session_writes_need_csrf(session_client, disabled_echo):
    assert session_client.post("/v1/admin/plugins/echo/enable").status_code == 403
    assert session_client.post("/v1/admin/plugins/echo/enable",
                               headers=CSRF_HEADERS).status_code == 200


def test_required_config_and_types():
    data = yaml.safe_load((ECHO_DIR / "manifest.yaml").read_text(encoding="utf-8"))
    data["config_schema"].append({"name": "region", "type": "enum", "values": ["eu", "us"],
                                  "required": True})
    data["config_schema"].append({"name": "retries", "type": "integer"})
    m = Manifest.model_validate(data)
    assert settings.missing_required(m, settings.effective_config(m, {})) == ["region"]
    with pytest.raises(Exception):
        settings.split_patch(m, {"region": "mars"})
    with pytest.raises(Exception):
        settings.split_patch(m, {"retries": True})       # bool is not an integer
    assert settings.split_patch(m, {"region": "eu", "retries": 3, "api_secret": None}) == (
        {"region": "eu", "retries": 3}, {"api_secret": None})


def test_enable_refuses_missing_required(client, admin_headers, disabled_echo, monkeypatch):
    monkeypatch.setattr(settings, "missing_required", lambda m, c: ["region"])
    r = client.post("/v1/admin/plugins/echo/enable", headers=admin_headers)
    assert r.status_code == 400 and "region" in r.json()["error"]


# ------------------------------------------------------------ connect relay

def test_connect_flow_relays(client, admin_headers, echo, owner):
    start = client.post("/v1/admin/plugins/echo/connect/start", headers=admin_headers)
    assert start.json() == {"kind": "none", "enabled_plugins": ["echo"]}
    assert client.post("/v1/admin/plugins/echo/disconnect", headers=admin_headers).json() == {
        "ok": True}
    assert get_registry().plugin_rows()["echo"]["connected"] == 0
    fin = client.post("/v1/admin/plugins/echo/connect/finish",
                      json={"code": "auth-code-SECRET", "state": "st"}, headers=admin_headers)
    assert fin.json() == {"ok": True}
    assert get_registry().plugin_rows()["echo"]["connected"] == 1
    assert "auth-code-SECRET" not in _all_rows_text()   # relayed once, recorded nowhere


def test_connect_by_service_name(client, admin_headers, echo):
    service = get_registry().service_of("echo")
    r = client.post(f"/v1/admin/plugins/{service}/connect/start", headers=admin_headers)
    assert r.status_code == 200
    assert client.post("/v1/admin/plugins/nosuch/connect/start",
                       headers=admin_headers).status_code == 404


def test_qr_is_proxied_uncached(client, admin_headers, echo_local):
    echo_local.impl.connection.qr_png = lambda: b"\x89PNGdata"
    r = client.get("/v1/admin/plugins/echo/connect/qr.png", headers=admin_headers)
    assert r.content == b"\x89PNGdata" and r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"] == "image/png"


def test_qr_absent_is_404(client, admin_headers, echo):
    assert client.get("/v1/admin/plugins/echo/connect/qr.png",
                      headers=admin_headers).status_code == 404


# ------------------------------------------------------------ OAuth callback page

def test_oauth_callback_page_needs_no_owner_credential(client, owner):
    # The provider's cross-site redirect carries no SameSite=Strict session
    # cookie, so the page must be served without one (it holds no data).
    r = client.get("/oauth/callback/google?code=<script>alert(1)</script>&state=s")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in r.text and "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp and r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    assert 'var service = "google";' in r.text
    assert "alert(1)" not in r.text                     # nothing from the URL is rendered
    assert "/connect/finish" in r.text and "aab-console" in r.text


def test_oauth_callback_page_strips_the_code_before_posting(client, owner):
    text = client.get("/oauth/callback/google").text
    # The query is dropped from the address bar before anything else runs.
    assert text.index("history.replaceState") < text.index("fetch(")
    # No live session: the owner is asked to log in elsewhere and retry; the
    # retry is wired by addEventListener (the CSP allows no inline handlers).
    assert "Log in to the console in another tab, then retry." in text
    assert 'addEventListener("click", finish)' in text and "onclick" not in text


def test_oauth_callback_page_requires_cloudflare_access_when_enabled(client, owner,
                                                                    monkeypatch):
    monkeypatch.setenv("CF_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud-tag")
    get_settings.cache_clear()
    r = client.get("/oauth/callback/google?code=x&state=y")
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


def test_connect_finish_still_needs_session_and_csrf(client, owner, disabled_echo):
    body = {"code": "c", "state": "s"}
    r = client.post("/v1/admin/plugins/echo/connect/finish", json=body, headers=CSRF_HEADERS)
    assert r.status_code == 401
    assert client.post("/auth/login", json={"username": owner.username,
                                            "password": owner.password}).status_code == 200
    assert client.post("/v1/admin/plugins/echo/connect/finish", json=body).status_code == 403
    assert client.post("/v1/admin/plugins/echo/connect/finish", json=body,
                       headers=CSRF_HEADERS).status_code == 200


def test_oauth_callback_rejects_odd_service_names(client, owner):
    assert client.get("/oauth/callback/Goo%22gle").status_code == 404
