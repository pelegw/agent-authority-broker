"""The `aab` CLI against the real app, via a TestClient standing in for httpx."""

import json

import pytest
from fastapi.testclient import TestClient

from cli import aab

from .conftest import OWNER_PASSWORD, SETUP_TOKEN


@pytest.fixture()
def cli_app(env, monkeypatch, logs_to_stderr):
    """Route the CLI's httpx.Client to the in-process app; records headers sent."""
    from broker.main import app
    seen = []

    def fake_client(base_url, headers, timeout):
        seen.append(dict(headers))
        return TestClient(app, base_url=base_url, headers=headers)

    monkeypatch.setattr(aab.httpx, "Client", fake_client)
    monkeypatch.delenv("AAB_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("SETUP_TOKEN", raising=False)
    return seen


def test_tokens_list(cli_app, admin_token, capsys):
    assert aab.main(["--token", admin_token, "tokens", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [t["name"] for t in listed] == ["test"]
    assert cli_app[0]["Authorization"] == f"Bearer {admin_token}"


def test_token_from_env_and_create_then_revoke(cli_app, admin_token, capsys, monkeypatch):
    monkeypatch.setenv("AAB_ADMIN_TOKEN", admin_token)
    assert aab.main(["tokens", "create", "--name", "deploy"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["token"].startswith("aab_admin_")
    assert aab.main(["tokens", "revoke", created["id"]]) == 0


def test_bad_token_exits_nonzero_with_the_error(cli_app, owner, capsys):
    assert aab.main(["--token", "aab_admin_nope", "sessions", "list"]) == 1
    assert "401" in capsys.readouterr().err


def test_missing_token_is_a_usage_error(cli_app, capsys):
    assert aab.main(["tokens", "list"]) == 2
    assert "AAB_ADMIN_TOKEN" in capsys.readouterr().err


def test_setup_reads_the_password_with_getpass(cli_app, monkeypatch, capsys):
    monkeypatch.setenv("SETUP_TOKEN", SETUP_TOKEN)
    from broker.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr(aab.getpass, "getpass", lambda _prompt: OWNER_PASSWORD)
    assert aab.main(["setup", "--username", "owner", "--setup-token", SETUP_TOKEN]) == 0
    assert json.loads(capsys.readouterr().out)["setup_completed"] is True
    assert "Authorization" not in cli_app[0]   # setup never sends an admin token
