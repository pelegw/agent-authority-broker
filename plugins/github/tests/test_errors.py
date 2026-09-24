"""The 503/502 contract against GitHub, rate limits, and refused credentials.

503 = not performed (safe to retry), 502 = the write may have happened
(never retried automatically). Lookups that precede a write and token
minting are "not performed" whatever goes wrong; the write itself is 502
once it has been sent.
"""

import httpx
import pytest

from .test_minting import CALLS


def test_connect_failure_on_a_write_is_503(app_mode, gh, perform):
    gh.fail("POST", r"/repos/octo/a/issues$", "connect")
    r = perform("create_issue", CALLS["create_issue"])
    assert r.status_code == 503


@pytest.mark.parametrize("what", ["read_timeout", 500, 502, 504])
def test_a_write_that_fails_after_sending_is_502(app_mode, gh, perform, what):
    gh.fail("POST", r"/repos/octo/a/issues$", what)
    r = perform("create_issue", CALLS["create_issue"])
    assert r.status_code == 502


def test_a_garbled_success_is_502(app_mode, gh, perform):
    gh.fail("POST", r"/repos/octo/a/issues$", httpx.Response(201, text="<html>"))
    assert perform("create_issue", CALLS["create_issue"]).status_code == 502


@pytest.mark.parametrize("what", ["read_timeout", 500])
def test_a_failed_lookup_before_the_write_is_503(app_mode, gh, perform, what):
    # push_file reads the current sha first; if that fails, nothing was written.
    gh.fail("GET", r"/repos/octo/a/contents/new\.txt$", what)
    r = perform("push_file", CALLS["push_file"])
    assert r.status_code == 503
    assert gh.calls("PUT", "/repos/") == []


@pytest.mark.parametrize("what", ["read_timeout", 500, "connect"])
def test_a_failed_merge_pre_read_is_503(app_mode, gh, perform, what):
    gh.fail("GET", r"/repos/octo/a/pulls/2$", what)
    assert perform("merge_pr", CALLS["merge_pr"]).status_code == 503
    assert gh.calls("PUT", "/repos/") == []


@pytest.mark.parametrize("what", ["read_timeout", 500, "connect"])
def test_a_failed_token_mint_is_503_never_502(app_mode, gh, perform, what):
    gh.fail("POST", r"/access_tokens$", what)
    r = perform("create_issue", CALLS["create_issue"])
    assert r.status_code == 503
    assert gh.calls("POST", "/repos/") == []


def test_a_read_that_times_out_is_502(app_mode, gh, perform):
    gh.fail("GET", r"/repos/octo/a/issues$", "read_timeout")
    assert perform("list_issues", {"repo": "octo/a"}).status_code == 502


def test_rate_limit_is_429_with_retry_after(app_mode, gh, perform):
    gh.rate_limit("GET", r"/repos/octo/a/issues$")
    r = perform("list_issues", {"repo": "octo/a"})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "120"
    assert "retry after 120 s" in r.json()["error"]


def test_secondary_rate_limit_is_429_with_githubs_retry_after(app_mode, gh, perform):
    gh.rate_limit("POST", r"/repos/octo/a/issues$", secondary=True)
    r = perform("create_issue", CALLS["create_issue"])
    assert r.status_code == 429 and r.headers["Retry-After"] == "30"


def test_rate_limit_on_minting_is_429(app_mode, gh, perform):
    gh.rate_limit("POST", r"/access_tokens$")
    r = perform("get_file", CALLS["get_file"])
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


def test_other_403s_are_not_rate_limits(app_mode, gh, perform):
    gh.fail("POST", r"/repos/octo/a/issues$",
            httpx.Response(403, json={"message": "Resource not accessible by integration"}))
    r = perform("create_issue", CALLS["create_issue"])
    assert r.status_code == 403 and "Retry-After" not in r.headers


def test_a_refused_token_is_503_and_evicted_from_the_cache(app_mode, gh, perform):
    assert perform("get_file", CALLS["get_file"]).status_code == 200
    before = len(gh.token_requests())
    gh.tokens.clear()                           # GitHub revoked every token
    r = perform("get_file", CALLS["get_file"])
    assert r.status_code == 503                 # refused before anything ran
    assert perform("get_file", CALLS["get_file"]).status_code == 200
    assert len(gh.token_requests()) == before + 1


def test_unknown_action_and_bad_params(app_mode, perform, gh):
    assert perform("nope", {}, call_scope=scope_ok()).status_code == 404
    assert perform("get_issue", {"repo": "octo/a", "number": "1"}).status_code == 400
    assert perform("get_issue", {"repo": "octo/a", "number": True}).status_code == 400
    assert perform("get_issue", {"repo": "octo/a"}).status_code == 400
    assert perform("get_file", {"repo": "octo/a", "path": "../x"}).status_code == 400
    assert perform("get_file", {"path": "README.md"}).status_code == 400
    # A lone surrogate (a valid JSON escape, but not encodable text) is
    # refused before GitHub, not discovered mid-request as "unknown outcome".
    raw = ('{"action": "create_issue", "params": {"repo": "octo/a", "title": "\\ud800"},'
           ' "scope": {"credential": {"permissions": {"issues": "write"}}}}')
    r = app_mode.post("/perform", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert gh.calls("POST", "/repos/") == []


def scope_ok():
    from .conftest import scope_for
    return scope_for("get_file")


def test_github_is_never_asked_to_follow_a_redirect(app_mode, gh, perform):
    gh.fail("GET", r"/repos/octo/a/issues$",
            httpx.Response(302, headers={"location": "https://evil.example/steal"}))
    r = perform("list_issues", {"repo": "octo/a"})
    assert r.status_code == 404
    assert not any("evil" in str(r.path) for r in gh.requests)
