"""The Google plugin service end to end: the real plugin package
(plugins/google: gmail, gcal, gdrive over one google_oauth connection)
served by the plugin runtime, discovered and pinned by the registry as ONE
service, reached through RemoteAdapter, driven through the owner's admin
routes, engine.perform and the REST surface, against the fake Google
(plugins/google/tests/fake_google.py, loaded by path).

What must hold across the broker/plugin boundary:
  * the three vendored manifests are byte-identical to the plugin's own;
  * the OAuth client secret is relayed once and stored once, in the
    plugin's shared `google` slot, never in broker.db; the client id is
    kept identical on the three plugins;
  * connect start/finish run through the broker's admin routes with the
    redirect URI the broker computed, and the code is recorded nowhere;
  * a read-only Gmail key's call refreshes with gmail.readonly only
    (target-enforced), and get_my_access says so; labels are proxy;
  * a label-restricted key sees 404 on a thread outside its labels,
    exactly like a missing one; hidden threads and folders likewise.

The registration helper lives here (not in targets/conftest.py) so this
lane touches no shared fixture file.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from broker import db, engine, hidden
from broker.authority.capability import from_json, normalize_all
from broker.config import get_settings
from broker.errors import PolicyError
from broker.plugins.registry import TARGETS_DIR, Registry, get_registry
from broker.policy import evaluate
from broker.services import plugins_admin

REPO = Path(__file__).resolve().parents[3]
GOOGLE_DIR = REPO / "plugins" / "google"
try:
    import aab_plugin_google  # noqa: F401
except ImportError:
    sys.path.insert(0, str(GOOGLE_DIR))

GOOGLE_TOKEN = "google-plugin-token-for-broker-tests-0123"
PLUGINS = ("gmail", "gcal", "gdrive")
# A local deploy is reached on loopback; the broker builds the redirect from Host.
LOCAL_HOST = "localhost:8080"
REDIRECT = f"http://{LOCAL_HOST}/oauth/callback/google"
NOT_FOUND = {"error": "not found", "code": "not_found"}


def _load_fake():
    spec = importlib.util.spec_from_file_location(
        "aab_google_test_fake", GOOGLE_DIR / "tests" / "fake_google.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fg = _load_fake()


def register_google(tmp_path, google):
    """Serve the three adapters through the real runtime and let the registry
    discover them as ONE service `google`, exactly as in production."""
    from aab_plugin_google.main import build_adapters
    from aab_plugin_runtime import serve
    from cryptography.fernet import Fernet

    from tests.conftest import runtime_factory

    adapters = build_adapters(transport=google.transport())
    runtime = serve(adapters, GOOGLE_TOKEN, tmp_path / "google-plugin-secrets",
                    Fernet.generate_key().decode())
    get_registry().discover({"google": ("http://plugin-google:8090", GOOGLE_TOKEN)},
                            client_factory=runtime_factory(runtime))
    assert set(PLUGINS) <= set(get_registry().entries()), get_registry().refused
    return runtime, adapters


@pytest.fixture()
def gg(env, owner, tmp_path):
    """plugin-google registered (pinned), nothing configured or enabled."""
    google = fg.FakeGoogle()
    runtime, adapters = register_google(tmp_path, google)
    return types.SimpleNamespace(google=google, runtime=runtime, adapters=adapters,
                                 connection=adapters[0].connection)


def configure_and_enable(client, admin_headers, plugins=PLUGINS):
    r = client.patch("/v1/admin/plugins/gmail", headers=admin_headers, json={"config": {
        "client_id": fg.CLIENT_ID, "client_secret": fg.CLIENT_SECRET}})
    assert r.status_code == 200, r.text
    for pid in plugins:
        r = client.post(f"/v1/admin/plugins/{pid}/enable", headers=admin_headers)
        assert r.status_code == 200, r.text


def connect(client, admin_headers, google) -> dict:
    google.expected_redirect = REDIRECT
    start = client.post("/v1/admin/plugins/google/connect/start",
                        headers={**admin_headers, "Host": LOCAL_HOST})
    assert start.status_code == 200, start.text
    q = {k: v[0] for k, v in parse_qs(urlsplit(start.json()["url"]).query).items()}
    fin = client.post("/v1/admin/plugins/google/connect/finish", headers=admin_headers,
                      json={"code": fg.AUTH_CODE, "state": q["state"]})
    assert fin.status_code == 200, fin.text
    return q


@pytest.fixture()
def gconn(gg, client, admin_headers):
    """plugin-google configured, all three enabled, connected via the admin routes."""
    configure_and_enable(client, admin_headers)
    connect(client, admin_headers, gg.google)
    return gg


def cap(target, actions, **kw):
    return {"target": target, "actions": list(actions), **kw}


def call(client, agent, target, action, params=None):
    return client.post(f"/v1/targets/{target}/actions/{action}",
                       json={"params": params or {}}, headers=agent.headers)


def db_text() -> str:
    with db.connect() as conn:
        rows = []
        for t in ("plugins", "plugin_secrets", "audit_log", "decisions", "app_config",
                  "actions"):
            rows += [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
    return json.dumps(rows, default=str)


# ---- manifests and discovery --------------------------------------------------------

@pytest.mark.parametrize("pid", PLUGINS)
def test_vendored_manifest_is_byte_identical_to_the_plugins(pid):
    plugin = (GOOGLE_DIR / "aab_plugin_google" / "manifests" / f"{pid}.yaml").read_bytes()
    vendored = (TARGETS_DIR / pid / "manifest.yaml").read_bytes()
    assert plugin == vendored, (f"broker/broker/targets/{pid}/manifest.yaml drifted from "
                                f"plugins/google/aab_plugin_google/manifests/{pid}.yaml")


@pytest.mark.parametrize("pid", PLUGINS)
def test_vendored_manifest_loads_with_shared_oauth_config(pid):
    m = Registry().vendored(pid)
    assert (m.connection.kind, m.connection.shared, m.connection.enforcement) == (
        "google_oauth", "google", "target")
    assert {f.name: (f.secret, f.shared) for f in m.config_schema} == {
        "client_id": (False, True), "client_secret": (True, True)}


def test_one_service_serves_all_three(gg):
    reg = get_registry()
    for pid in PLUGINS:
        e = reg.entries()[pid]
        assert e.service == "google" and type(e.adapter).__name__ == "RemoteAdapter"
        assert e.manifest.model_dump() == reg.vendored(pid).model_dump()
    assert reg.refused == {}


# ---- the shared Google account config -------------------------------------------------

def test_client_secret_is_relayed_once_and_stored_once_in_the_google_slot(gg, client,
                                                                          admin_headers):
    configure_and_enable(client, admin_headers)
    store = gg.runtime.state.secret_store
    assert store.read_all("google")["client_secret"] == fg.CLIENT_SECRET
    for pid in PLUGINS:
        assert store.read_all(pid) == {}                   # no per-plugin copies
    assert fg.CLIENT_SECRET not in db_text()               # never in broker.db


def test_client_id_is_kept_identical_on_all_three(gg, client, admin_headers):
    client.patch("/v1/admin/plugins/gdrive", headers=admin_headers,
                 json={"config": {"client_id": fg.CLIENT_ID}})
    for pid in PLUGINS:
        view = client.get(f"/v1/admin/plugins/{pid}", headers=admin_headers).json()
        assert view["config"]["client_id"] == fg.CLIENT_ID
        assert view["connection"]["shared"] == "google"
        assert {f["name"]: f["shared"] for f in view["config_schema"]} == {
            "client_id": True, "client_secret": True}


def test_a_shared_client_id_reaches_the_connection_even_from_a_disabled_plugin(
        gg, client, admin_headers):
    configure_and_enable(client, admin_headers, plugins=["gcal"])
    client.patch("/v1/admin/plugins/gdrive", headers=admin_headers,
                 json={"config": {"client_id": "rotated-client-id"}})     # gdrive is disabled
    assert gg.runtime.state.secret_store.read_all("google")["client_id"] == "rotated-client-id"
    view = client.get("/v1/admin/plugins/gcal", headers=admin_headers).json()
    assert view["config"]["client_id"] == "rotated-client-id"


def test_enable_needs_the_client_id(gg, client, admin_headers):
    r = client.post("/v1/admin/plugins/gcal/enable", headers=admin_headers)
    assert r.status_code == 400 and "client_id" in r.json()["error"]


# ---- connect through the broker's admin routes ------------------------------------------

def test_connect_start_and_finish_with_the_computed_redirect_uri(gg, client, admin_headers):
    configure_and_enable(client, admin_headers)
    q = connect(client, admin_headers, gg.google)
    assert q["redirect_uri"] == REDIRECT
    # The plugin reused the stored redirect URI for the exchange.
    assert gg.google.token_forms[-1]["redirect_uri"] == REDIRECT
    rows = get_registry().plugin_rows()
    for pid in PLUGINS:
        assert rows[pid]["connected"] == 1
        assert rows[pid]["last_health"]["health"] == "ok"
    text = db_text()
    for secret in (fg.AUTH_CODE, fg.REFRESH_TOKEN, fg.CLIENT_SECRET, q["state"]):
        assert secret not in text                          # relayed once, recorded nowhere


def test_consent_asks_for_the_enabled_plugins_only(gg, client, admin_headers):
    configure_and_enable(client, admin_headers, plugins=["gdrive"])
    start = client.post("/v1/admin/plugins/google/connect/start",
                        headers={**admin_headers, "Host": LOCAL_HOST})
    scope = parse_qs(urlsplit(start.json()["url"]).query)["scope"][0].split()
    assert set(scope) == {fg.DRIVE_RO, fg.DRIVE}


def test_enabling_another_plugin_later_reports_missing_scopes(gg, client, admin_headers):
    configure_and_enable(client, admin_headers, plugins=["gmail"])
    gg.google.granted = [s for s in fg.ALL_SCOPES if "calendar" not in s and "drive" not in s]
    connect(client, admin_headers, gg.google)
    r = client.post("/v1/admin/plugins/gcal/enable", headers=admin_headers)
    health = r.json()["last_health"]
    assert health["health"] == "reconnect needed: scopes missing"
    assert health["missing_scopes"] == ["calendar.events", "calendar.readonly"]


def test_redirect_uri_local_and_public(env, monkeypatch):
    assert plugins_admin.oauth_redirect_uri("google", "localhost:8080") == \
        "http://localhost:8080/oauth/callback/google"
    with pytest.raises(PolicyError) as exc:
        plugins_admin.oauth_redirect_uri("google", "evil.test/x?y=")
    assert exc.value.status == 400
    monkeypatch.setenv("ORIGIN_SECRET", "x" * 32)
    get_settings.cache_clear()
    monkeypatch.delenv("SITE_DOMAIN", raising=False)
    with pytest.raises(PolicyError) as exc:                 # public mode fails closed
        plugins_admin.oauth_redirect_uri("google", "localhost:8080")
    assert exc.value.status == 409
    monkeypatch.setenv("SITE_DOMAIN", "aab.example.com")
    # Public mode never trusts the Host header.
    assert plugins_admin.oauth_redirect_uri("google", "evil.test") == \
        "https://aab.example.com/oauth/callback/google"


def test_a_bad_host_refuses_google_connect(gg, client, admin_headers, admin_ctx):
    configure_and_enable(client, admin_headers)
    with pytest.raises(PolicyError) as exc:
        plugins_admin.connect_start(admin_ctx, "google", "bad host")
    assert exc.value.status == 400
    # A non-loopback http origin is refused by the plugin itself: Google
    # would send the code over plain http.
    r = client.post("/v1/admin/plugins/google/connect/start", headers=admin_headers)
    assert r.status_code == 400 and "localhost" in r.json()["error"]


def test_disconnect_via_the_admin_route(gconn, client, admin_headers):
    r = client.post("/v1/admin/plugins/google/disconnect", headers=admin_headers)
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert all(get_registry().plugin_rows()[p]["connected"] == 0 for p in PLUGINS)


# ---- target-enforced scopes -------------------------------------------------------------

def test_read_only_gmail_key_refreshes_with_gmail_readonly_only(gconn, client, make_agent):
    a = make_agent([cap("gmail", ["search_threads", "get_thread", "list_labels"])],
                   role="read-only")
    assert call(client, a, "gmail", "search_threads").status_code == 200
    assert call(client, a, "gmail", "get_thread", {"thread_id": "aaa1"}).status_code == 200
    assert gconn.google.refreshes == [(fg.GM_RO,)]
    assert call(client, a, "gmail", "send", {"to": ["x@example.com"]}).status_code == 403
    assert gconn.google.refreshes == [(fg.GM_RO,)]


def test_the_broker_asks_for_exactly_the_actions_scopes(gconn, make_agent):
    a = make_agent([cap("gmail", ["send"])])
    d = evaluate(a.auth, "gmail", "send", {"to": ["alice@example.com"]}, 0)
    assert d.scope.credential == {"permissions": {"gmail.metadata": "read",
                                                  "gmail.send": "write"}}


def test_get_my_access_reports_where_each_dimension_is_enforced(gconn, client, make_agent):
    a = make_agent([cap("gmail", ["search_threads"], selector={"label": ["Label_work"]})])
    body = client.get("/v1/me", headers=a.headers).json()
    where = body["targets"]["gmail"]["enforced_where"]
    assert where["scopes"] == "target" and where["label"] == "proxy"
    assert where["contact"] == "proxy" and where["date_window_days"] == "proxy"


# ---- proxy-enforced narrowing, end to end ----------------------------------------------

def test_label_restricted_key_gets_404_outside_its_labels(gconn, client, make_agent):
    a = make_agent([cap("gmail", ["search_threads", "get_thread"],
                        selector={"label": ["Label_work"]})])
    assert call(client, a, "gmail", "get_thread", {"thread_id": "aaa1"}).status_code == 200
    outside = call(client, a, "gmail", "get_thread", {"thread_id": "bbb2"})
    missing = call(client, a, "gmail", "get_thread", {"thread_id": "fff9"})
    assert outside.status_code == missing.status_code == 404
    assert outside.json() == missing.json() == NOT_FOUND
    listed = [t["id"] for t in call(client, a, "gmail", "search_threads").json()["items"]]
    assert listed == ["aaa1", "ddd4", "eee5"]


def test_hidden_thread_is_404_and_absent(gconn, client, make_agent):
    a = make_agent([cap("gmail", ["search_threads", "get_thread"])])
    hidden.add("gmail", "thread", "aaa1", "Quarterly report")
    assert call(client, a, "gmail", "get_thread", {"thread_id": "aaa1"}).json() == NOT_FOUND
    assert call(client, a, "gmail", "get_thread", {"thread_id": "AAA1"}).json() == NOT_FOUND
    listed = [t["id"] for t in call(client, a, "gmail", "search_threads").json()["items"]]
    assert "aaa1" not in listed


def test_hidden_label_hides_its_threads(gconn, client, make_agent):
    a = make_agent([cap("gmail", ["search_threads", "get_thread"])])
    hidden.add("gmail", "label", "Label_secret")
    assert call(client, a, "gmail", "get_thread", {"thread_id": "ccc3"}).json() == NOT_FOUND


def test_drive_folder_subtree_through_the_broker(gconn, client, make_agent):
    a = make_agent([cap("gdrive", ["list_files", "get_file_metadata"],
                        selector={"folder": ["fA"]})])
    # The broker checks list_files' folder itself (ancestors via the plugin)...
    assert call(client, a, "gdrive", "list_files", {"folder_id": "fA1"}).status_code == 200
    assert call(client, a, "gdrive", "list_files", {"folder_id": "fB"}).status_code == 403
    # ...and the plugin checks files (a different resource kind) by walking parents.
    assert call(client, a, "gdrive", "get_file_metadata", {"file_id": "doc1"}).status_code == 200
    assert call(client, a, "gdrive", "get_file_metadata",
                {"file_id": "img1"}).json() == NOT_FOUND


def test_hidden_folder_hides_everything_under_it(gconn, client, make_agent):
    a = make_agent([cap("gdrive", ["list_files", "get_file_metadata", "search_files"])])
    hidden.add("gdrive", "folder", "fA1")
    assert call(client, a, "gdrive", "list_files", {"folder_id": "fA1"}).json() == NOT_FOUND
    assert call(client, a, "gdrive", "get_file_metadata",
                {"file_id": "doc1"}).json() == NOT_FOUND
    found = [f["id"] for f in call(client, a, "gdrive", "search_files",
                                   {"query": "plan"}).json()["items"]]
    assert "doc1" not in found and "fA1" not in found


def test_positive_flags_survive_normalization_and_restrict(gconn, client, make_agent):
    # The grant algebra drops a flag's `true` as top; a restricting flag must
    # therefore be `false`, which is how every Google flag is phrased.
    caps = normalize_all([from_json(cap("gcal", ["get_event"], constraints={
        "private_events": False}))], get_registry().manifests())
    assert caps[0].constraints == {"private_events": False}
    dropped = normalize_all([from_json(cap("gcal", ["get_event"], constraints={
        "private_events": True}))], get_registry().manifests())
    assert dropped[0].constraints == {}
    a = make_agent([cap("gcal", ["get_event"], constraints={"private_events": False})])
    r = call(client, a, "gcal", "get_event", {"calendar_id": "primary", "event_id": "e2"})
    assert r.json() == NOT_FOUND
    ok = call(client, a, "gcal", "get_event", {"calendar_id": "primary", "event_id": "e1"})
    assert ok.status_code == 200 and ok.json()["summary"] == "Standup"


def test_hidden_event_reaches_the_plugin_as_a_deny(gconn, client, make_agent):
    a = make_agent([cap("gcal", ["list_events", "get_event"])])
    hidden.add("gcal", "event", "e1", "Standup")
    assert call(client, a, "gcal", "get_event",
                {"calendar_id": "primary", "event_id": "e1"}).json() == NOT_FOUND
    listed = [e.get("id") for e in call(client, a, "gcal", "list_events",
                                        {"calendar_id": "primary"}).json()["items"]]
    assert "e1" not in listed and "e4" in listed


def test_primary_alias_is_normalized_before_the_hidden_check(gconn, client, make_agent):
    a = make_agent([cap("gcal", ["list_events"])])
    hidden.add("gcal", "calendar", fg.OWNER)
    assert call(client, a, "gcal", "list_events", {"calendar_id": "primary"}).json() == NOT_FOUND


def test_attachment_download_over_rest(gconn, client, make_agent):
    a = make_agent([cap("gmail", ["get_attachment"])])
    r = call(client, a, "gmail", "get_attachment",
             {"thread_id": "aaa1", "message_id": "f011", "part_id": "1"})
    assert r.status_code == 200 and r.content == b"%PDF-report"


def test_draft_mode_send_is_pending_approval_and_sends_nothing(gconn, client, make_agent):
    a = make_agent([cap("gmail", ["send"], mode="draft")])
    r = call(client, a, "gmail", "send", {"to": ["alice@example.com"], "subject": "hi"})
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"
    assert gconn.google.gmail_writes == []


def test_engine_perform_send_within_the_contact_allow_set(gconn, make_agent):
    a = make_agent([cap("gmail", ["send"], selector={"contact": ["alice@example.com"]})])
    ok = engine.perform(a.auth, "gmail", "send", {"to": ["alice@example.com"]})
    assert ok.body["status"] == "sent"
    with pytest.raises(PolicyError) as exc:
        engine.perform(a.auth, "gmail", "send", {"to": ["bob@other.org"]})
    assert exc.value.status == 403


# ---- the two cross-cutting changes this plugin needed ------------------------------------

def test_shared_config_fields_need_a_shared_connection_slot():
    from broker.plugins.manifest import ManifestError, load_manifest_text
    text = (TARGETS_DIR / "gmail" / "manifest.yaml").read_text(encoding="utf-8")
    assert "  shared: google\n" in text
    with pytest.raises(ManifestError, match="connection.shared"):
        load_manifest_text(text.replace("  shared: google\n", ""))


def test_in_process_adapter_passes_redirect_only_to_a_start_that_takes_it():
    from broker.plugins.adapter import InProcessAdapter

    class OAuthish:
        def start(self, enabled_plugins, redirect_uri=None):
            return {"redirect": redirect_uri}

    class QRish:
        def start(self, enabled_plugins):
            return {"kind": "qr"}

    m = Registry().vendored("gmail")
    impl = types.SimpleNamespace(connection=OAuthish())
    uri = "https://aab.example.com/oauth/callback/google"
    assert InProcessAdapter(impl, m).connect_start(["gmail"], uri) == {"redirect": uri}
    impl.connection = QRish()
    assert InProcessAdapter(impl, m).connect_start(["gmail"], uri) == {"kind": "qr"}
