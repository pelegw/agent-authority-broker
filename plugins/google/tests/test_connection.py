"""The google_oauth connection: consent URL, state nonce, code exchange, the
per-scope-set token cache, scope checks, disconnect, and secret hygiene."""

import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from aab_plugin_runtime import AdapterError

from . import fake_google as fg
from .conftest import REDIRECT, configure, connect, requirements

GMAIL = {"X-Plugin-Id": "gmail"}


def start(client, enabled=("gmail",), redirect=REDIRECT, plugin="gmail"):
    return client.post("/connect/start", headers={"X-Plugin-Id": plugin},
                       json={"enabled_plugins": list(enabled), "redirect_uri": redirect})


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def finish(client, state, code=fg.AUTH_CODE):
    return client.post("/connect/finish", headers=GMAIL, json={"code": code, "state": state})


# ---- consent URL -----------------------------------------------------------------------

def test_start_needs_the_oauth_client_configured(client):
    r = start(client)
    assert r.status_code == 400 and "client id and secret" in r.json()["error"]


def test_consent_url_asks_for_the_enabled_plugins_scopes_only(client):
    configure(client)
    q = query(start(client, enabled=["gmail"]).json()["url"])
    assert set(q["scope"].split()) == {fg.GM_RO, fg.GM_META, fg.GM_COMPOSE, fg.GM_SEND,
                                       fg.GM_MODIFY, fg.GM_FULL}
    q2 = query(start(client, enabled=["gcal", "gdrive"]).json()["url"])
    assert set(q2["scope"].split()) == {fg.CAL_RO, fg.CAL_EVENTS, fg.DRIVE_RO, fg.DRIVE}


def test_consent_url_parameters(client):
    configure(client)
    r = start(client)
    assert r.status_code == 200 and r.json()["kind"] == "oauth"
    url = r.json()["url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    q = query(url)
    assert q["client_id"] == fg.CLIENT_ID and q["redirect_uri"] == REDIRECT
    assert (q["response_type"], q["access_type"], q["prompt"], q["include_granted_scopes"]) == (
        "code", "offline", "consent", "true")
    assert len(q["state"]) >= 43                   # 32 random bytes, base64url
    assert fg.CLIENT_SECRET not in r.text


def test_each_start_has_a_fresh_state(client):
    configure(client)
    assert query(start(client).json()["url"])["state"] != query(start(client).json()["url"])[
        "state"]


@pytest.mark.parametrize("enabled,status", [([], 400), (["nosuch"], 400)])
def test_start_refuses_bad_plugin_sets(client, enabled, status):
    configure(client)
    assert start(client, enabled=enabled).status_code == status


@pytest.mark.parametrize("uri,ok", [
    ("https://aab.example.com/oauth/callback/google", True),
    ("http://localhost:8080/oauth/callback/google", True),
    ("http://127.0.0.1/oauth/callback/google", True),
    ("http://aab.example.com/oauth/callback/google", False),   # http off loopback
    ("https://aab.example.com/elsewhere", False),
    ("https://aab.example.com/oauth/callback/google?next=x", False),
    ("https://user@aab.example.com/oauth/callback/google", False),
    ("javascript:alert(1)", False),
    (None, False),
])
def test_redirect_uri_is_checked(client, uri, ok):
    configure(client)
    r = client.post("/connect/start", headers=GMAIL,
                    json={"enabled_plugins": ["gmail"], "redirect_uri": uri})
    assert (r.status_code == 200) is ok, r.text


# ---- finish: state nonce and code exchange ----------------------------------------------

def test_redirect_uri_round_trip_and_exchange(client, google, connection):
    configure(client)
    google.expected_redirect = "https://aab.example.com/oauth/callback/google"
    state = query(start(client, redirect=google.expected_redirect).json()["url"])["state"]
    r = finish(client, state)
    assert r.status_code == 200, r.text
    exchange = google.token_forms[-1]
    # The code is exchanged with the SAME redirect URI and the client secret.
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["redirect_uri"] == google.expected_redirect
    assert exchange["client_secret"] == fg.CLIENT_SECRET
    assert r.json()["connected"] is True and "gmail.readonly" in r.json()["granted_scopes"]
    assert connection.status()["connected"] is True


def test_state_is_single_use(client, google):
    configure(client)
    state = query(start(client).json()["url"])["state"]
    assert finish(client, state).status_code == 200
    again = finish(client, state)
    assert again.status_code == 400 and "no pending" in again.json()["error"]


def test_a_wrong_state_burns_the_pending_one(client):
    configure(client)
    state = query(start(client).json()["url"])["state"]
    assert finish(client, "guessed-state").status_code == 400
    assert finish(client, state).status_code == 400     # one try only


def test_state_expires_after_ten_minutes(client, clock):
    configure(client)
    state = query(start(client).json()["url"])["state"]
    clock.advance(601)
    r = finish(client, state)
    assert r.status_code == 400 and "no pending" in r.json()["error"]


def test_state_within_ttl_is_accepted(client, clock):
    configure(client)
    state = query(start(client).json()["url"])["state"]
    clock.advance(599)
    assert finish(client, state).status_code == 200


def test_bad_code_is_400_and_stores_nothing(client, connection):
    configure(client)
    state = query(start(client).json()["url"])["state"]
    r = finish(client, state, code="4/wrong")
    assert r.status_code == 400 and "invalid_grant" in r.json()["error"]
    assert connection.status()["connected"] is False


def test_missing_refresh_token_is_refused(client, google, connection):
    configure(client)
    google.omit_refresh_token = True
    state = query(start(client).json()["url"])["state"]
    assert finish(client, state).status_code == 400
    assert connection.status()["connected"] is False


# ---- minting: the per-scope-set cache -------------------------------------------------

def test_refresh_asks_for_exactly_the_calls_scopes(connected, google, connection):
    connection.mint(requirements("gmail", "search_threads"))
    connection.mint(requirements("gmail", "send"))
    assert google.refreshes == [(fg.GM_RO,), tuple(sorted([fg.GM_META, fg.GM_SEND]))]


def test_cache_hit_on_the_same_scope_set(connected, google, connection):
    a = connection.mint(requirements("gmail", "search_threads"))
    b = connection.mint(requirements("gmail", "get_thread"))      # same scope set
    assert a is b and len(google.refreshes) == 1


def test_cache_miss_near_expiry(connected, google, connection, clock):
    connection.mint(requirements("gdrive", "list_files"))
    clock.advance(3599 - 299)              # < 5 minutes left
    connection.mint(requirements("gdrive", "list_files"))
    assert len(google.refreshes) == 2


def test_cache_holds_until_five_minutes_before_expiry(connected, google, connection, clock):
    connection.mint(requirements("gdrive", "list_files"))
    clock.advance(3599 - 301)
    connection.mint(requirements("gdrive", "list_files"))
    assert len(google.refreshes) == 1


def test_scopes_not_granted_is_403_without_asking_google(client, google, connection):
    google.granted = [fg.GM_RO]
    connect(client, google, enabled=["gmail"])
    with pytest.raises(AdapterError) as exc:
        connection.mint(requirements("gmail", "send"))
    assert exc.value.status == 403 and exc.value.message == "scopes not granted; reconnect"
    assert google.refreshes == []


def test_a_wider_token_than_asked_is_refused(connected, google, connection):
    google.widen = True
    with pytest.raises(AdapterError) as exc:
        connection.mint(requirements("gmail", "search_threads"))
    assert exc.value.status == 503
    status = connection.plugin_status("gmail")
    assert status["healthy"] is False and "more scopes than requested" in status["health"]
    google.widen = False                  # nothing was cached: the next call refreshes again
    connection.mint(requirements("gmail", "search_threads"))
    assert len(google.refreshes) == 2


def test_malformed_requirements_fail_closed(connected, connection):
    for bad in ({}, {"permissions": {}}, {"permissions": "gmail.readonly"},
                {"permissions": {"Gmail Readonly!": "read"}}):
        with pytest.raises(AdapterError) as exc:
            connection.mint(bad)
        assert exc.value.status == 400


def test_revoked_refresh_token_asks_for_reconnect(connected, google, connection):
    google.revoked.append(fg.REFRESH_TOKEN)
    with pytest.raises(AdapterError) as exc:
        connection.mint(requirements("gcal", "list_events"))
    assert exc.value.status == 503
    assert connection.plugin_status("gcal")["health"].startswith("reconnect required")


def test_token_endpoint_unreachable_is_503(connected, google, connection):
    google.fail_next["oauth2.googleapis.com/token"] = httpx.ConnectError
    with pytest.raises(AdapterError) as exc:
        connection.mint(requirements("gcal", "list_events"))
    assert exc.value.status == 503


def test_not_connected_mint_is_503(client, connection):
    configure(client)
    with pytest.raises(AdapterError) as exc:
        connection.mint(requirements("gmail", "search_threads"))
    assert exc.value.status == 503


# ---- status and missing scopes ------------------------------------------------------------

def test_status_per_plugin(connected):
    body = connected.get("/status", headers={"X-Plugin-Id": "gcal"}).json()
    assert body["connected"] is True and body["healthy"] is True and body["health"] == "ok"
    assert body["enforcement"] == "mixed" and body["missing_scopes"] == []
    assert body["connection"]["kind"] == "google_oauth"
    assert body["connection"]["client_configured"] is True


def test_a_plugin_enabled_after_connecting_reports_missing_scopes(client, google):
    google.granted = [s for s in fg.ALL_SCOPES if "gmail" in s or "mail.google" in s]
    connect(client, google, enabled=["gmail"])
    gmail = client.get("/status", headers=GMAIL).json()
    gcal = client.get("/status", headers={"X-Plugin-Id": "gcal"}).json()
    assert gmail["healthy"] is True
    assert gcal["connected"] is True and gcal["healthy"] is False
    assert gcal["health"] == "reconnect needed: scopes missing"
    assert gcal["missing_scopes"] == ["calendar.events", "calendar.readonly"]


def test_status_before_configuration(client):
    body = client.get("/status", headers=GMAIL).json()
    assert body["connected"] is False and body["health"].startswith("not configured")


# ---- disconnect ------------------------------------------------------------------------------

def test_disconnect_revokes_and_wipes(connected, google, connection):
    connection.mint(requirements("gmail", "search_threads"))
    r = connected.post("/disconnect", headers=GMAIL)
    assert r.json() == {"ok": True, "revoked": True}
    assert google.revoked == [fg.REFRESH_TOKEN]
    assert connection.status()["connected"] is False
    with pytest.raises(AdapterError) as exc:          # the cached token is gone too
        connection.mint(requirements("gmail", "search_threads"))
    assert exc.value.status == 503
    # The OAuth client stays configured: reconnecting needs no re-entry.
    assert connection.status()["client_configured"] is True


def test_disconnect_wipes_even_when_revocation_fails(connected, google, connection):
    google.fail_next["oauth2.googleapis.com/revoke"] = httpx.ConnectError
    assert connected.post("/disconnect", headers=GMAIL).json()["revoked"] is False
    assert connection.status()["connected"] is False


def test_a_new_client_id_drops_the_old_refresh_token(connected, connection):
    r = connected.post("/configure", headers=GMAIL, json={"config": {"client_id": "other-id"}})
    assert r.status_code == 200
    assert connection.status()["connected"] is False


def test_the_same_client_id_keeps_the_connection(connected, connection):
    connected.post("/configure", headers={"X-Plugin-Id": "gdrive"},
                   json={"config": {"client_id": fg.CLIENT_ID}})
    assert connection.status()["connected"] is True


# ---- secrets never leak --------------------------------------------------------------------

SECRETS = (fg.CLIENT_SECRET, fg.REFRESH_TOKEN, fg.AUTH_CODE, "DO-NOT-LEAK")


def test_secrets_never_in_logs_responses_reprs_or_disk(client, google, connection, app,
                                                       tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    connect(client, google)
    token = connection.mint(requirements("gmail", "search_threads"))
    responses = [client.get("/status", headers={"X-Plugin-Id": p}).text
                 for p in ("gmail", "gcal", "gdrive")]
    responses.append(client.get("/manifests").text)
    responses.append(client.post("/perform", headers=GMAIL, json={
        "action": "list_labels", "params": {},
        "scope": {"credential": requirements("gmail", "list_labels")}}).text)
    reprs = [repr(connection), repr(token), repr(connection._slot)]
    for text in [caplog.text, *responses, *reprs]:
        for secret in SECRETS:
            assert secret not in text
    assert token.value.startswith("ya29.")          # it is a real token, just never shown
    for f in (tmp_path / "secrets").iterdir():
        data = f.read_bytes()
        assert fg.CLIENT_SECRET.encode() not in data and fg.REFRESH_TOKEN.encode() not in data
