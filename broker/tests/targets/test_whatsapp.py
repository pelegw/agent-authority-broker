"""The WhatsApp plugin end to end: the real plugin app (plugins/whatsapp)
served by the plugin runtime, discovered and pinned by the registry, reached
through RemoteAdapter, and driven through engine.perform and the REST surface.

What must hold across the broker/plugin boundary:
  * the vendored manifest is byte-identical to the plugin's own;
  * a hidden chat is the same 404 as a missing one on every get, and absent
    from every list, search and the new-messages feed;
  * a hidden chat's media never reaches the sidecar;
  * a draft-mode capability turns send_message into pending_approval;
  * sidecar 503 (not sent) and 502 (unknown) are recorded as such;
  * the broker's resource_ref post-filter holds even if the plugin leaks.
"""

import threading
import time

import httpx
import pytest

from broker import db, engine, hidden, notify
from broker.actions import queue
from broker.errors import PolicyError
from broker.plugins.adapter import AdapterError, CallScope
from broker.plugins.registry import TARGETS_DIR, Registry, get_registry

from .conftest import WA_TOKEN, WHATSAPP_DIR, fakes, wa_cap

ALICE, BOB, GROUP, CAROL = fakes.ALICE, fakes.BOB, fakes.GROUP, fakes.CAROL
MISSING = "999999999@s.whatsapp.net"
ACT = "/v1/targets/whatsapp/actions"
NOT_FOUND = {"error": "not found", "code": "not_found"}
READS = ["list_chats", "get_chat", "read_messages", "search_messages",
         "check_new_messages", "search_contacts", "get_media"]


def call(client, agent, action, params=None, **body):
    return client.post(f"{ACT}/{action}", json={"params": params or {}, **body},
                       headers=agent.headers)


def items(r) -> list[dict]:
    assert r.status_code == 200, r.text
    return r.json()["items"]


def outcomes() -> list[str]:
    with db.connect() as conn:
        return [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM decisions WHERE kind = 'outcome' ORDER BY id")]


def ledger_states() -> list[str]:
    with db.connect() as conn:
        return [r["state"] for r in conn.execute("SELECT state FROM capacity_ledger ORDER BY id")]


# ---- the manifest and discovery ---------------------------------------------------

def test_vendored_manifest_is_byte_identical_to_the_plugins():
    plugin = (WHATSAPP_DIR / "aab_plugin_whatsapp" / "manifest.yaml").read_bytes()
    vendored = (TARGETS_DIR / "whatsapp" / "manifest.yaml").read_bytes()
    assert plugin == vendored, ("broker/broker/targets/whatsapp/manifest.yaml drifted from "
                                "plugins/whatsapp/aab_plugin_whatsapp/manifest.yaml")


def test_manifest_has_nothing_to_configure_in_the_console():
    m = Registry().vendored("whatsapp")
    # Configuration principle: SIDECAR_URL, SIDECAR_TOKEN and MESSAGES_DB are
    # the container's deployment env, never broker-side config.
    assert m.config_schema == []
    assert m.connection.kind == "sidecar_qr" and m.connection.enforcement == "proxy"


def test_discovery_pins_the_plugin_to_the_vendored_manifest(wa):
    reg = get_registry()
    e = reg.entries()["whatsapp"]
    assert e.service == "whatsapp" and type(e.adapter).__name__ == "RemoteAdapter"
    # Pinned: the broker uses its own vendored copy, not what the plugin offered.
    assert e.manifest.model_dump() == reg.vendored("whatsapp").model_dump()
    assert reg.refused == {}


def test_a_plugin_offering_another_version_is_refused(env, owner, tmp_path):
    from aab_plugin_runtime import serve
    from aab_plugin_whatsapp.adapter import WhatsAppAdapter
    from aab_plugin_whatsapp.archive import Archive
    from cryptography.fernet import Fernet

    from tests.conftest import runtime_factory
    adapter = WhatsAppAdapter(fakes.FakeSidecar().client(), Archive(str(tmp_path / "m.db")))
    adapter.manifest = {**adapter.manifest, "version": "9.9.9"}
    runtime = serve([adapter], WA_TOKEN, tmp_path / "s", Fernet.generate_key().decode())
    get_registry().discover({"whatsapp": ("http://plugin-whatsapp:8090", WA_TOKEN)},
                            client_factory=runtime_factory(runtime))
    assert "whatsapp" not in get_registry().entries()
    assert "manifest mismatch" in get_registry().refused["whatsapp"]


# ---- reads -------------------------------------------------------------------------

def test_reads_through_rest(client, wa, make_agent):
    a = make_agent([wa_cap(READS)])
    assert [c["jid"] for c in items(call(client, a, "list_chats"))] == [BOB, GROUP, ALICE]
    assert call(client, a, "get_chat", {"chat": "+972502222222"}).json()["name"] == "Bob"
    assert [m["id"] for m in items(call(client, a, "read_messages", {"chat": ALICE}))] == [
        "A2", "A1"]
    assert [m["id"] for m in items(call(client, a, "search_messages",
                                        {"query": "dessert"}))] == ["G1"]
    assert [c["jid"] for c in items(call(client, a, "search_contacts",
                                         {"query": "cohen"}))] == [ALICE]
    media = call(client, a, "get_media", {"chat": BOB, "message_id": "B1"})
    assert media.status_code == 200 and media.content == fakes.IMAGE_BYTES
    assert media.headers["content-type"].startswith("image/jpeg")


def test_engine_binary_and_selector(wa, make_agent):
    a = make_agent([wa_cap(["get_media", "list_chats"], selector={"chat": [BOB]})])
    r = engine.perform(a.auth, "whatsapp", "get_media", {"chat": BOB, "message_id": "B1"})
    assert (r.binary, r.mime) == (fakes.IMAGE_BYTES, "image/jpeg")
    # The capability's selector reaches the plugin as allow_only.
    assert [c["jid"] for c in engine.perform(a.auth, "whatsapp", "list_chats", {}).body[
        "items"]] == [BOB]


# ---- hidden == 404 -----------------------------------------------------------------

@pytest.mark.parametrize("action,params,hide", [
    ("get_chat", {}, ALICE),
    ("read_messages", {}, ALICE),
    ("search_messages", {"query": "lunch"}, ALICE),
    ("get_media", {"message_id": "B1"}, BOB),
])
def test_hidden_chat_is_the_same_404_as_a_missing_one(client, wa, make_agent, action,
                                                      params, hide):
    a = make_agent([wa_cap(READS)])
    hidden.add("whatsapp", "chat", hide)
    hid = call(client, a, action, {**params, "chat": hide})
    gone = call(client, a, action, {**params, "chat": MISSING})
    assert hid.status_code == gone.status_code == 404
    assert hid.json() == gone.json() == NOT_FOUND
    assert wa.sidecar.media_calls == []


def test_hidden_via_the_admin_api_is_normalized_by_the_plugin(client, wa, make_agent,
                                                              admin_headers):
    a = make_agent([wa_cap(READS)])
    r = client.post("/v1/admin/hidden", json={"target": "whatsapp", "kind": "chat",
                                              "resource_id": "+972501111111"},
                    headers=admin_headers)
    assert r.status_code == 200 and r.json()["resource_id"] == ALICE
    # A device-suffixed alias is the same chat: still hidden.
    alias = call(client, a, "get_chat", {"chat": "972501111111:7@s.whatsapp.net"})
    assert alias.status_code == 404 and alias.json() == NOT_FOUND


def test_hidden_chat_is_absent_from_lists_search_and_events(client, wa, make_agent):
    a = make_agent([wa_cap(READS)])
    cursor = call(client, a, "check_new_messages").json()["cursor"]
    hidden.add("whatsapp", "chat", ALICE)
    fakes.insert_message(wa.archive, ALICE, "SECRET1", "private ping")
    fakes.insert_message(wa.archive, BOB, "PUB1", "public ping")
    assert ALICE not in [c["jid"] for c in items(call(client, a, "list_chats"))]
    assert items(call(client, a, "search_messages", {"query": "lunch"})) == []
    assert [m["text"] for m in items(call(client, a, "search_messages",
                                          {"query": "ping"}))] == ["public ping"]
    feed = call(client, a, "check_new_messages", {"cursor": cursor}).json()
    assert [m["text"] for m in feed["items"]] == ["public ping"]
    # The REST long-poll route (params as query string) filters the same way.
    polled = client.get(f"{ACT}/check_new_messages", params={"cursor": cursor, "wait": 0},
                        headers=a.headers).json()
    assert [m["text"] for m in polled["items"]] == ["public ping"]
    # Contacts stay visible by design (they are the name -> JID resolver).
    assert ALICE in [c["jid"] for c in items(call(client, a, "search_contacts",
                                                  {"query": "alice"}))]


def test_media_for_a_hidden_chat_never_reaches_the_sidecar(client, wa, make_agent):
    params = {"chat": BOB, "message_id": "B1"}
    ok = make_agent([wa_cap(["get_media"])])
    assert call(client, ok, "get_media", params).status_code == 200
    assert wa.sidecar.media_calls == [(BOB, "B1")]

    denied = make_agent([wa_cap(["get_media"])], denies={"whatsapp": {"chat": [BOB]}})
    assert call(client, denied, "get_media", params).status_code == 404
    narrow = make_agent([wa_cap(["get_media"], selector={"chat": [ALICE]})])
    assert call(client, narrow, "get_media", params).status_code == 403
    hidden.add("whatsapp", "chat", BOB)
    assert call(client, ok, "get_media", params).json() == NOT_FOUND
    # The plugin holds the line on its own too: a deny scope sent straight to
    # it (as if the broker had missed it) is refused before the sidecar.
    with pytest.raises(AdapterError) as e:
        get_registry().adapter("whatsapp").perform(
            "get_media", params, CallScope("direct", visibility={
                "chat": {"deny": [BOB], "allow_only": None}}))
    assert e.value.status == 404
    assert wa.sidecar.media_calls == [(BOB, "B1")]          # only the first, allowed call


# ---- sends -------------------------------------------------------------------------

def test_direct_send_is_normalized_and_bounded_by_the_selector(client, wa, make_agent):
    a = make_agent([wa_cap(["send_message"], selector={"chat": [ALICE]})])
    r = call(client, a, "send_message", {"to": "+972501111111", "text": "hi"})
    assert r.status_code == 200 and r.json() == {"status": "sent", "message_id": "MSG1",
                                                 "ts": 1700000000}
    off = call(client, a, "send_message", {"to": BOB, "text": "hi"})
    assert off.status_code == 403 and off.json()["code"] == "out_of_grant"
    assert wa.sidecar.sent == [(ALICE, "hi")]


def test_send_to_a_hidden_chat_is_404_and_never_sent(client, wa, make_agent):
    a = make_agent([wa_cap(["send_message"])])
    hidden.add("whatsapp", "chat", ALICE)
    for to in (ALICE, "+972501111111", "972501111111:3@s.whatsapp.net"):
        r = call(client, a, "send_message", {"to": to, "text": "x"})
        assert r.status_code == 404 and r.json() == NOT_FOUND, to
    assert wa.sidecar.sent == []


def test_non_canonical_recipient_is_400(client, wa, make_agent):
    a = make_agent([wa_cap(["send_message"])])
    r = call(client, a, "send_message", {"to": "+972501111111@s.whatsapp.net", "text": "x"})
    assert r.status_code == 400 and wa.sidecar.sent == []


def test_draft_capability_makes_send_pending_approval(client, wa, make_agent, admin_headers,
                                                      monkeypatch):
    got = []

    class Provider:
        def notify_action(self, item):
            got.append(item)

    monkeypatch.setattr(notify, "_PROVIDERS", [Provider()])
    a = make_agent([wa_cap(["send_message"], mode="draft")])
    r = call(client, a, "send_message", {"to": "+972501111111", "text": "hi"})
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"
    assert wa.sidecar.sent == []                             # nothing sent yet
    row = queue.get_row(r.json()["action_id"])
    assert row["status"] == "pending" and row["params"]["to"] == ALICE
    assert row["resource_label"] == "Alice"                  # labelled by the plugin
    assert got[0]["summary"] == "Send to Alice: hi"
    done = client.post(f"/v1/admin/actions/{row['id']}/approve", headers=admin_headers)
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert wa.sidecar.sent == [(ALICE, "hi")]


# ---- 503 / 502 ---------------------------------------------------------------------

@pytest.mark.parametrize("failure,status,code,outcome,ledger,sent", [
    ((503, False), 503, "unavailable", "unavailable", "released", []),
    ((httpx.ConnectError, False), 503, "unavailable", "unavailable", "released", []),
    ((401, False), 503, "unavailable", "unavailable", "released", []),
    ((httpx.ReadTimeout, True), 502, "unknown_outcome", "unknown", "reserved", [(ALICE, "x")]),
    ((502, True), 502, "unknown_outcome", "unknown", "reserved", [(ALICE, "x")]),
    ((500, True), 502, "unknown_outcome", "unknown", "reserved", [(ALICE, "x")]),
])
def test_sidecar_failures_follow_the_contract(client, wa, make_agent, failure, status, code,
                                              outcome, ledger, sent):
    a = make_agent([wa_cap(["send_message"])])
    wa.sidecar.fail_next["/send"] = failure
    r = call(client, a, "send_message", {"to": ALICE, "text": "x"})
    assert r.status_code == status and r.json()["code"] == code
    assert outcomes() == [outcome]
    assert ledger_states() == [ledger]      # 503 frees the budget; 502 keeps it (24 h)
    assert wa.sidecar.sent == sent


def test_queued_send_503_returns_to_pending_502_fails_for_good(client, wa, make_agent,
                                                               admin_headers):
    a = make_agent([wa_cap(["send_message"], mode="draft")])
    action_id = call(client, a, "send_message", {"to": ALICE, "text": "x"}).json()["action_id"]
    wa.sidecar.fail_next["/send"] = (httpx.ConnectError, False)
    r = client.post(f"/v1/admin/actions/{action_id}/approve", headers=admin_headers)
    assert r.status_code == 503 and queue.get_row(action_id)["status"] == "pending"
    wa.sidecar.fail_next["/send"] = (httpx.ReadTimeout, True)
    r = client.post(f"/v1/admin/actions/{action_id}/approve", headers=admin_headers)
    assert r.status_code == 502 and queue.get_row(action_id)["status"] == "failed"
    # Never retried: a failed row cannot be approved again.
    assert client.post(f"/v1/admin/actions/{action_id}/approve",
                       headers=admin_headers).status_code == 409
    assert wa.sidecar.sent == [(ALICE, "x")]


def test_reads_when_the_archive_is_locked_are_503(wa, make_agent, monkeypatch):
    import sqlite3
    a = make_agent([wa_cap(READS)])

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(wa.adapter.archive, "list_chats", locked)
    with pytest.raises(PolicyError) as e:
        engine.perform(a.auth, "whatsapp", "list_chats", {})
    assert (e.value.status, e.value.code) == (503, "unavailable")


# ---- belt and braces: the broker's post-filter --------------------------------------

def test_post_filter_holds_when_the_plugin_leaks(client, wa, make_agent, monkeypatch):
    a = make_agent([wa_cap(READS)])
    cursor = call(client, a, "check_new_messages").json()["cursor"]
    hidden.add("whatsapp", "chat", ALICE)
    fakes.insert_message(wa.archive, ALICE, "LEAK1", "leak me")
    fakes.insert_message(wa.archive, BOB, "OK1", "fine to see")
    # Break the plugin: it now ignores the chat visibility entirely.
    monkeypatch.setattr(wa.adapter, "_chat_scope", lambda scope: ([], None))
    leaked = wa.adapter.perform("list_chats", {}, {"visibility": {
        "chat": {"deny": [ALICE], "allow_only": None}}}).data["items"]
    assert ALICE in [c["jid"] for c in leaked]              # the leak is real

    assert ALICE not in [c["jid"] for c in items(call(client, a, "list_chats"))]
    assert items(call(client, a, "search_messages", {"query": "lunch"})) == []
    assert items(call(client, a, "search_messages", {"query": "leak"})) == []
    feed = call(client, a, "check_new_messages", {"cursor": cursor}).json()["items"]
    assert [m["text"] for m in feed] == ["fine to see"]


def test_post_filter_enforces_the_selector_when_the_plugin_leaks(client, wa, make_agent,
                                                                  monkeypatch):
    a = make_agent([wa_cap(READS, selector={"chat": [BOB]})])
    monkeypatch.setattr(wa.adapter, "_chat_scope", lambda scope: ([], None))
    assert [c["jid"] for c in items(call(client, a, "list_chats"))] == [BOB]
    assert {m["chat_jid"] for m in items(call(client, a, "search_messages",
                                              {"query": "e"}))} <= {BOB}


# ---- the owner's side: enable, health, QR, disconnect ------------------------------

def test_admin_enable_health_qr_and_disconnect(client, admin_headers, wa_disabled):
    base = "/v1/admin/plugins/whatsapp"
    view = client.get(base, headers=admin_headers).json()
    assert view["config_schema"] == [] and view["enabled"] is False
    # Nothing to configure: every would-be field is unknown.
    r = client.patch(base, json={"config": {"sidecar_url": "http://evil.test"}},
                     headers=admin_headers)
    assert r.status_code == 400

    body = client.post(f"{base}/enable", headers=admin_headers).json()
    assert body["enabled"] is True and body["connected"] is True
    assert body["last_health"]["health"] == "ok"
    assert body["last_health"]["connection"]["kind"] == "sidecar_qr"

    wa_disabled.sidecar.paired(False)
    body = client.post(f"{base}/health", headers=admin_headers).json()
    assert body["connected"] is False
    assert body["last_health"]["health"] == "waiting for QR pairing"
    assert client.post(f"{base}/connect/start", headers=admin_headers).json() == {"kind": "qr"}
    qr = client.get(f"{base}/connect/qr.png", headers=admin_headers)
    assert qr.status_code == 200 and qr.content == wa_disabled.sidecar.qr
    assert qr.headers["cache-control"] == "no-store"

    wa_disabled.sidecar.paired(True)              # the owner scanned the code
    assert client.post(f"{base}/connect/finish", json={},
                       headers=admin_headers).json() == {"ok": True}
    assert client.get(base, headers=admin_headers).json()["connected"] is True
    assert client.get(f"{base}/connect/qr.png", headers=admin_headers).status_code == 409
    r = client.post(f"{base}/disconnect", headers=admin_headers)
    assert r.status_code == 409 and "Linked devices" in r.json()["error"]


def test_sidecar_down_keeps_the_last_known_connection(client, admin_headers, wa):
    wa.sidecar.fail_next["/status"] = (httpx.ConnectError, False)
    body = client.post("/v1/admin/plugins/whatsapp/health", headers=admin_headers).json()
    assert body["connected"] is True and body["last_health"]["healthy"] is False


def test_not_paired_means_every_action_is_503(client, admin_headers, wa, make_agent):
    wa.sidecar.paired(False)
    client.post("/v1/admin/plugins/whatsapp/health", headers=admin_headers)
    a = make_agent([wa_cap(READS)])
    r = call(client, a, "list_chats")
    assert r.status_code == 503 and r.json()["code"] == "not_connected"


def test_resolve_for_agents_is_filtered_by_the_broker(client, wa, make_agent):
    a = make_agent([wa_cap(READS, selector={"chat": [ALICE, BOB]})])
    hidden.add("whatsapp", "chat", BOB)
    r = client.get("/v1/targets/whatsapp/resolve", params={"kind": "chat", "q": "o"},
                   headers=a.headers).json()["items"]
    ids = {i["id"] for i in r}
    assert BOB not in ids and GROUP not in ids and CAROL not in ids


def test_long_poll_returns_when_a_message_lands(client, wa, make_agent, monkeypatch):
    monkeypatch.setenv("LONG_POLL_INTERVAL_SECONDS", "0.05")
    from broker.config import get_settings
    get_settings.cache_clear()
    a = make_agent([wa_cap(["check_new_messages"])])
    cursor = call(client, a, "check_new_messages").json()["cursor"]
    t = threading.Timer(0.3, fakes.insert_message, args=(wa.archive, BOB, "LATE1", "arrived"))
    t.start()
    t0 = time.monotonic()
    body = client.get(f"{ACT}/check_new_messages", params={"cursor": cursor, "wait": 5},
                      headers=a.headers).json()
    t.join()
    assert [m["text"] for m in body["items"]] == ["arrived"]
    assert time.monotonic() - t0 < 4

