"""Owner pins for external plugins: the pins module (storage, validation, the
review summary and diff), the admin routes that list offers and pin or unpin
them (audited under the owner, 404 when nothing is offered or pinned, 409
for ids the broker tree or the routes own), and that a DB-pinned plugin is a
plugin like any other for agents (the key-filtered skill doc lists it)."""

import json

import pytest
import yaml
from cryptography.fernet import Fernet

from aab_plugin_runtime import serve
from broker import db, hidden
from broker.plugins import pins, registry
from broker.plugins.manifest import ManifestError, load_manifest_text
from broker.plugins.registry import get_registry
from broker.services import plugins_admin

from .conftest import (ECHO_DIR, PLUGIN_TOKEN, cap, echo_manifest, enable_plugin,
                       register_remote, runtime_factory)

ECHO_TEXT = (ECHO_DIR / "manifest.yaml").read_text(encoding="utf-8")


@pytest.fixture()
def external(env, tmp_path):
    """Nothing vendored in the registry's tree: echo is an external plugin."""
    registry.reset_registry(registry.Registry(vendored_dirs=(tmp_path / "no-tree",)))


def offer(echo_impl, tmp_path, service="echosvc"):
    """`service` offers echo's manifest at discovery (refused until pinned)."""
    runtime = serve([echo_impl], PLUGIN_TOKEN, tmp_path / f"{service}-secrets",
                    Fernet.generate_key().decode())
    get_registry().discover({service: ("http://plugin-echo", PLUGIN_TOKEN)},
                            client_factory=runtime_factory(runtime))
    return runtime


def audit_rows(action):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))]


def edited(**changes) -> str:
    """Echo's manifest text with top-level keys replaced."""
    data = yaml.safe_load(ECHO_TEXT)
    data.update(changes)
    return yaml.safe_dump(data, sort_keys=False)


# ---- the pins module ---------------------------------------------------------------

def test_set_get_record_all_delete(env):
    assert pins.get("echo") is None and pins.record("echo") is None and pins.all() == []
    m = pins.set("echo", ECHO_TEXT, "github.com/o/r", "v1.2.3", "a" * 40, "owner")
    assert m.model_dump() == echo_manifest().model_dump()
    assert pins.get("echo").model_dump() == m.model_dump()
    rec = pins.record("echo")
    assert {k: rec[k] for k in ("plugin_id", "version", "source", "ref", "commit",
                                "pinned_by")} == {
        "plugin_id": "echo", "version": "0.1.0", "source": "github.com/o/r", "ref": "v1.2.3",
        "commit": "a" * 40, "pinned_by": "owner"}
    assert pins.all() == [rec]
    pins.set("echo", edited(version="0.2.0"), by="owner")          # replaces, one row
    assert [r["version"] for r in pins.all()] == ["0.2.0"]
    assert pins.record("echo")["source"] == ""
    assert pins.delete("echo") is True and pins.delete("echo") is False
    assert pins.get("echo") is None


@pytest.mark.parametrize("pid,text,by", [
    ("other", ECHO_TEXT, "owner"),               # declares another id
    ("echo", "id: [unclosed", "owner"),          # not YAML
    ("echo", "- a list", "owner"),               # not a mapping
    ("echo", edited(actions=[]), "owner"),       # fails manifest validation
    ("Bad-Id", ECHO_TEXT, "owner"),              # not a plugin id
])
def test_set_refuses_invalid_manifests(env, pid, text, by):
    with pytest.raises(ManifestError):
        pins.set(pid, text, by=by)
    assert pins.all() == []


def test_set_refuses_a_pin_nobody_approved(env):
    with pytest.raises(ValueError):
        pins.set("echo", ECHO_TEXT, by="  ")
    assert pins.all() == []


def test_junk_ids_never_reach_the_database(env):
    for junk in (None, "", "../echo", "echo; drop table plugin_pins", 7):
        assert pins.get(junk) is None and pins.record(junk) is None
        assert pins.delete(junk) is False


def test_summary_is_what_the_owner_approves():
    s = pins.summary(echo_manifest())
    assert (s["id"], s["version"], s["display_name"]) == ("echo", "0.1.0", "Echo")
    acts = {a["name"]: a for a in s["actions"]}
    assert acts["post_item"]["side_effect"] == "write"
    assert acts["post_item"]["modes"] == ["direct", "draft"]
    assert acts["list_items"]["modes"] == ["direct"]
    assert "delete_item" in s["side_effects"]["destructive"]
    assert s["secret_config"] == ["api_secret"]
    assert {f["name"]: f["secret"] for f in s["config"]} == {"greeting": False,
                                                             "api_secret": True}
    assert {n["dimension"] for n in s["narrowings"]} >= {"room", "folder", "sender"}
    assert s["connection"] == {"kind": "none", "enforcement": "proxy", "shared": None}
    json.dumps(s)                                             # plain data


def test_diff_of_a_first_pin_adds_everything():
    m = echo_manifest()
    d = pins.diff(None, m)
    assert d["from_version"] is None and d["to_version"] == "0.1.0" and d["changed"]
    assert d["actions"]["added"] == sorted(m.action_names)
    assert d["actions"]["removed"] == [] and d["actions"]["changed"] == []
    assert d["config"]["secret_added"] == ["api_secret"]
    assert d["connection"] is None


def test_diff_of_an_identical_manifest_changes_nothing():
    d = pins.diff(echo_manifest(), echo_manifest())
    assert d["changed"] is False
    for section in ("actions", "narrowings", "constraints", "config"):
        assert all(d[section][k] == [] for k in ("added", "removed", "changed"))


def test_diff_summarises_an_upgrade():
    old = echo_manifest()
    data = yaml.safe_load(ECHO_TEXT)
    data["version"] = "0.2.0"
    for a in data["actions"]:
        if a["name"] == "post_item":
            a["modes"] = ["draft"]                            # a mode change
    data["actions"].append({"name": "purge_room", "side_effect": "destructive"})
    data["actions"] = [a for a in data["actions"] if a["name"] != "touch_item"]
    data["narrowings"] = [n for n in data["narrowings"] if n["dimension"] != "sender"]
    data["config_schema"].append({"name": "webhook_key", "type": "string", "secret": True})
    for n in data["narrowings"]:
        n["applies_to"] = [x for x in n["applies_to"] if x != "touch_item"]
    for c in data.get("constraints", []):
        c["applies_to"] = [x for x in c["applies_to"] if x != "touch_item"]
    new = load_manifest_text(yaml.safe_dump(data, sort_keys=False))
    d = pins.diff(old, new)
    assert (d["from_version"], d["to_version"], d["changed"]) == ("0.1.0", "0.2.0", True)
    assert d["actions"]["added"] == ["purge_room"]
    assert d["actions"]["removed"] == ["touch_item"]
    post = next(c for c in d["actions"]["changed"] if c["name"] == "post_item")
    assert post["modes"] == {"from": ["direct", "draft"], "to": ["draft"]}
    assert post["side_effect"] == {"from": "write", "to": "write"}
    assert "sender" in d["narrowings"]["removed"]
    assert d["config"]["added"] == ["webhook_key"]
    assert d["config"]["secret_added"] == ["webhook_key"]


# ---- the routes ----------------------------------------------------------------------

def test_offered_is_empty_and_pin_is_404_when_nothing_is_offered(client, admin_headers,
                                                                  external):
    r = client.get("/v1/admin/plugins/offered", headers=admin_headers)
    assert r.status_code == 200 and r.json() == {"items": []}
    r = client.post("/v1/admin/plugins/echo/pin", headers=admin_headers)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    r = client.delete("/v1/admin/plugins/echo/pin", headers=admin_headers)
    assert r.status_code == 404
    assert pins.all() == [] and audit_rows("plugin.pin") == []


def test_offered_lists_the_review_card(client, admin_headers, external, echo_impl, tmp_path):
    offer(echo_impl, tmp_path)
    [item] = client.get("/v1/admin/plugins/offered", headers=admin_headers).json()["items"]
    assert item["id"] == "echo" and item["service"] == "echosvc"
    assert "no vendored manifest" in item["reason"]
    assert item["valid"] is True and item["error"] is None
    assert item["pinnable"] is True and item["blocked"] is None
    assert item["pinned"] is None
    assert item["summary"]["secret_config"] == ["api_secret"]
    assert item["diff"]["from_version"] is None and item["diff"]["to_version"] == "0.1.0"
    # The owner reviews it; agents see nothing of it.
    assert client.get("/v1/admin/plugins", headers=admin_headers).json()["items"] == []


def test_pin_registers_audits_and_clears_the_offer(client, admin_headers, owner, external,
                                                   echo_impl, tmp_path):
    offer(echo_impl, tmp_path)
    r = client.post("/v1/admin/plugins/echo/pin", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registered"] is True
    assert body["pin"]["version"] == "0.1.0" and body["pin"]["pinned_by"] == owner.username
    assert body["plugin"]["id"] == "echo" and body["plugin"]["enabled"] is False
    assert client.get("/v1/admin/plugins/offered", headers=admin_headers).json() == {"items": []}
    assert get_registry().service_of("echo") == "echosvc"
    [row] = audit_rows("plugin.pin")
    assert row["actor"] == owner.username and row["actor_via"] == "token"
    assert row["actor_principal"] == owner.id and row["result"] == "ok"
    detail = json.loads(row["detail"])
    assert detail["version"] == "0.1.0" and detail["previous"] is None
    assert detail["service"] == "echosvc" and detail["registered"] is True
    # The stored copy is a valid manifest, and it is the one in use.
    assert pins.get("echo").model_dump() == get_registry().manifests()["echo"].model_dump()


def test_unpin_withdraws_the_plugin_and_offers_it_again(client, admin_headers, owner, external,
                                                        echo_impl, tmp_path, make_agent):
    offer(echo_impl, tmp_path)
    client.post("/v1/admin/plugins/echo/pin", headers=admin_headers)
    enable_plugin()
    agent = make_agent([cap(["list_items"])])
    url = "/v1/targets/echo/actions/list_items"
    assert client.post(url, json={"params": {}}, headers=agent.headers).status_code == 200
    r = client.delete("/v1/admin/plugins/echo/pin", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"unpinned": "echo", "version": "0.1.0", "withdrawn": True}
    assert pins.record("echo") is None
    # Gone for agents at once (404, like any missing target), and disabled.
    assert client.post(url, json={"params": {}}, headers=agent.headers).status_code == 404
    assert client.get("/v1/admin/plugins/echo", headers=admin_headers).status_code == 404
    assert get_registry().plugin_rows()["echo"]["enabled"] == 0
    [item] = client.get("/v1/admin/plugins/offered", headers=admin_headers).json()["items"]
    assert item["id"] == "echo" and item["pinnable"] is True
    [row] = audit_rows("plugin.unpin")
    assert row["actor"] == owner.username and json.loads(row["detail"])["withdrawn"] is True
    assert client.delete("/v1/admin/plugins/echo/pin", headers=admin_headers).status_code == 404
    # Pinning again brings it back, disabled until the owner enables it.
    r = client.post("/v1/admin/plugins/echo/pin", headers=admin_headers)
    assert r.json()["registered"] is True and r.json()["plugin"]["enabled"] is False


def test_a_repin_shows_the_previous_version(client, admin_headers, external, echo_impl,
                                            tmp_path):
    pins.set("echo", edited(version="0.0.9"), by="owner")
    offer(echo_impl, tmp_path)                                # mismatch: refused, offered
    [item] = client.get("/v1/admin/plugins/offered", headers=admin_headers).json()["items"]
    assert item["pinned"]["version"] == "0.0.9"
    assert item["diff"]["from_version"] == "0.0.9" and item["diff"]["to_version"] == "0.1.0"
    r = client.post("/v1/admin/plugins/echo/pin", headers=admin_headers)
    assert r.status_code == 200 and r.json()["registered"] is True
    assert json.loads(audit_rows("plugin.pin")[-1]["detail"])["previous"] == "0.0.9"


def test_an_in_tree_plugin_cannot_be_pinned(client, admin_headers, vendored_echo, echo_impl,
                                            tmp_path):
    echo_impl.manifest = {**echo_impl.manifest, "version": "9.9.9"}
    offer(echo_impl, tmp_path)                                # mismatch with the tree
    [item] = client.get("/v1/admin/plugins/offered", headers=admin_headers).json()["items"]
    assert item["pinnable"] is False and "ships with the broker" in item["blocked"]
    r = client.post("/v1/admin/plugins/echo/pin", headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    assert pins.all() == [] and audit_rows("plugin.pin") == []


def test_an_id_served_by_another_service_cannot_be_pinned(client, admin_headers, external,
                                                          echo_impl, tmp_path):
    pins.set("echo", ECHO_TEXT, by="owner")
    offer(echo_impl, tmp_path, "echosvc")
    offer(echo_impl, tmp_path, "othersvc")                    # same id: refused, offered
    assert get_registry().service_of("echo") == "echosvc"
    r = client.post("/v1/admin/plugins/echo/pin", headers=admin_headers)
    assert r.status_code == 409 and "already served" in r.json()["error"]


def test_an_invalid_offer_is_shown_but_cannot_be_pinned(client, admin_headers, external,
                                                        echo_impl, tmp_path):
    echo_impl.manifest = {**echo_impl.manifest, "actions": []}
    offer(echo_impl, tmp_path)
    [item] = client.get("/v1/admin/plugins/offered", headers=admin_headers).json()["items"]
    assert item["valid"] is False and item["error"] and item["summary"] is None
    r = client.post("/v1/admin/plugins/echo/pin", headers=admin_headers)
    assert r.status_code == 400 and r.json()["code"] == "invalid_manifest"
    [row] = audit_rows("plugin.pin")
    assert row["result"] == "error" and pins.all() == []


def test_reserved_and_invalid_ids_cannot_be_pinned(external, admin_ctx):
    from broker.errors import PolicyError
    text = ECHO_TEXT.replace("id: echo", "id: offered")
    for pid, manifest in (("offered", text), ("Bad", ECHO_TEXT)):
        with pytest.raises(PolicyError) as e:
            plugins_admin.pin_manifest(admin_ctx, pid, manifest)
        assert e.value.status == 409
    assert pins.all() == []


def test_pin_manifest_pins_before_the_service_exists(external, admin_ctx, echo_impl, tmp_path):
    """The install flow: the owner approves the manifest first, the service
    comes up later and is registered by ordinary discovery."""
    out = plugins_admin.pin_manifest(admin_ctx, "echo", ECHO_TEXT, source="github.com/o/r",
                                     ref="v0.1.0", commit="b" * 40, service="echosvc")
    assert out["registered"] is False and out["plugin"] is None
    assert (out["pin"]["source"], out["pin"]["ref"], out["pin"]["commit"]) == (
        "github.com/o/r", "v0.1.0", "b" * 40)
    detail = json.loads(audit_rows("plugin.pin")[0]["detail"])
    assert (detail["source"], detail["ref"], detail["commit"]) == (
        "github.com/o/r", "v0.1.0", "b" * 40)
    offer(echo_impl, tmp_path)
    assert get_registry().service_of("echo") == "echosvc"


def test_pin_manifest_refuses_invalid_text_and_audits(external, admin_ctx):
    from broker.errors import PolicyError
    with pytest.raises(PolicyError) as e:
        plugins_admin.pin_manifest(admin_ctx, "echo", edited(actions=[]))
    assert e.value.status == 400 and e.value.code == "invalid_manifest"
    assert audit_rows("plugin.pin")[0]["result"] == "error" and pins.all() == []


def test_hiding_a_resource_keeps_offers_awaiting_review(client, admin_headers, external,
                                                        echo_impl, tmp_path):
    # add_hidden clears the ancestry cache only; clear_cache would drop offers.
    offer(echo_impl, tmp_path)
    pins.set("echo", ECHO_TEXT, by="owner")
    get_registry().repin("echo")
    offer(echo_impl, tmp_path, "othersvc")                    # a second offer, refused
    r = client.post("/v1/admin/hidden", headers=admin_headers,
                    json={"target": "echo", "kind": "room", "resource_id": "r2"})
    assert r.status_code == 200, r.text
    assert "r2" in hidden.hidden_sets("echo").get("room", set())
    assert "echo" in get_registry().offered


@pytest.mark.parametrize("method,path", [("GET", "/v1/admin/plugins/offered"),
                                         ("POST", "/v1/admin/plugins/echo/pin"),
                                         ("DELETE", "/v1/admin/plugins/echo/pin")])
def test_pin_routes_need_the_owner(client, external, make_agent, method, path):
    assert client.request(method, path).status_code == 401
    agent = make_agent()
    assert client.request(method, path, headers=agent.headers).status_code == 401


# ---- agents see a DB-pinned plugin like any other -------------------------------------

def test_me_skill_lists_a_database_pinned_plugin(client, owner, external, echo_impl, tmp_path,
                                                 make_agent):
    pins.set("echo", ECHO_TEXT, by=owner.username)
    register_remote(echo_impl, tmp_path)
    enable_plugin()
    agent = make_agent([cap(["list_items"])], name="reader")
    r = client.get("/v1/me/skill", headers=agent.headers)
    assert r.status_code == 200
    assert "### Echo (`echo`)" in r.text
    assert "`POST /v1/targets/echo/actions/list_items`" in r.text
    assert "post_item" not in r.text                          # still filtered to the key
