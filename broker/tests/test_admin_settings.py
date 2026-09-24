"""GET/PATCH /v1/admin/settings: owner-only, validated, audited by name."""

import json

from broker import db, runtime_settings as rs

from .conftest import CSRF_HEADERS


def _audits(action):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))]


def test_settings_routes_require_admin(client, owner):
    assert client.get("/v1/admin/settings").status_code == 401
    assert client.patch("/v1/admin/settings",
                        json={"settings": {"draft_ttl_hours": 2}}).status_code == 401
    assert rs.runtime_settings().draft_ttl_hours != 2


def test_get_lists_settings_and_the_env_only_keys(client, admin_headers):
    body = client.get("/v1/admin/settings", headers=admin_headers).json()
    names = [s["name"] for s in body["settings"]]
    assert names == list(rs.SPECS)
    env_only = {e["name"] for e in body["env_only"]}
    for name in ("ORIGIN_SECRET", "CF_ACCESS_ENABLED", "CF_ACCESS_AUD", "ALLOW_INSECURE_ADMIN",
                 "SETUP_TOKEN", "BROKER_SECRETS_KEY", "DECISION_SIGNING_KEY"):
        assert name in env_only
    assert all(e["why"] for e in body["env_only"])


def test_patch_applies_and_is_audited_by_name(client, admin_headers, owner):
    r = client.patch("/v1/admin/settings", headers=admin_headers,
                     json={"settings": {"draft_ttl_hours": 6,
                                        "mcp_allowed_hosts_extra": ["aab.example.com"]}})
    assert r.status_code == 200, r.text
    items = {i["name"]: i for i in r.json()["settings"]}
    assert items["draft_ttl_hours"]["value"] == 6
    assert "aab.example.com" in items["mcp_allowed_hosts_extra"]["effective"]
    assert rs.runtime_settings().draft_ttl_hours == 6
    [row] = _audits("settings.update")
    detail = json.loads(row["detail"])
    assert detail["changed"] == ["draft_ttl_hours", "mcp_allowed_hosts_extra"]
    assert (row["actor"], row["actor_principal"], row["actor_via"]) == \
        (owner.username, owner.id, "token")


def test_patch_rejects_bad_input_without_writing(client, admin_headers):
    for body in ({"settings": {"draft_ttl_hours": 0}},
                 {"settings": {"origin_secret": "x"}},
                 {"settings": {"cf_access_enabled": False}},
                 {"settings": {"nope": 1}},
                 {"settings": {}}):
        r = client.patch("/v1/admin/settings", headers=admin_headers, json=body)
        assert r.status_code == 400, body
    assert client.patch("/v1/admin/settings", headers=admin_headers,
                        json={"settings": {}, "extra": 1}).status_code == 422
    assert _audits("settings.update") == []


def test_patch_from_a_session_needs_the_csrf_header(session_client):
    body = {"settings": {"draft_ttl_hours": 3}}
    assert session_client.patch("/v1/admin/settings", json=body).status_code == 403
    r = session_client.patch("/v1/admin/settings", json=body, headers=CSRF_HEADERS)
    assert r.status_code == 200
    [row] = _audits("settings.update")
    assert row["actor_via"] == "session"


def test_null_resets_to_the_env_default(client, admin_headers):
    client.patch("/v1/admin/settings", headers=admin_headers,
                 json={"settings": {"grant_max_hours": 5}})
    r = client.patch("/v1/admin/settings", headers=admin_headers,
                     json={"settings": {"grant_max_hours": None}})
    item = next(i for i in r.json()["settings"] if i["name"] == "grant_max_hours")
    assert item["source"] == "default" and item["value"] == item["default"]
