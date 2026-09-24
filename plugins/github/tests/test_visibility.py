"""Hidden == missing, on every action; the branch selector; resource_ref.

The plugin holds these lines on its own, whatever the broker checked: a
scope sent straight to it with a deny is refused before any token exists.
"""

import pytest

from .conftest import items, scope_for
from .test_minting import CALLS

NOT_FOUND = {"error": "not found"}


@pytest.fixture(params=["app", "pat"])
def mode(request, client, app_mode_factory):
    return app_mode_factory(request.param)


@pytest.fixture()
def app_mode_factory(client):
    from .conftest import APP_CONFIG, PAT, PRIVATE_KEY_PEM, configure, install

    def make(kind):
        if kind == "app":
            configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
            install(client)
        else:
            configure(client, {}, {"pat": PAT})
        return kind
    return make


def _as(action, repo):
    return {**CALLS[action], "repo": repo}


@pytest.mark.parametrize("action", sorted(CALLS))
def test_a_hidden_repo_is_the_same_404_as_a_missing_one(mode, gh, perform, action):
    before_tokens, before_calls = len(gh.token_requests()), len(gh.requests)
    hidden = perform(action, _as(action, "octo/secret"), deny=["octo/secret"])
    missing = perform(action, _as(action, "octo/nothing"), deny=["octo/secret"])
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json() == NOT_FOUND
    # Nothing about the hidden repository reached GitHub, not even a token.
    assert not any("secret" in r.path for r in gh.requests[before_calls:])
    assert all(b.get("repositories") != ["secret"] for b in gh.token_requests()[before_tokens:])


def test_a_deny_in_another_spelling_still_denies(mode, perform):
    r = perform("get_file", _as("get_file", "Octo/Secret"), deny=["OCTO/SECRET"])
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_a_repo_outside_allow_only_is_404(mode, gh, perform):
    before = len(gh.requests)
    r = perform("get_file", _as("get_file", "octo/b"), allow=["octo/a"])
    assert r.status_code == 404 and r.json() == NOT_FOUND
    r = perform("get_file", _as("get_file", "octo/a"), allow=[])      # [] = nothing
    assert r.status_code == 404
    assert not any(r.method == "GET" and r.path.startswith("/repos/")
                   for r in gh.requests[before:])


def test_a_repo_outside_the_credential_list_is_404(mode, perform):
    r = perform("get_file", _as("get_file", "octo/b"), repos=["octo/a"])
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_hidden_repos_are_absent_from_list_repos(mode, perform):
    listed = [i["repo"] for i in items(perform("list_repos", {}, deny=["octo/secret"]))]
    assert "octo/secret" not in listed and "octo/a" in listed
    only = [i["repo"] for i in items(perform("list_repos", {}, allow=["octo/b"]))]
    assert only == ["octo/b"]
    assert items(perform("list_repos", {}, allow=[])) == []


def test_every_repo_row_carries_a_resource_ref(mode, perform):
    for row in items(perform("list_repos", {})):
        assert row["resource_ref"] == {"kind": "repo", "id": row["repo"]}
    for action in ("list_issues", "list_prs"):
        for row in items(perform(action, {"repo": "Octo/A"})):
            assert row["resource_ref"] == {"kind": "repo", "id": "octo/a"}
    for action in ("get_issue", "get_file"):
        r = perform(action, _as(action, "OCTO/a"))
        assert r.json()["data"]["resource_ref"] == {"kind": "repo", "id": "octo/a"}


def test_write_results_carry_no_resource_ref(mode, perform):
    # A performed write must never be turned into a 404 by a post-filter.
    r = perform("create_issue", CALLS["create_issue"])
    assert r.status_code == 200 and "resource_ref" not in r.json()["data"]


def test_a_renamed_repository_is_not_followed(mode, gh, perform):
    gh.renamed["octo/old-name"] = "octo/secret"
    r = perform("get_file", _as("get_file", "octo/old-name"), deny=["octo/secret"])
    assert r.status_code == 404 and r.json() == NOT_FOUND
    assert not any("secret" in r.path for r in gh.requests)


# ---- branches ---------------------------------------------------------------------

BRANCH_CALLS = {
    "create_branch": ({"branch": "agent/new"}, "agent/new"),
    "push_file": ({"branch": "dev", "path": "n.txt", "content": "x", "message": "m"}, "dev"),
    "create_pr": ({"head": "dev", "base": "main", "title": "t"}, "dev"),
    "delete_branch": ({"branch": "dev"}, "dev"),
}


@pytest.mark.parametrize("action", sorted(BRANCH_CALLS))
def test_a_branch_outside_the_selector_is_refused_before_any_token(mode, gh, perform, action):
    params, _ = BRANCH_CALLS[action]
    before_tokens, before = len(gh.token_requests()), len(gh.requests)
    r = perform(action, {"repo": "octo/a", **params}, branch_allow=["agent/only"])
    assert r.status_code == 403
    assert r.json() == {"error": "branch not allowed by your grant"}
    assert gh.token_requests()[before_tokens:] == []
    assert gh.requests[before:] == []


@pytest.mark.parametrize("action", sorted(BRANCH_CALLS))
def test_a_branch_inside_the_selector_passes(mode, perform, action):
    params, branch = BRANCH_CALLS[action]
    r = perform(action, {"repo": "octo/a", **params}, branch_allow=[branch, "other"])
    assert r.status_code == 200, r.text


def test_a_glob_in_the_selector_matches_only_itself(mode, perform):
    r = perform("create_branch", {"repo": "octo/a", "branch": "feat/x"},
                branch_allow=["feat/*"])
    assert r.status_code == 403


def test_a_denied_branch_is_hidden(mode, perform):
    r = perform("delete_branch", {"repo": "octo/a", "branch": "dev"}, branch_deny=["dev"])
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_merge_checks_the_base_branch_before_merging(mode, gh, perform):
    r = perform("merge_pr", {"repo": "octo/a", "number": 2}, branch_allow=["dev"])
    assert r.status_code == 403                      # PR 2 merges dev INTO main
    assert gh.calls("PUT", "/repos/") == []
    r = perform("merge_pr", {"repo": "octo/a", "number": 2}, branch_allow=["main"])
    assert r.status_code == 200 and r.json()["data"]["status"] == "merged"


def test_branch_rules_do_not_apply_to_reads(mode, perform):
    r = perform("get_file", {"repo": "octo/a", "path": "README.md", "ref": "dev"},
                branch_allow=["main"])
    assert r.status_code == 200


# ---- malformed scopes -------------------------------------------------------------

@pytest.mark.parametrize("vis", [
    {"repo": {"deny": "octo/a"}}, {"repo": {"allow_only": "octo/a"}}, {"repo": "x"},
    {"branch": {"allow_only": "dev"}}, [],
])
def test_a_malformed_scope_is_400_never_unrestricted(mode, gh, perform, vis):
    before = len(gh.requests)
    scope = {**scope_for("delete_branch"), "visibility": vis}
    r = perform("delete_branch", {"repo": "octo/a", "branch": "dev"}, call_scope=scope)
    assert r.status_code == 400
    assert gh.requests[before:] == []
