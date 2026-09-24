"""plugin-google's log lines: each access token says which scope set it
covers and whether it was refreshed or cached, never the token; consent and
connect say what was granted; Google's refusals log the status class and
reason code, never the URL (its query holds the Gmail search) or the body."""

import logging

from . import fake_google as fg


def google_lines(caplog, logger="aab_plugin_google.connection") -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger]


def test_access_tokens_log_their_scope_set_and_source(perform, google, caplog):
    caplog.set_level(logging.DEBUG)
    assert perform("gmail", "search_threads", {"query": "from:alice secret-term"}
                   ).status_code == 200
    assert perform("gmail", "search_threads").status_code == 200
    tokens = [line for line in google_lines(caplog) if line.startswith("google access token")]
    assert tokens[0].startswith("google access token source=refreshed scopes=gmail.readonly")
    assert tokens[1] == "google access token source=cache scopes=gmail.readonly"
    for token in google.tokens:
        assert token not in caplog.text
    assert "secret-term" not in caplog.text and fg.REFRESH_TOKEN not in caplog.text


def test_connect_logs_what_was_granted_never_the_code(client, google, caplog):
    from .conftest import connect
    caplog.set_level(logging.DEBUG)
    connect(client, google)
    lines = google_lines(caplog)
    assert any(line.startswith("google consent started plugins=gcal,gdrive,gmail")
               for line in lines)
    assert any(line.startswith("google connected scopes_granted=") for line in lines)
    for value in (fg.AUTH_CODE, fg.CLIENT_SECRET, fg.REFRESH_TOKEN):
        assert value not in caplog.text


def test_a_google_refusal_logs_status_class_and_reason(perform, google, caplog):
    caplog.set_level(logging.INFO)
    google.fail_next["GET gmail.googleapis.com/gmail/v1/users/me/threads"] = 500
    assert perform("gmail", "search_threads", {"query": "private query"}).status_code == 503
    [line] = google_lines(caplog, "aab_plugin_google.client")
    assert line == "google api refused method=GET google_status=500 status_class=5xx " \
                   "reason=- maps_to=503"
    assert "private query" not in caplog.text
