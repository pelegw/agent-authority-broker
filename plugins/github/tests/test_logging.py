"""plugin-github's log lines: each installation token says what it was minted
for (permission and repository names) and whether it came from the cache,
never the token; GitHub refusals log the status class and what they map to,
never the path (it can name a file or branch from the params) or GitHub's
message."""

import logging

from .test_minting import CALLS


def github_lines(caplog, logger="aab_plugin_github") -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger]


def test_minting_logs_the_scope_and_the_source_never_the_token(app_mode, gh, perform, caplog):
    caplog.set_level(logging.DEBUG)
    assert perform("get_file", CALLS["get_file"]).status_code == 200
    assert perform("get_file", CALLS["get_file"]).status_code == 200
    minted = [line for line in github_lines(caplog) if line.startswith("github installation")]
    assert minted == [
        "github installation token source=minted permissions=contents:read repos=a",
        "github installation token source=cache permissions=contents:read repos=a"]
    for token in gh.tokens:
        assert token not in caplog.text
    assert "README.md" not in "\n".join(github_lines(caplog))


def test_pat_mode_says_so(pat_mode, perform, caplog):
    caplog.set_level(logging.INFO)
    assert perform("get_file", CALLS["get_file"]).status_code == 200
    assert "github credential mode=pat enforcement=proxy" in github_lines(caplog)


def test_a_github_refusal_logs_the_status_class_not_the_path(app_mode, gh, perform, caplog):
    caplog.set_level(logging.INFO)
    gh.fail("GET", r"/repos/octo/a/contents/", 500)
    r = perform("get_file", {"repo": "octo/a", "path": "secret-plans.md"})
    assert r.status_code == 502
    [line] = github_lines(caplog, "aab_plugin_github.api")
    assert line == "github api refused method=GET github_status=500 status_class=5xx " \
                   "maps_to=502 retry_after=-"
    assert "secret-plans" not in caplog.text


def test_an_unreachable_github_is_a_warning(app_mode, gh, perform, caplog):
    caplog.set_level(logging.INFO)
    gh.fail("GET", r"/repos/octo/a/contents/", "connect")
    perform("get_file", CALLS["get_file"])
    [record] = [r for r in caplog.records if r.name == "aab_plugin_github.api"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "github api unreachable method=GET error=ConnectError " \
                                  "maps_to=503"
