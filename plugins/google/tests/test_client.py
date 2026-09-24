"""The shared API client's mapping onto the broker's 503/502 contract."""

import httpx
import pytest

from . import fake_google as fg
from .conftest import scope_for


def search(perform):
    return perform("gmail", "search_threads", {})


def send(perform):
    return perform("gmail", "send", {"to": ["alice@example.com"], "subject": "s", "body": "b"})


def test_unreachable_is_503_for_reads_and_writes(perform, google):
    google.fail_next["GET gmail.googleapis.com/gmail/v1/users/me/labels"] = httpx.ConnectError
    google.fail_next["GET gmail.googleapis.com/gmail/v1/users/me/threads"] = httpx.ConnectError
    assert search(perform).status_code == 503
    google.fail_next["POST gmail.googleapis.com/gmail/v1/users/me/messages/send"] = \
        httpx.ConnectTimeout
    r = send(perform)
    assert r.status_code == 503 and google.gmail_writes == []


def test_lost_read_is_503_but_lost_write_is_502(perform, google):
    google.fail_next["GET gmail.googleapis.com/gmail/v1/users/me/threads"] = httpx.ReadTimeout
    assert search(perform).status_code == 503         # a read changed nothing: retry is safe
    google.fail_next["POST gmail.googleapis.com/gmail/v1/users/me/messages/send"] = \
        httpx.ReadTimeout
    r = send(perform)
    assert r.status_code == 502 and "unknown outcome" in r.json()["error"]


@pytest.mark.parametrize("status,read_expect,write_expect", [
    (500, 503, 502), (503, 503, 502), (302, 503, 502)])
def test_server_errors_split_by_side_effect(perform, google, status, read_expect,
                                            write_expect):
    google.fail_next["GET gmail.googleapis.com/gmail/v1/users/me/threads"] = status
    assert search(perform).status_code == read_expect
    google.fail_next["POST gmail.googleapis.com/gmail/v1/users/me/messages/send"] = status
    assert send(perform).status_code == write_expect


def test_404_is_one_not_found(perform):
    r = perform("gmail", "get_thread", {"thread_id": "fff9"})
    assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_rate_limits_are_429(perform, google):
    google.fail_next["GET gmail.googleapis.com/gmail/v1/users/me/threads"] = 429
    assert search(perform).status_code == 429


def test_403_rate_limit_reason_is_429(perform, google, monkeypatch):
    real = google._handle

    def handle(request):
        if request.url.path.endswith("/threads"):
            reason = [{"reason": "userRateLimitExceeded"}]
            return httpx.Response(403, json={"error": {"code": 403, "message": "slow down",
                                                       "errors": reason}})
        return real(request)
    monkeypatch.setattr(google, "_handle", handle)
    assert search(perform).status_code == 429


def test_insufficient_scope_is_a_403_passthrough(connected, google):
    # A requirement too narrow for the endpoint: Google itself refuses.
    scope = scope_for("gmail", "send")
    scope["credential"] = {"permissions": {"gmail.send": "write"}}
    r = connected.post("/perform", headers={"X-Plugin-Id": "gmail"}, json={
        "action": "send", "params": {"to": ["a@example.com"], "thread_id": "aaa1"},
        "scope": scope})
    assert r.status_code == 403 and "insufficient" in r.json()["error"]
    assert google.gmail_writes == []


def test_401_drops_the_cached_token_and_is_retryable(perform, google):
    assert search(perform).status_code == 200
    first = len(google.refreshes)
    google.tokens.clear()                            # Google forgets every token
    google.fail_next["GET gmail.googleapis.com/gmail/v1/users/me/threads"] = 401
    assert search(perform).status_code == 503
    assert search(perform).status_code == 200        # a fresh token was minted
    assert len(google.refreshes) == first + 1


def test_redirects_are_never_followed(perform, google, monkeypatch):
    real = google._handle
    seen = []

    def handle(request):
        seen.append(str(request.url))
        if request.url.path.endswith("/labels"):
            return httpx.Response(302, headers={"location": "https://evil.test/steal"})
        return real(request)
    monkeypatch.setattr(google, "_handle", handle)
    assert perform("gmail", "list_labels").status_code == 503
    assert not any("evil.test" in u for u in seen)


def test_bearer_token_is_only_in_the_authorization_header(perform, google, monkeypatch):
    real = google._handle
    seen = []

    def handle(request):
        seen.append((str(request.url), request.content))
        return real(request)
    monkeypatch.setattr(google, "_handle", handle)
    search(perform)
    for url, body in seen:
        assert "ya29." not in url and b"ya29." not in (body or b"")


def test_client_id_is_the_only_oauth_value_in_a_url(connected, google):
    for method, url, query in google.requests:
        for secret in (fg.CLIENT_SECRET, fg.REFRESH_TOKEN, fg.AUTH_CODE):
            assert secret not in url and secret not in str(query)
