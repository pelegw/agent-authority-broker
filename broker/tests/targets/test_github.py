"""The GitHub plugin end to end: the real plugin app (plugins/github) served by
the plugin runtime, discovered and pinned by the registry, reached through
RemoteAdapter, configured and connected through the broker's admin API, and
driven through REST and MCP. GitHub itself is the scripted fake from
plugins/github/tests/fakes.py, which enforces installation-token scope the
way GitHub does.

What must hold across the broker/plugin boundary:
  * the vendored manifest is byte-identical to the plugin's own;
  * the App key and PAT pass through the broker once and are stored nowhere
    broker-side;
  * a read-only key's call mints a token with contents:read only, for only
    the addressed repository;
  * a key whose repo selector is ["octo/a"] cannot reach octo/b: the broker
    refuses (403) and no token is minted at all;
  * enforced_where reports repo/permissions as "target" with the App and
    "proxy" with the PAT fallback;
  * a hidden repository is the same 404 as a missing one on every surface
    (REST, MCP, list_repos, resolve), and never reaches GitHub;
  * GitHub failures follow the 503/502 contract into the decision record.

Registration helpers live here (not in targets/conftest.py) so this lane
touches no shared fixture file.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from broker import db, hidden
from broker.plugins.registry import TARGETS_DIR, Registry, get_registry

from ..mcp_helpers import call as mcp_call, live, text_json  # noqa: F401  (live: fixture)

REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB_DIR = REPO_ROOT / "plugins" / "github"
try:
    import aab_plugin_github  # noqa: F401
except ImportError:
    sys.path.insert(0, str(GITHUB_DIR))

GH_TOKEN = "github-plugin-token-for-broker-tests-0123"
ACT = "/v1/targets/github/actions"
NOT_FOUND = {"error": "not found", "code": "not_found"}
READS = ["list_repos", "list_issues", "get_issue", "get_file", "list_prs"]


def _load_fakes():
    spec = importlib.util.spec_from_file_location(
        "aab_github_test_fakes", GITHUB_DIR / "tests" / "fakes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fakes = _load_fakes()

# One valid call per repository-addressed action (params other than repo).
REPO_CALLS = {
    "list_issues": {},
    "get_issue": {"number": 1},
    "get_file": {"path": "README.md"},
    "list_prs": {},
    "create_issue": {"title": "t"},
    "comment_issue": {"number": 1, "body": "b"},
    "close_issue": {"number": 1},
    "create_branch": {"branch": "agent/new"},
    "push_file": {"branch": "dev", "path": "n.txt", "content": "x", "message": "m"},
    "create_pr": {"head": "dev", "base": "main", "title": "t"},
    "merge_pr": {"number": 2},
    "delete_branch": {"branch": "dev"},
}


# ---- registration (the WhatsApp pattern, for the github service) ------------------

def register_github(adapter, tmp_path, token: str = GH_TOKEN):
    """Serve `adapter` through the real plugin-github app and let the registry
    discover it as service `github`, exactly as in production."""
    from aab_plugin_github.main import create_app
    from cryptography.fernet import Fernet

    from tests.conftest import runtime_factory

    runtime = create_app({"PLUGIN_TOKEN": token,
                          "PLUGIN_SECRETS_KEY": Fernet.generate_key().decode(),
                          "PLUGIN_SECRETS_DIR": str(tmp_path / "gh-plugin-secrets")},
                         adapter=adapter)
    get_registry().discover({"github": ("http://plugin-github:8090", token)},
                            client_factory=runtime_factory(runtime))
    assert "github" in get_registry().entries(), get_registry().refused
    return runtime


@pytest.fixture()
def gh_disabled(env, owner, tmp_path):
    """The real GitHub plugin over the fake GitHub, registered but not enabled."""
    from aab_plugin_github.adapter import GitHubAdapter
    from aab_plugin_github.api import GitHubAPI

    clock = fakes.Clock()
    github = fakes.FakeGitHub(clock)
    adapter = GitHubAdapter(GitHubAPI(transport=github.transport(), clock=clock), clock=clock)
    runtime = register_github(adapter, tmp_path)
    return types.SimpleNamespace(gh=github, adapter=adapter, runtime=runtime, clock=clock)


def _admin(client, headers, method, path, body=None):
    r = client.request(method, f"/v1/admin/plugins/github{path}", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def gh_app(client, gh_disabled, admin_headers):
    """App mode through the console's admin API: config, enable, install."""
    _admin(client, admin_headers, "PATCH", "", {"config": {
        "app_id": fakes.APP_ID, "app_slug": fakes.APP_SLUG,
        "private_key_pem": fakes.PRIVATE_KEY_PEM}})
    _admin(client, admin_headers, "POST", "/enable")
    start = _admin(client, admin_headers, "POST", "/connect/start")
    assert start["kind"] == "install"
    _admin(client, admin_headers, "POST", "/connect/finish",
           {"installation_id": fakes.INSTALLATION_ID, "state": start["state"]})
    return gh_disabled


@pytest.fixture()
def gh_pat(client, gh_disabled, admin_headers):
    """PAT fallback through the admin API: every dimension proxy-enforced."""
    _admin(client, admin_headers, "PATCH", "", {"config": {"pat": fakes.PAT}})
    _admin(client, admin_headers, "POST", "/enable")
    return gh_disabled


def gh_cap(actions, **kw):
    return {"target": "github", "actions": list(actions), **kw}


def call(client, agent, action, params=None, **body):
    return client.post(f"{ACT}/{action}", json={"params": params or {}, **body},
                       headers=agent.headers)


def items(r) -> list[dict]:
    assert r.status_code == 200, r.text
    return r.json()["items"]


def outcomes() -> list[str]:
    with db.connect() as conn:
        return [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM decisions WHERE kind = 'outcome' ORDER BY id")]


def ledger_states() -> list[str]:
    with db.connect() as conn:
        return [r["state"] for r in conn.execute("SELECT state FROM capacity_ledger ORDER BY id")]


# ---- the manifest, discovery and config --------------------------------------------

def test_vendored_manifest_is_byte_identical_to_the_plugins():
    plugin = (GITHUB_DIR / "aab_plugin_github" / "manifest.yaml").read_bytes()
    vendored = (TARGETS_DIR / "github" / "manifest.yaml").read_bytes()
    assert plugin == vendored, ("broker/broker/targets/github/manifest.yaml drifted from "
                                "plugins/github/aab_plugin_github/manifest.yaml")


def test_manifest_config_is_console_config_with_two_secrets():
    m = Registry().vendored("github")
    fields = {f.name: (f.type, f.secret) for f in m.config_schema}
    assert fields == {"app_id": ("string", False), "app_slug": ("string", False),
                      "private_key_pem": ("text", True), "private_key_path": ("string", False),
                      "pat": ("string", True)}
    assert m.connection.kind == "github_app" and m.connection.enforcement == "target"
    dims = {n.dimension: (n.form, n.enforcement) for n in m.narrowings}
    assert dims == {"repo": ("list", "target"), "permissions": ("level", "target"),
                    "branch": ("pattern", "proxy")}


def test_discovery_pins_the_plugin_to_the_vendored_manifest(gh_disabled):
    reg = get_registry()
    e = reg.entries()["github"]
    assert e.service == "github" and type(e.adapter).__name__ == "RemoteAdapter"
    assert e.manifest.model_dump() == reg.vendored("github").model_dump()
    assert reg.refused == {}


def test_secrets_pass_through_the_broker_and_stay_in_the_plugin(client, gh_app, env, tmp_path):
    row = get_registry().plugin_rows()["github"]
    assert row["config"] == {"app_id": fakes.APP_ID, "app_slug": fakes.APP_SLUG}
    pem_line = fakes.PRIVATE_KEY_PEM.splitlines()[1].encode()
    for path in tmp_path.glob("broker.db*"):
        assert pem_line not in path.read_bytes()
    with db.connect() as conn:
        audit = " ".join(str(dict(r)) for r in conn.execute("SELECT * FROM audit_log"))
    assert "BEGIN PRIVATE KEY" not in audit and "private_key_pem" in audit  # name only
    store = gh_app.runtime.state.secret_store
    assert store.read_all("github")["private_key_pem"] == fakes.PRIVATE_KEY_PEM


def test_app_mode_is_connected_and_target_enforced(client, gh_app, admin_headers):
    view = _admin(client, admin_headers, "GET", "")
    assert view["connected"] is True
    assert view["last_health"]["enforcement"] == "target"
    assert view["last_health"]["connection"]["account"] == "octo"


# ---- minimal tokens ------------------------------------------------------------------

def test_a_read_only_keys_call_mints_contents_read_only(client, gh_app, make_agent):
    a = make_agent([gh_cap(["*"])], role="read-only")
    before = len(gh_app.gh.token_requests())
    r = call(client, a, "get_file", {"repo": "Octo/A", "path": "README.md"})
    assert r.status_code == 200 and r.json()["content"] == "hello from Octo/A"
    assert gh_app.gh.token_requests()[before:] == [
        {"permissions": {"contents": "read"}, "repositories": ["a"]}]
    # The role caps it at reads: a write never gets as far as a token.
    assert call(client, a, "create_issue", {"repo": "octo/a", "title": "t"}).status_code == 403
    assert all(set(b["permissions"].values()) == {"read"}
               for b in gh_app.gh.token_requests())


def test_a_write_mints_only_that_writes_permission(client, gh_app, make_agent):
    a = make_agent([gh_cap(["push_file"])])
    before = len(gh_app.gh.token_requests())
    r = call(client, a, "push_file", {"repo": "octo/a", "branch": "dev", "path": "x.md",
                                      "content": "x", "message": "m"})
    assert r.status_code == 200, r.text
    assert gh_app.gh.token_requests()[before:] == [
        {"permissions": {"contents": "write"}, "repositories": ["a"]}]


def test_a_repo_selector_stops_other_repos_at_the_broker(client, gh_app, make_agent):
    a = make_agent([gh_cap(READS, selector={"repo": ["octo/a"]})])
    before_tokens, before = len(gh_app.gh.token_requests()), len(gh_app.gh.requests)
    r = call(client, a, "get_file", {"repo": "octo/b", "path": "README.md"})
    assert r.status_code == 403 and r.json()["code"] == "out_of_grant"
    # Refused by the broker: the plugin was never asked, GitHub never saw it.
    assert gh_app.gh.token_requests()[before_tokens:] == []
    assert gh_app.gh.requests[before:] == []
    ok = call(client, a, "get_file", {"repo": "octo/a", "path": "README.md"})
    assert ok.status_code == 200
    assert gh_app.gh.token_requests()[-1] == {"permissions": {"contents": "read"},
                                              "repositories": ["a"]}
    # list_repos is narrowed at GitHub too: the token names only octo/a.
    assert [i["repo"] for i in items(call(client, a, "list_repos"))] == ["octo/a"]
    assert gh_app.gh.token_requests()[-1] == {"permissions": {"metadata": "read"},
                                              "repositories": ["a"]}


# ---- enforced_where --------------------------------------------------------------------

def test_app_mode_reports_target_enforcement(client, gh_app, make_agent):
    a = make_agent([gh_cap(["get_file", "push_file"])])
    where = client.get("/v1/me", headers=a.headers).json()["targets"]["github"][
        "enforced_where"]
    assert where["repo"] == "target" and where["permissions"] == "target"
    assert where["branch"] == "proxy"          # branch patterns are ours, not GitHub's


def test_pat_mode_reports_proxy_enforcement(client, gh_pat, make_agent):
    a = make_agent([gh_cap(["get_file", "push_file"])])
    where = client.get("/v1/me", headers=a.headers).json()["targets"]["github"][
        "enforced_where"]
    assert where["repo"] == "proxy" and where["permissions"] == "proxy"
    r = call(client, a, "get_file", {"repo": "octo/a", "path": "README.md"})
    assert r.status_code == 200
    assert gh_pat.gh.token_requests() == []       # a PAT is never "narrowed"


def test_decisions_record_where_each_call_was_enforced(client, gh_app, make_agent):
    a = make_agent([gh_cap(["get_file"])])
    call(client, a, "get_file", {"repo": "octo/a", "path": "README.md"})
    with db.connect() as conn:
        row = conn.execute("SELECT enforced_where FROM decisions WHERE kind = 'decision'"
                           " ORDER BY id DESC LIMIT 1").fetchone()
    where = json.loads(row["enforced_where"])
    assert where["repo"] == "target" and where["permissions"] == "target"


# ---- hidden == 404 on every surface ------------------------------------------------------

@pytest.mark.parametrize("action", sorted(REPO_CALLS))
def test_a_hidden_repo_is_the_same_404_as_a_missing_one(client, gh_app, make_agent, action):
    a = make_agent([gh_cap(["*"])])
    hidden.add("github", "repo", "octo/secret")
    before_tokens, before = len(gh_app.gh.token_requests()), len(gh_app.gh.requests)
    hid = call(client, a, action, {"repo": "octo/secret", **REPO_CALLS[action]})
    gone = call(client, a, action, {"repo": "octo/missing", **REPO_CALLS[action]})
    assert hid.status_code == gone.status_code == 404
    assert hid.json() == gone.json() == NOT_FOUND
    assert not any("secret" in r.path for r in gh_app.gh.requests[before:])
    assert all(b.get("repositories") != ["secret"]
               for b in gh_app.gh.token_requests()[before_tokens:])


def test_hidden_via_the_admin_api_is_normalized_by_the_plugin(client, gh_app, make_agent,
                                                              admin_headers):
    a = make_agent([gh_cap(READS)])
    r = client.post("/v1/admin/hidden", json={"target": "github", "kind": "repo",
                                              "resource_id": "Octo/Secret"},
                    headers=admin_headers)
    assert r.status_code == 200 and r.json()["resource_id"] == "octo/secret"
    alias = call(client, a, "get_file", {"repo": "OCTO/secret", "path": "README.md"})
    assert alias.status_code == 404 and alias.json() == NOT_FOUND


def test_a_hidden_repo_is_absent_from_lists_and_resolve(client, gh_app, make_agent):
    a = make_agent([gh_cap(READS)])
    hidden.add("github", "repo", "octo/secret")
    listed = [i["repo"] for i in items(call(client, a, "list_repos"))]
    assert listed == ["octo/a", "octo/b"]
    found = client.get("/v1/targets/github/resolve", params={"kind": "repo", "q": "octo"},
                       headers=a.headers).json()["items"]
    assert [f["id"] for f in found] == ["octo/a", "octo/b"]
    me = client.get("/v1/me", headers=a.headers).text
    assert "secret" not in me


def test_a_hidden_repo_is_404_over_mcp_too(live, gh_app, make_agent):
    a = make_agent([gh_cap(READS)])
    hidden.add("github", "repo", "octo/secret")
    r = mcp_call(live, a.headers, "github_get_file", {"repo": "octo/secret",
                                                       "path": "README.md"})
    assert r["isError"] is True and text_json(r)["code"] == "not_found"
    ok = mcp_call(live, a.headers, "github_get_file", {"repo": "octo/a", "path": "README.md"})
    assert ok["isError"] is False
    assert not any("secret" in r.path for r in gh_app.gh.requests)


def test_a_keys_own_deny_hides_a_repo(client, gh_app, make_agent):
    a = make_agent([gh_cap(READS)], denies={"github": {"repo": ["octo/b"]}})
    assert "octo/b" not in [i["repo"] for i in items(call(client, a, "list_repos"))]
    r = call(client, a, "list_issues", {"repo": "octo/b"})
    assert r.status_code == 404 and r.json() == NOT_FOUND


# ---- branches, drafts and the error contract ---------------------------------------------

def test_the_branch_selector_holds_through_the_broker(client, gh_app, make_agent):
    a = make_agent([gh_cap(["push_file"], selector={"branch": ["agent/work"]})])
    before = len(gh_app.gh.token_requests())
    r = call(client, a, "push_file", {"repo": "octo/a", "branch": "main", "path": "x",
                                      "content": "x", "message": "m"})
    assert r.status_code == 403
    assert gh_app.gh.token_requests()[before:] == []
    assert gh_app.gh.calls("PUT", "/repos/") == []


def test_a_draft_capability_queues_the_write_for_a_human(client, gh_app, make_agent,
                                                         admin_headers):
    a = make_agent([gh_cap(["create_issue"], mode="draft")])
    r = call(client, a, "create_issue", {"repo": "Octo/A", "title": "From the agent"})
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"
    assert gh_app.gh.calls("POST", "/repos/") == []
    done = client.post(f"/v1/admin/actions/{r.json()['action_id']}/approve",
                       headers=admin_headers)
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert gh_app.gh.repos["octo/a"].issues[3]["title"] == "From the agent"


@pytest.mark.parametrize("failure,status,code,outcome,ledger", [
    ("connect", 503, "unavailable", "unavailable", "released"),
    ("read_timeout", 502, "unknown_outcome", "unknown", "reserved"),
    (500, 502, "unknown_outcome", "unknown", "reserved"),
])
def test_github_failures_follow_the_contract(client, gh_app, make_agent, failure, status,
                                             code, outcome, ledger):
    a = make_agent([gh_cap(["create_issue"])])
    gh_app.gh.fail("POST", r"/repos/octo/a/issues$", failure)
    r = call(client, a, "create_issue", {"repo": "octo/a", "title": "t"})
    assert r.status_code == status and r.json()["code"] == code
    assert outcomes() == [outcome]
    assert ledger_states() == [ledger]     # 503 frees the budget; 502 keeps it


def test_a_github_rate_limit_is_429_and_frees_the_budget(client, gh_app, make_agent):
    a = make_agent([gh_cap(["create_issue"])])
    gh_app.gh.rate_limit("POST", r"/repos/octo/a/issues$")
    r = call(client, a, "create_issue", {"repo": "octo/a", "title": "t"})
    assert r.status_code == 429 and "retry after" in r.json()["error"]
    assert ledger_states() == ["released"]


def test_a_missing_installed_permission_is_a_clear_403(client, gh_app, make_agent):
    gh_app.gh.installations[fakes.INSTALLATION_ID]["permissions"] = {
        "metadata": "read", "contents": "read"}
    gh_app.clock.advance(601)
    a = make_agent([gh_cap(["create_issue"])])
    r = call(client, a, "create_issue", {"repo": "octo/a", "title": "t"})
    assert r.status_code == 403
    assert r.json()["error"] == "installation lacks permission issues:write"


def test_disconnect_takes_github_offline(client, gh_app, make_agent, admin_headers):
    a = make_agent([gh_cap(READS)])
    assert call(client, a, "list_issues", {"repo": "octo/a"}).status_code == 200
    _admin(client, admin_headers, "POST", "/disconnect")
    assert get_registry().plugin_rows()["github"]["connected"] == 0
    r = call(client, a, "list_issues", {"repo": "octo/a"})
    assert r.status_code == 503 and r.json()["code"] == "not_connected"


def test_the_plugin_refuses_a_scope_sent_straight_to_it(gh_app):
    """Belt and braces: even a caller that skipped the broker's checks gets
    a 404 for a denied repository, with no token minted."""
    from broker.plugins.adapter import AdapterError, CallScope
    before = len(gh_app.gh.token_requests())
    with pytest.raises(AdapterError) as e:
        get_registry().adapter("github").perform(
            "get_file", {"repo": "octo/secret", "path": "README.md"},
            CallScope("direct", visibility={"repo": {"deny": ["octo/secret"],
                                                     "allow_only": None}},
                      credential={"permissions": {"contents": "read"}}))
    assert e.value.status == 404
    assert gh_app.gh.token_requests()[before:] == []
