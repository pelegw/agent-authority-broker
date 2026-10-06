"""The secrets-in-logs sweep: drive every flow that handles a secret, with
every logger at DEBUG, and prove no secret value appears in any log record
or in the handler's output.

Flows: owner setup (a wrong setup token first), login (a wrong password
first, and a password typed as the username), the session, admin token
create, plugin configure with a secret field, the Telegram bot token and a
link code, the installer's GitHub token (stored through its route, then
relayed to a fake installer with an inspect, next to INSTALLER_TOKEN), the
Google OAuth client and a full connect (code, refresh token, access tokens
minted per call), the GitHub App private key, install and minted
installation tokens, agent key create, rotate and use (and a refused
key-shaped value), delegation, a password change and logout.

The capture handler sees records BEFORE the redaction backstop (which only
edits the copy our own handler formats), so this proves nothing secret is
logged in the first place; the handler's output is checked as well."""

import base64
import logging

import pytest
from fastapi.testclient import TestClient

from broker.config import get_settings
from broker.identity import ratelimit
from broker.notify import telegram as tg
from broker.plugins import registry
from broker.plugins.registry import TARGETS_DIR
from broker.services import plugin_install

from ..conftest import CSRF_HEADERS, ECHO_DIR, PLUGIN_TOKEN, cap, register_remote
from ..test_plugin_install import INSTALLER_TOKEN, INSTALLER_URL, FakeInstaller
from ..test_plugin_install import SOURCE as PACKAGE_SOURCE
from .test_github import GH_TOKEN, fakes as gh_fakes, register_github
from .test_google import GOOGLE_TOKEN, LOCAL_HOST, REDIRECT, fg, register_google

SETUP = "setup-token-sweep-Q7pWm2KfLr9XbT4sVn8yZc1D"
WRONG_SETUP = "wrong-setup-token-sweep-9f8e7d6c5b4a"
PASSWORD = "sweep owner password 12345"
WRONG_PASSWORD = "sweep wrong password 67890"
NEW_PASSWORD = "sweep new owner password 24680"
SIGNING_KEY = "5ee9" * 16
API_SECRET = "echo-api-secret-value-DO-NOT-LEAK"
BOT_TOKEN = "987654321:AAsweepBotTokenValueDoNotLeak0123456"
# No known token prefix, and a shape no redaction row matches: only never
# being logged keeps it out.
GIT_TOKEN = "sweep-installer-github-token-DoNotLeak01"
BOGUS_KEY = "aab_" + "c0de" * 12


class FakeTelegramAPI:
    """Answers the bot API over httpx (so the token really travels in the
    request path, as in production) without the network."""

    def __init__(self):
        import httpx
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        import httpx
        method = request.url.path.rsplit("/", 1)[-1]
        result = {"getMe": {"username": "sweep_bot"}}.get(method, {"message_id": 1})
        return httpx.Response(200, json={"ok": True, "result": result})


@pytest.fixture()
def sweep(env, client, monkeypatch, tmp_path, secrets_key, echo_impl, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    ratelimit.reset()
    monkeypatch.setenv("SETUP_TOKEN", SETUP)
    monkeypatch.setenv("DECISION_SIGNING_KEY", SIGNING_KEY)
    get_settings.cache_clear()
    import httpx
    api = FakeTelegramAPI()
    monkeypatch.setattr(tg, "_client", lambda token, timeout: httpx.Client(
        base_url=f"{tg._API}/bot{token}", timeout=timeout, transport=api.transport))
    registry.reset_registry(registry.Registry(vendored_dirs=(TARGETS_DIR, ECHO_DIR.parent)))
    secrets = {"setup token": SETUP, "wrong setup token": WRONG_SETUP, "password": PASSWORD,
               "wrong password": WRONG_PASSWORD, "new password": NEW_PASSWORD,
               "signing key": SIGNING_KEY, "broker secrets key": secrets_key,
               "echo api secret": API_SECRET, "bot token": BOT_TOKEN,
               "bot token secret half": BOT_TOKEN.split(":")[1], "bogus key": BOGUS_KEY,
               "installer git token": GIT_TOKEN, "installer token": INSTALLER_TOKEN,
               "echo plugin token": PLUGIN_TOKEN, "google plugin token": GOOGLE_TOKEN,
               "github plugin token": GH_TOKEN, "google client secret": fg.CLIENT_SECRET,
               "google auth code": fg.AUTH_CODE, "google refresh token": fg.REFRESH_TOKEN,
               "github pat": gh_fakes.PAT}
    for n, line in enumerate(gh_fakes.PRIVATE_KEY_PEM.splitlines()[1:-1]):
        secrets[f"pem line {n}"] = line

    # ---- the owner: setup, login, session, admin token --------------------------------
    body = {"username": "owner", "password": PASSWORD}
    assert client.post("/auth/setup", json={**body, "setup_token": WRONG_SETUP}
                       ).status_code == 403
    assert client.post("/auth/setup", json={**body, "setup_token": SETUP}).status_code == 200
    assert client.post("/auth/login", json={"username": "owner", "password": WRONG_PASSWORD}
                       ).status_code == 401
    assert client.post("/auth/login", json={"username": PASSWORD, "password": PASSWORD}
                       ).status_code == 401
    r = client.post("/auth/login", json=body)
    assert r.status_code == 200
    secrets["session cookie"] = client.cookies.get("aab_session")
    r = client.post("/v1/admin/tokens", json={"name": "sweep"}, headers=CSRF_HEADERS)
    secrets["admin token"] = r.json()["token"]
    admin = {"Authorization": f"Bearer {secrets['admin token']}"}
    r = client.post("/v1/admin/tokens", json={"name": "robot", "scope": "monitor"},
                    headers=admin)
    secrets["monitor token"] = r.json()["token"]
    monitor_basic = base64.b64encode(f"uptimerobot:{secrets['monitor token']}".encode()).decode()
    secrets["monitor basic header"] = monitor_basic

    # ---- plugins: echo (a secret field), Google (OAuth), GitHub (App key) --------------
    register_remote(echo_impl, tmp_path)
    google = fg.FakeGoogle()
    register_google(tmp_path, google)
    gh_clock = gh_fakes.Clock()
    gh = gh_fakes.FakeGitHub(gh_clock)
    from aab_plugin_github.adapter import GitHubAdapter
    from aab_plugin_github.api import GitHubAPI
    register_github(GitHubAdapter(GitHubAPI(transport=gh.transport(), clock=gh_clock),
                                  clock=gh_clock), tmp_path)

    def ok(r):
        assert r.status_code in (200, 201), r.text
        return r.json()

    ok(client.patch("/v1/admin/plugins/echo", headers=admin,
                    json={"config": {"api_secret": API_SECRET}}))
    ok(client.post("/v1/admin/plugins/echo/enable", headers=admin))
    ok(client.patch("/v1/admin/plugins/gmail", headers=admin, json={"config": {
        "client_id": fg.CLIENT_ID, "client_secret": fg.CLIENT_SECRET}}))
    ok(client.post("/v1/admin/plugins/gmail/enable", headers=admin))
    google.expected_redirect = REDIRECT
    start = ok(client.post("/v1/admin/plugins/google/connect/start",
                           headers={**admin, "Host": LOCAL_HOST}))
    from urllib.parse import parse_qs, urlsplit
    state = parse_qs(urlsplit(start["url"]).query)["state"][0]
    secrets["google state"] = state
    ok(client.post("/v1/admin/plugins/google/connect/finish", headers=admin,
                   json={"code": fg.AUTH_CODE, "state": state}))
    ok(client.patch("/v1/admin/plugins/github", headers=admin, json={"config": {
        "app_id": gh_fakes.APP_ID, "app_slug": gh_fakes.APP_SLUG,
        "private_key_pem": gh_fakes.PRIVATE_KEY_PEM, "pat": gh_fakes.PAT}}))
    ok(client.post("/v1/admin/plugins/github/enable", headers=admin))
    install = ok(client.post("/v1/admin/plugins/github/connect/start", headers=admin))
    secrets["github state"] = install["state"]
    ok(client.post("/v1/admin/plugins/github/connect/finish", headers=admin,
                   json={"installation_id": gh_fakes.INSTALLATION_ID,
                         "state": install["state"]}))

    # ---- the monitor: the summary over the probe, as Bearer and as Basic auth ---------
    for headers in ({"Authorization": f"Bearer {secrets['monitor token']}"},
                    {"Authorization": f"Basic {monitor_basic}"}):
        assert client.get("/v1/health", headers=headers).status_code in (200, 503)
    assert client.get("/v1/health", headers={"Authorization": f"Bearer {BOGUS_KEY}"}
                      ).status_code == 401
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {secrets['monitor token']}"}
                      ).status_code == 401

    # ---- Telegram: the bot token and a link code -----------------------------------
    ok(client.post("/v1/admin/telegram/token", headers=admin, json={"token": BOT_TOKEN}))
    secrets["telegram link code"] = ok(client.post("/v1/admin/telegram/link/start",
                                                   headers=admin))["code"]

    # ---- the installer's GitHub token: stored, then relayed with an inspect ----------
    monkeypatch.setenv("INSTALLER_URL", INSTALLER_URL)
    monkeypatch.setenv("INSTALLER_TOKEN", INSTALLER_TOKEN)
    get_settings.cache_clear()
    installer = FakeInstaller()
    monkeypatch.setattr(plugin_install, "client_factory",
                        lambda base, headers, timeout: TestClient(installer.app, base_url=base,
                                                                  headers=headers))
    ok(client.post("/v1/admin/plugins/install/git-token", headers=admin,
                   json={"token": GIT_TOKEN}))
    ok(client.post("/v1/admin/plugins/install/inspect", headers=admin,
                   json={"source": PACKAGE_SOURCE, "ref": "v0.1.0"}))
    ok(client.get("/v1/admin/plugins/install/status", headers=admin))
    # It really travelled (so the sweep looked at a real relay), in the body only.
    [inspected] = installer.calls("/inspect")
    assert inspected["body"]["git_token"] == GIT_TOKEN
    assert inspected["token"] == INSTALLER_TOKEN

    # ---- agent keys: create, use, rotate, delegate, a refused one --------------------
    key = ok(client.post("/v1/admin/keys", headers=admin, json={
        "name": "sweeper", "role": "full", "rate_per_min": 60, "capabilities": [
            cap(["list_items", "post_item"]),
            {"target": "gmail", "actions": ["search_threads"]},
            {"target": "github", "actions": ["get_file"]}]}))
    secrets["agent key"] = key["key"]
    agent = {"Authorization": f"Bearer {key['key']}"}
    ok(client.post("/v1/targets/echo/actions/post_item", headers=agent,
                   json={"params": {"room": "r1", "text": "hello"}}))
    ok(client.post("/v1/targets/gmail/actions/search_threads", headers=agent,
                   json={"params": {}}))
    ok(client.post("/v1/targets/github/actions/get_file", headers=agent,
                   json={"params": {"repo": "octo/a", "path": "README.md"}}))
    assert client.get("/v1/targets", headers={"Authorization": f"Bearer {BOGUS_KEY}"}
                      ).status_code == 401
    child = ok(client.post("/v1/delegations", headers=agent, json={
        "name": "helper", "capabilities": [cap(["list_items"])]}))
    secrets["delegated key"] = child["key"]
    secrets["rotated key"] = ok(client.post(f"/v1/admin/keys/{key['id']}/rotate",
                                            headers=admin))["key"]

    # ---- the owner again: password change, logout -------------------------------------
    ok(client.post("/v1/admin/password", headers=CSRF_HEADERS, json={
        "current_password": PASSWORD, "new_password": NEW_PASSWORD}))
    ok(client.post("/auth/logout", headers=CSRF_HEADERS))

    for token in google.tokens:
        secrets[f"google access token {token[:12]}"] = token
    for token in gh.tokens:
        secrets[f"github installation token {token[:8]}"] = token
    out = capsys.readouterr()
    ratelimit.reset()
    return {"secrets": secrets, "records": list(caplog.records), "caplog": caplog.text,
            "output": out.out + out.err}


def everything(sweep) -> str:
    return "\n".join([sweep["caplog"], sweep["output"], *(
        f"{r.name} {r.getMessage()} {r.exc_text or ''} {r.stack_info or ''}"
        for r in sweep["records"])])


def leaked(secrets: dict, text: str) -> list[str]:
    return sorted(name for name, value in secrets.items() if value and value in text)


def test_no_secret_appears_in_any_record_or_line(sweep):
    assert leaked(sweep["secrets"], everything(sweep)) == []


def test_the_detector_catches_a_planted_leak(sweep):
    planted = everything(sweep) + " oops " + sweep["secrets"]["admin token"]
    assert leaked(sweep["secrets"], planted) == ["admin token"]


def test_the_sweep_is_not_vacuous(sweep):
    """Every flow ran and logged: the checks above looked at real lines."""
    secrets, messages = sweep["secrets"], [r.getMessage() for r in sweep["records"]]
    assert any(n.startswith("google access token ") for n in secrets)
    assert any(n.startswith("github installation token ") for n in secrets)
    for expected in ("owner setup refused", "owner setup completed", "owner login failed",
                     "owner logged in", "admin token created", "plugin configured",
                     "plugin connect finished", "google connected",
                     "google access token source=refreshed",
                     "github installation token source=minted", "telegram bot token stored",
                     "telegram link started", "secret stored slot=broker",
                     "secret stored slot=broker name=installer_git_token",
                     "installer github token stored", "plugin package inspected",
                     "secret slot written slot=echo", "key created", "decision decision=allow",
                     "perform plugin=gmail", "agent authentication failed reason=unknown_key",
                     "delegation created", "key rotated", "owner password changed",
                     "owner logged out", "request method=POST path=/auth/login"):
        assert any(m.startswith(expected) or expected in m for m in messages), expected
    # The handler wrote the same lines, formatted.
    assert "INFO broker.access [broker " in sweep["output"]
