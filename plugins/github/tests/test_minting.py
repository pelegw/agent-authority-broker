"""Installation tokens: exactly the minimal permissions and repositories per
call, cached by the exact tuple, never wider than installed or requested."""

import pytest

from aab_plugin_github.connection import GitHubAppConnection

from .conftest import APP_CONFIG, NEEDS, configure, install, items, scope_for
from .fakes import PAT, PRIVATE_KEY_PEM

# One valid call per action, against octo/a. Branch-changing actions use
# fresh names so each succeeds on the fake.
CALLS = {
    "list_issues": {"repo": "octo/a"},
    "get_issue": {"repo": "octo/a", "number": 1},
    "get_file": {"repo": "octo/a", "path": "README.md"},
    "list_prs": {"repo": "octo/a"},
    "create_issue": {"repo": "octo/a", "title": "t"},
    "comment_issue": {"repo": "octo/a", "number": 1, "body": "b"},
    "close_issue": {"repo": "octo/a", "number": 1},
    "create_branch": {"repo": "octo/a", "branch": "agent/new"},
    "push_file": {"repo": "octo/a", "branch": "dev", "path": "new.txt", "content": "x",
                  "message": "m"},
    "create_pr": {"repo": "octo/a", "head": "dev", "base": "main", "title": "t"},
    "merge_pr": {"repo": "octo/a", "number": 2},
    "delete_branch": {"repo": "octo/a", "branch": "dev"},
}


@pytest.mark.parametrize("action", sorted(CALLS))
def test_each_action_mints_exactly_its_permissions_for_exactly_its_repo(
        app_mode, gh, perform, action):
    before = len(gh.token_requests())
    r = perform(action, CALLS[action], repos=["octo/a", "octo/b"])
    assert r.status_code == 200, r.text
    minted = gh.token_requests()[before:]
    # The capability allowed octo/a AND octo/b; the token covers only the
    # repository this call addresses, and only this action's permissions.
    assert minted == [{"permissions": NEEDS[action], "repositories": ["a"]}]


def test_read_and_write_request_bodies(app_mode, gh, perform):
    before = len(gh.token_requests())
    assert perform("get_file", CALLS["get_file"]).status_code == 200
    assert perform("push_file", CALLS["push_file"]).status_code == 200
    assert gh.token_requests()[before:] == [
        {"permissions": {"contents": "read"}, "repositories": ["a"]},
        {"permissions": {"contents": "write"}, "repositories": ["a"]},
    ]


def test_list_repos_mints_for_the_capability_list_minus_denied(app_mode, gh, perform):
    before = len(gh.token_requests())
    r = perform("list_repos", {}, repos=["octo/a", "octo/b", "octo/secret"],
                allow=["octo/a", "octo/b", "octo/secret"], deny=["octo/secret"])
    assert [i["repo"] for i in items(r)] == ["octo/a", "octo/b"]
    assert gh.token_requests()[before:] == [
        {"permissions": {"metadata": "read"}, "repositories": ["a", "b"]}]


def test_list_repos_unrestricted_mints_without_a_repository_list(app_mode, gh, perform):
    before = len(gh.token_requests())
    assert perform("list_repos", {}).status_code == 200
    assert gh.token_requests()[before:] in ([], [{"permissions": {"metadata": "read"}}])


def test_cache_hits_with_the_same_tuple_and_misses_with_another(app_mode, gh, perform):
    before = len(gh.token_requests())
    for _ in range(3):
        assert perform("get_file", CALLS["get_file"]).status_code == 200
    assert perform("list_issues", {"repo": "octo/a"}).status_code == 200    # other perms
    assert perform("get_file", {"repo": "octo/b", "path": "README.md"}).status_code == 200
    assert perform("get_file", CALLS["get_file"]).status_code == 200         # still cached
    assert gh.token_requests()[before:] == [
        {"permissions": {"contents": "read"}, "repositories": ["a"]},
        {"permissions": {"issues": "read"}, "repositories": ["a"]},
        {"permissions": {"contents": "read"}, "repositories": ["b"]},
    ]


def test_a_cached_token_is_reused_for_at_most_50_minutes(app_mode, gh, perform, clock):
    before = len(gh.token_requests())
    perform("get_file", CALLS["get_file"])
    clock.advance(49 * 60)
    perform("get_file", CALLS["get_file"])
    assert len(gh.token_requests()) - before == 1
    clock.advance(2 * 60)                         # 51 minutes after minting
    perform("get_file", CALLS["get_file"])
    assert len(gh.token_requests()) - before == 2


def test_a_permission_the_installation_lacks_is_403_before_any_token_request(
        app_mode, gh, perform, clock):
    gh.installations["777"]["permissions"] = {"metadata": "read", "contents": "read"}
    clock.advance(601)                            # installation info is re-read
    before = len(gh.token_requests())
    r = perform("push_file", CALLS["push_file"])
    assert r.status_code == 403
    assert r.json()["error"] == "installation lacks permission contents:write"
    assert gh.token_requests()[before:] == []
    assert gh.calls("PUT", "/repos/") == []


def test_permissions_removed_on_github_since_the_last_read_are_caught(app_mode, gh, perform):
    # The cached installation info still says issues:write; GitHub refuses
    # the token (422), the plugin re-reads the installation and says why.
    gh.installations["777"]["permissions"] = {"metadata": "read", "contents": "write"}
    r = perform("create_issue", CALLS["create_issue"])
    assert r.status_code == 403
    assert r.json()["error"] == "installation lacks permission issues:write"
    assert gh.calls("POST", "/repos/") == []


def test_a_token_wider_than_requested_is_refused_and_never_used(app_mode, gh, perform):
    gh.widen_tokens = True
    r = perform("get_file", CALLS["get_file"])
    assert r.status_code == 503
    assert "wider than requested" in r.json()["error"]
    assert gh.calls("GET", "/repos/") == []


def test_repositories_of_another_owner_are_never_sent_by_name(app_mode, gh, perform):
    # "x" would be resolved inside the installation's account (octo/x):
    # a repository under another owner is unreachable, not renamed.
    before = len(gh.token_requests())
    r = perform("get_file", {"repo": "evil/x", "path": "README.md"})
    assert r.status_code == 404
    assert gh.token_requests()[before:] == []


def test_a_list_of_only_foreign_repositories_is_empty_never_all(app_mode, gh, perform):
    before = len(gh.token_requests())
    r = perform("list_repos", {}, repos=["evil/x"], allow=["evil/x"])
    assert items(r) == []
    assert gh.token_requests()[before:] == []


def test_a_repository_outside_the_installation_is_404(app_mode, gh, perform):
    r = perform("get_file", {"repo": "octo/nope", "path": "README.md"})
    assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_list_repos_with_names_the_installation_lacks_keeps_the_rest(app_mode, gh, perform):
    r = perform("list_repos", {}, repos=["octo/a", "octo/nope"], allow=["octo/a", "octo/nope"])
    assert [i["repo"] for i in items(r)] == ["octo/a"]
    bodies = gh.token_requests()
    assert {"permissions": {"metadata": "read"}, "repositories": ["a"]} in bodies
    assert all(body.get("repositories") != [] for body in bodies)


def test_requirements_beyond_the_action_are_refused(app_mode, gh, perform):
    before = len(gh.token_requests())
    r = perform("get_file", CALLS["get_file"],
                permissions={"contents": "write"})           # get_file needs read
    assert r.status_code == 400
    r = perform("get_file", CALLS["get_file"],
                permissions={"contents": "read", "issues": "read"})
    assert r.status_code == 400
    assert gh.token_requests()[before:] == []


def test_missing_requirements_never_mint_everything(app_mode, gh, perform):
    before = len(gh.token_requests())
    for cred in ({}, {"permissions": {}}, None):
        scope = {**scope_for("get_file"), "credential": cred}
        assert perform("get_file", CALLS["get_file"], call_scope=scope).status_code == 400
    assert gh.token_requests()[before:] == []


def test_pat_mode_never_calls_the_token_endpoint(pat_mode, gh, perform):
    assert perform("get_file", CALLS["get_file"]).status_code == 200
    assert perform("push_file", CALLS["push_file"]).status_code == 200
    assert perform("list_repos", {}).status_code == 200
    assert gh.token_requests() == []
    assert all(r.auth == f"Bearer {PAT}" for r in gh.requests)


def test_pat_mode_reports_proxy_enforcement(pat_mode):
    status = pat_mode.get("/status").json()
    assert status["enforcement"] == "proxy" and status["mode"] == "pat"
    assert status["connected"] is True


def test_app_mode_reports_target_enforcement(app_mode):
    status = app_mode.get("/status").json()
    assert status["enforcement"] == "target" and status["mode"] == "app"
    assert status["connection"]["installed_permissions"]["contents"] == "write"
    assert status["connection"]["repositories_count"] == 3


def test_the_app_wins_over_a_pat_when_both_are_configured(client, gh, perform):
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM, "pat": PAT})
    install(client)
    assert client.get("/status").json()["enforcement"] == "target"
    assert perform("get_file", CALLS["get_file"]).status_code == 200
    assert all(r.auth != f"Bearer {PAT}" for r in gh.requests)


def test_not_installed_is_503_not_performed(client, gh, perform):
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    r = perform("create_issue", CALLS["create_issue"])
    assert r.status_code == 503
    assert gh.calls("POST", "/repos/") == []


def test_mint_is_usable_directly_and_records_what_it_is_for(adapter, gh, client):
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    install(client)
    conn: GitHubAppConnection = adapter.connection
    token = conn.mint({"permissions": {"issues": "read"},
                       "resources": {"repo": ["octo/b", "Octo/A"]}})
    assert token.mode == "app"
    assert token.repositories == ("a", "b")
    assert token.permissions == (("issues", "read"),)
