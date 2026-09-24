"""OpenAPI generated per request from enabled manifests, and the
key-filtered /v1/me/openapi.json."""

from broker.plugins import settings
from broker.plugins.registry import get_registry

from .conftest import cap

P = "/v1/targets/echo/actions/"


def body_schema(doc, action):
    return doc["paths"][P + action]["post"]["requestBody"]["content"]["application/json"][
        "schema"]


def test_one_path_per_enabled_action(client, echo_local):
    doc = client.get("/openapi.json").json()
    assert "/v1/targets/{target}/actions/{action}" not in doc["paths"]
    actions = {p[len(P):] for p in doc["paths"] if p.startswith(P)}
    assert actions == set(get_registry().manifests()["echo"].action_names)
    post = body_schema(doc, "post_item")
    manifest_params = get_registry().manifests()["echo"].action("post_item").params
    assert post["properties"]["params"] == manifest_params
    assert {"as_draft", "run_at", "delay_seconds", "note"} <= set(post["properties"])
    assert doc["paths"][P + "post_item"]["post"]["summary"] == "Post an item to a room."


def test_mode_and_schedule_fields_only_where_allowed(client, echo_local):
    doc = client.get("/openapi.json").json()
    assert "as_draft" not in body_schema(doc, "list_items")["properties"]
    assert "as_draft" not in body_schema(doc, "touch_item")["properties"]   # direct only
    assert "run_at" not in body_schema(doc, "delete_item")["properties"]    # not schedulable
    assert "get" in doc["paths"][P + "watch"]                               # long-poll
    blob = doc["paths"][P + "get_blob"]["post"]["responses"]["200"]["content"]
    assert "application/octet-stream" in blob


def test_disabled_plugin_disappears_from_openapi(client, echo_local):
    settings.set_enabled("echo", False)
    doc = client.get("/openapi.json").json()
    assert not any(p.startswith(P) for p in doc["paths"])


def test_me_openapi_is_filtered_to_the_key(client, echo_local, make_agent):
    a = make_agent([cap(["list_items", "post_item"])])
    doc = client.get("/v1/me/openapi.json", headers=a.headers).json()
    actions = {p[len(P):] for p in doc["paths"] if p.startswith(P)}
    assert actions == {"list_items", "post_item"}
    assert not any(p.startswith(("/v1/admin", "/auth", "/oauth")) for p in doc["paths"])
    assert "/v1/me" in doc["paths"] and "/v1/permissions" in doc["paths"]
    assert doc["components"]["schemas"]["Error"]["required"] == ["error", "code"]


def test_me_openapi_needs_a_key(client, echo_local):
    assert client.get("/v1/me/openapi.json").status_code == 401


def test_key_without_grants_sees_no_actions(client, echo_local, make_agent):
    doc = client.get("/v1/me/openapi.json", headers=make_agent().headers).json()
    assert not any(p.startswith(P) for p in doc["paths"])
