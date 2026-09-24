"""Reading the CallScope: faithful, and closed on anything odd."""

import pytest

from aab_plugin_runtime import AdapterError
from aab_plugin_github.scope import (branch_allowed, credential, is_visible, missing,
                                     visibility, within)


def test_visibility_absent_means_unrestricted():
    assert visibility({}, "repo") == ([], None)
    assert visibility({"visibility": {"branch": {"deny": ["x"]}}}, "repo") == ([], None)


def test_empty_allow_only_stays_empty():
    # [] means nothing is allowed; coercing it to None would fail open.
    assert visibility({"visibility": {"repo": {"deny": [], "allow_only": []}}}, "repo") == \
        ([], [])
    assert not is_visible("octo/a", [], [])


@pytest.mark.parametrize("bad", [
    None, "scope", {"visibility": []}, {"visibility": {"repo": "a"}},
    {"visibility": {"repo": {"deny": "octo/a"}}},
    {"visibility": {"repo": {"allow_only": "octo/a"}}},     # not a list of characters
    {"visibility": {"repo": {"allow_only": [1]}}},
])
def test_malformed_visibility_is_400(bad):
    with pytest.raises(AdapterError) as e:
        visibility(bad, "repo")
    assert e.value.status == 400


def test_deny_wins():
    assert not is_visible("octo/a", ["octo/a"], ["octo/a"])
    assert is_visible("octo/a", ["octo/b"], None)


def test_credential_reads_permissions_and_normalizes_repos():
    perms, repos = credential({"credential": {
        "permissions": {"contents": "read"},
        "resources": {"repo": ["Octo/A", "octo/a", "octo/b"]}}})
    assert perms == {"contents": "read"}
    assert repos == ["octo/a", "octo/b"]
    assert credential({"credential": {"permissions": {"issues": "write"}}})[1] is None


@pytest.mark.parametrize("cred", [
    None, "x", {},                                   # no permissions at all
    {"permissions": {}},                             # empty = GitHub's "everything"
    {"permissions": {"contents": "admin"}},          # never requested
    {"permissions": {"contents": "READ"}},
    {"permissions": {"Contents": "read"}},
    {"permissions": {"contents": "read"}, "resources": "x"},
    {"permissions": {"contents": "read"}, "resources": {"repo": "octo/a"}},
    {"permissions": {"contents": "read"}, "resources": {"repo": ["not a repo"]}},
    {"permissions": {"contents": "read"}, "resources": {"repo": [f"o/r{i}" for i in range(501)]}},
])
def test_malformed_credential_is_400(cred):
    with pytest.raises(AdapterError) as e:
        credential({"credential": cred})
    assert e.value.status == 400


def test_within_and_missing():
    assert within({"contents": "read"}, {"contents": "write"})
    assert within({"contents": "write"}, {"contents": "admin"})
    assert not within({"contents": "write"}, {"contents": "read"})
    assert not within({"issues": "read"}, {"contents": "write"})
    assert not within({"contents": "read"}, {"contents": "bogus"})
    assert missing({"contents": "write", "issues": "read"}, {"contents": "read"}) == [
        "contents:write", "issues:read"]


def test_branch_selector_is_exact_string_membership():
    branch_allowed("main", [], None)
    branch_allowed("feat/*", [], ["feat/*"])        # only the literal string
    with pytest.raises(AdapterError) as e:
        branch_allowed("feat/x", [], ["feat/*"])     # no glob implication
    assert e.value.status == 403
    with pytest.raises(AdapterError) as e:
        branch_allowed("main", ["main"], ["main"])   # a denied branch is hidden
    assert e.value.status == 404
    with pytest.raises(AdapterError):
        branch_allowed("main", [], [])               # empty selector: nothing
