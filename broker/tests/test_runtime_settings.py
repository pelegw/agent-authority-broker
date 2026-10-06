"""runtime_settings: Settings defaults overlaid by console edits, typed and
bounded; env-only keys accounted for; MCP hosts can only be added to."""

import json
import time
import types

import pytest

from broker import auth, db, runtime_settings as rs
from broker.actions import queue
from broker.config import Settings, get_settings
from broker.errors import PolicyError
from broker.identity import sessions

CTX = types.SimpleNamespace(username="owner", principal_id="p1", via="token")


def _stored(name, value):
    db.set_config(rs.PREFIX + name, json.dumps(value))


def test_defaults_are_the_settings_values(env):
    eff = rs.runtime_settings()
    s = get_settings()
    for name in rs.SPECS:
        if name != "mcp_allowed_hosts_extra":
            assert getattr(eff, name) == getattr(s, name), name
    assert eff.mcp_allowed_hosts_extra == ()


def test_env_values_are_the_defaults_a_console_edit_overrides(env, monkeypatch):
    monkeypatch.setenv("MAX_DELEGATION_DEPTH", "2")
    get_settings.cache_clear()
    assert rs.runtime_settings().max_delegation_depth == 2
    rs.update(CTX, {"max_delegation_depth": 5})
    assert rs.runtime_settings().max_delegation_depth == 5
    rs.update(CTX, {"max_delegation_depth": None})          # reset -> env value again
    assert rs.runtime_settings().max_delegation_depth == 2
    assert db.get_config(rs.PREFIX + "max_delegation_depth") is None


@pytest.mark.parametrize("name,bad", [
    ("max_delegation_depth", 11), ("max_delegation_depth", -1),
    ("session_idle_seconds", 10), ("session_absolute_seconds", 10 ** 9),
    ("scheduler_tick_seconds", 0), ("draft_ttl_hours", 0), ("grant_max_hours", 10 ** 6),
    ("long_poll_interval_seconds", 0.0), ("plugin_timeout_seconds", 0.5),
    ("schedule_max_horizon_days", 0),
])
def test_out_of_range_values_are_refused(env, name, bad):
    with pytest.raises(PolicyError) as e:
        rs.update(CTX, {name: bad})
    assert e.value.status == 400 and e.value.code == "invalid_setting"
    assert db.get_config(rs.PREFIX + name) is None


@pytest.mark.parametrize("bad", [True, "5", 3.5, {"n": 1}, [3]])
def test_ints_must_be_real_integers(env, bad):
    with pytest.raises(PolicyError):
        rs.update(CTX, {"max_delegation_depth": bad})


def test_floats_accept_ints_but_not_bools_or_nan(env):
    rs.update(CTX, {"plugin_timeout_seconds": 12})
    assert rs.runtime_settings().plugin_timeout_seconds == 12.0
    for bad in (True, float("nan"), float("inf"), "12"):
        with pytest.raises(PolicyError):
            rs.update(CTX, {"plugin_timeout_seconds": bad})


def test_a_bad_field_leaves_every_setting_untouched(env):
    with pytest.raises(PolicyError):
        rs.update(CTX, {"draft_ttl_hours": 48, "scheduler_tick_seconds": 0})
    assert rs.runtime_settings().draft_ttl_hours == get_settings().draft_ttl_hours


def test_unknown_and_env_only_names_are_refused(env):
    with pytest.raises(PolicyError) as e:
        rs.update(CTX, {"no_such_setting": 1})
    assert e.value.code == "unknown_setting"
    for name in ("origin_secret", "cf_access_enabled", "cf_access_aud", "allow_insecure_admin",
                 "setup_token", "broker_secrets_key", "decision_signing_key",
                 "mcp_allowed_hosts", "broker_db"):
        with pytest.raises(PolicyError) as e:
            rs.update(CTX, {name: "x"})
        assert e.value.code == "env_only", name
    with pytest.raises(PolicyError):
        rs.update(CTX, {})


def test_an_invalid_stored_row_is_ignored_not_trusted(env):
    _stored("max_delegation_depth", 999)                     # hand-edited, out of bounds
    db.set_config(rs.PREFIX + "draft_ttl_hours", "not json")
    eff = rs.runtime_settings()
    assert eff.max_delegation_depth == get_settings().max_delegation_depth
    assert eff.draft_ttl_hours == get_settings().draft_ttl_hours


def test_every_settings_field_is_editable_or_listed_as_env_only(env):
    env_only = {e["field"] for e in rs.ENV_ONLY if e["field"]}
    for field in Settings.model_fields:
        assert field in rs.SPECS or field in env_only, field
    assert not set(rs.SPECS) & env_only


def test_describe_never_contains_secret_values(env, monkeypatch):
    secrets_ = {"SETUP_TOKEN": "setup-SECRET-1", "DECISION_SIGNING_KEY": "sign-SECRET-2",
                "ORIGIN_SECRET": "origin-SECRET-3"}
    for k, v in secrets_.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    text = json.dumps(rs.describe())
    for v in secrets_.values():
        assert v not in text
    rows = {e["name"]: e for e in rs.describe()["env_only"]}
    assert rows["SETUP_TOKEN"]["set"] is True and "value" not in rows["SETUP_TOKEN"]
    assert rows["BROKER_SECRETS_KEY"]["set"] is False
    assert rows["CF_ACCESS_ENABLED"]["value"] is False
    for name in ("ORIGIN_SECRET", "CF_ACCESS_ENABLED", "CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD",
                 "ALLOW_INSECURE_ADMIN", "SETUP_TOKEN", "BROKER_SECRETS_KEY",
                 "DECISION_SIGNING_KEY", "MCP_ALLOWED_HOSTS"):
        assert rows[name]["why"], name


def test_site_domain_is_an_exposure_setting(env):
    # The broker reads SITE_DOMAIN itself (services/plugins_admin.py) to build
    # the public OAuth redirect URI, so it is exposure, not compose-only.
    rows = {e["name"]: e for e in rs.describe()["env_only"]}
    assert rows["SITE_DOMAIN"]["category"] == "exposure"
    assert "OAuth" in rows["SITE_DOMAIN"]["why"]
    assert not any("SITE_DOMAIN" in e["name"] for e in rs.ENV_ONLY
                   if e["category"] == "compose")


def test_describe_reports_the_source_of_each_value(env, monkeypatch):
    monkeypatch.setenv("DRAFT_TTL_HOURS", "12")
    get_settings.cache_clear()
    rs.update(CTX, {"grant_max_hours": 48})
    items = {i["name"]: i for i in rs.describe()["settings"]}
    assert items["grant_max_hours"]["source"] == "console"
    assert items["grant_max_hours"]["value"] == 48
    assert items["draft_ttl_hours"]["source"] == "env"
    assert items["draft_ttl_hours"]["value"] == 12
    assert items["scheduler_tick_seconds"]["source"] == "default"
    assert items["max_delegation_depth"]["min"] == 0 and items["max_delegation_depth"]["max"] == 10


# ---- MCP hosts ----------------------------------------------------------------------

def test_extra_hosts_are_appended_never_replacing_the_env_list(env):
    base = list(rs.runtime_settings().mcp_allowed_hosts)
    assert "localhost:*" in base
    rs.update(CTX, {"mcp_allowed_hosts_extra": ["AAB.Example.com", "aab.example.com",
                                                "10.0.0.5:8080", "[::1]:*", "localhost:*"]})
    eff = rs.runtime_settings()
    assert list(eff.mcp_allowed_hosts[:len(base)]) == base
    assert eff.mcp_allowed_hosts.count("localhost:*") == 1
    assert "aab.example.com" in eff.mcp_allowed_hosts
    assert eff.mcp_allowed_hosts_extra == ("aab.example.com", "10.0.0.5:8080", "[::1]:*",
                                           "localhost:*")
    rs.update(CTX, {"mcp_allowed_hosts_extra": []})         # clearing extras keeps env
    assert list(rs.runtime_settings().mcp_allowed_hosts) == base


def test_a_hand_edited_row_cannot_drop_env_hosts(env):
    _stored("mcp_allowed_hosts_extra", [])
    db.set_config("setting:mcp_allowed_hosts", json.dumps([]))     # not a console setting
    assert "localhost:*" in rs.runtime_settings().mcp_allowed_hosts


@pytest.mark.parametrize("bad", ["*", "evil.com/path", "a b", "host:99999", "host:abc",
                                 "", "http://x.com", "x" * 300])
def test_malformed_hosts_are_refused(env, bad):
    with pytest.raises(PolicyError):
        rs.update(CTX, {"mcp_allowed_hosts_extra": [bad]})


def test_too_many_hosts_are_refused(env):
    with pytest.raises(PolicyError):
        rs.update(CTX, {"mcp_allowed_hosts_extra": [f"h{i}.example.com" for i in range(40)]})
    with pytest.raises(PolicyError):
        rs.update(CTX, {"mcp_allowed_hosts_extra": "localhost"})


# ---- call sites honour the overlay ------------------------------------------------------

def test_delegation_depth_override_applies_to_key_creation(env, owner):
    rs.update(CTX, {"max_delegation_depth": 0})
    root = auth.create_key(owner.id, "root", "full", 60, None)
    with pytest.raises(ValueError, match="depth"):
        auth.create_key(owner.id, "kid", "full", 60, None, parent_key_id=root.key_id,
                        created_by="delegation")


def test_schedule_bounds_follow_the_overlay(env):
    rs.update(CTX, {"schedule_min_lead_seconds": 600, "schedule_max_horizon_days": 1})
    now = int(time.time())
    with pytest.raises(PolicyError, match="600s"):
        queue.resolve_run_at(now + 300, None)
    with pytest.raises(PolicyError, match="1 days"):
        queue.resolve_run_at(now + 2 * 86400, None)
    assert queue.resolve_run_at(now + 3600, None) == now + 3600


def test_session_idle_limit_follows_the_overlay(env, owner, monkeypatch):
    cookie, _ = sessions.create(owner.id)
    assert sessions.lookup(cookie) is not None
    rs.update(CTX, {"session_idle_seconds": 300})
    real = time.time()
    monkeypatch.setattr(sessions, "_now", lambda: int(real) + 301)
    assert sessions.lookup(cookie) is None


def test_rotation_grace_follows_the_overlay(env, owner):
    rs.update(CTX, {"key_rotation_grace_seconds": 0})
    new = auth.create_key(owner.id, "rot", "full", 60, None)
    auth.rotate_key(new.key_id)
    assert auth.authenticate_bearer(f"Bearer {new.plaintext}") is None


def test_remote_adapter_reads_the_plugin_timeout_live(env):
    from broker.plugins import adapter as adapter_mod
    from broker.plugins import registry
    seen = []

    def factory(base_url, headers, timeout):
        seen.append(timeout)
        raise RuntimeError("stop here")

    from tests.conftest import echo_manifest
    # What the registry builds for every discovered plugin service.
    a = adapter_mod.RemoteAdapter("svc", "http://p", "tok", echo_manifest(),
                                  timeout=registry.live_plugin_timeout, client_factory=factory)
    rs.update(CTX, {"plugin_timeout_seconds": 7})
    with pytest.raises(Exception):
        a.status()
    rs.update(CTX, {"plugin_timeout_seconds": 9.5})
    with pytest.raises(Exception):
        a.status()
    assert seen == [7.0, 9.5]


def test_docs_list_every_setting_with_its_bounds():
    # docs/configuration.md is the owner-facing reference; keep it honest.
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[2] / "docs" / "configuration.md").read_text(
        encoding="utf-8")
    rows = {line.split("|")[1].strip().strip("`"): line for line in doc.splitlines()
            if line.startswith("| `")}
    for name, spec in rs.SPECS.items():
        assert name in rows, name
        if spec.kind != "hosts":
            cells = [c.strip() for c in rows[name].split("|")]
            assert cells[3] == rs._fmt(spec.lo) and cells[4] == rs._fmt(spec.hi), name
            assert cells[2] == str(Settings.model_fields[name].default), name


def test_discovered_plugins_read_the_live_timeout(env, echo_impl, tmp_path, vendored_echo):
    from broker.plugins import registry
    from tests.conftest import register_remote
    register_remote(echo_impl, tmp_path)
    assert registry.get_registry().adapter("echo")._timeout is registry.live_plugin_timeout


def test_the_new_relic_keys_are_listed_as_file_only_and_never_shown(env, monkeypatch):
    """The console names where log shipping is configured (the files) and
    why, and never shows the license key, even when the process has one."""
    monkeypatch.setenv("NEW_RELIC_LICENSE_KEY", "fake-license-not-for-display")
    get_settings.cache_clear()
    [row] = [e for e in rs.describe()["env_only"] if "NEW_RELIC_LICENSE_KEY" in e["name"]]
    for name in ("NEWRELIC_ENABLED", "NEW_RELIC_REGION", "AUDIT_EXPORT_INTERVAL",
                 "AUDIT_EXPORT_HASH_RESOURCES"):
        assert name in row["name"]
    assert row["category"] == "ops" and "by exception" in row["why"]
    assert "value" not in row and "set" not in row
    assert "fake-license-not-for-display" not in json.dumps(rs.describe())
