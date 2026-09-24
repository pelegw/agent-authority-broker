"""Read actions against a seeded archive (and an absent one), through the
runtime. Ported from WA_GW tests/test_read_api.py."""

import base64
import os

import pytest

from .conftest import items, scope
from .fakes import ALICE, BOB, CAROL, GROUP, IMAGE_BYTES, insert_message


def test_list_chats_ordered_by_recency(perform):
    assert [c["jid"] for c in items(perform("list_chats"))] == [BOB, GROUP, ALICE]


def test_list_chats_filters_by_name(perform):
    assert [c["jid"] for c in items(perform("list_chats", {"query": "fam"}))] == [GROUP]


def test_every_row_names_its_chat(perform):
    # The broker's post-filter drops any row whose resource_ref it may not see.
    for c in items(perform("list_chats")):
        assert c["resource_ref"] == {"kind": "chat", "id": c["jid"]}
    for action, params in [("read_messages", {"chat": ALICE}),
                           ("search_messages", {"query": "o"})]:
        for m in items(perform(action, params)):
            assert m["resource_ref"] == {"kind": "chat", "id": m["chat_jid"]}
    chat = perform("get_chat", {"chat": GROUP}).json()["data"]
    assert chat["resource_ref"] == {"kind": "chat", "id": GROUP}
    for c in items(perform("search_contacts", {"query": "o"})):
        assert c["resource_ref"] == {"kind": "contact", "id": c["jid"]}


def test_get_chat_normalizes_its_argument(perform):
    r = perform("get_chat", {"chat": "+972501111111"})
    assert r.json()["data"]["jid"] == ALICE and r.json()["data"]["name"] == "Alice"


def test_chat_messages_with_cursor(perform):
    assert [m["id"] for m in items(perform("read_messages", {"chat": ALICE}))] == ["A2", "A1"]
    older = items(perform("read_messages", {"chat": ALICE, "before": 1000}))
    assert [m["id"] for m in older] == ["A1"]


def test_media_flag_without_media_bytes(perform):
    msg = items(perform("read_messages", {"chat": BOB}))[0]
    assert msg["has_media"] == 1
    assert "media_ref" not in msg  # raw decryption keys never leave the plugin


def test_search_across_and_within_chats(perform):
    assert [m["id"] for m in items(perform("search_messages", {"query": "dessert"}))] == ["G1"]
    within = items(perform("search_messages", {"query": "o", "chat": ALICE}))
    assert within and all(m["chat_jid"] == ALICE for m in within)


def test_contacts_search(perform):
    assert [c["jid"] for c in items(perform("search_contacts", {"query": "cohen"}))] == [ALICE]


def test_unknown_chat_is_404(perform):
    for action, params in [("get_chat", {"chat": "999999@s.whatsapp.net"}),
                           ("read_messages", {"chat": "999999@s.whatsapp.net"}),
                           ("search_messages", {"query": "x", "chat": "999999@s.whatsapp.net"}),
                           ("get_media", {"chat": "999999@s.whatsapp.net", "message_id": "B1"})]:
        r = perform(action, params)
        assert r.status_code == 404 and r.json() == {"error": "no such chat"}, action


def test_empty_when_archive_missing(perform, archive):
    # Sidecar hasn't paired yet -> no messages.db. Reads degrade to empty, not 500.
    os.remove(archive.path)
    assert items(perform("list_chats")) == []
    assert items(perform("search_contacts", {"query": "a"})) == []
    assert items(perform("search_messages", {"query": "a"})) == []
    assert perform("check_new_messages").json()["data"] == {"cursor": 0, "items": []}


def test_negative_limit_cannot_dump_tables(perform):
    # SQLite treats LIMIT -1 as unlimited; the clamp must stop that.
    assert len(items(perform("list_chats", {"limit": -1}))) == 1
    assert len(items(perform("read_messages", {"chat": ALICE, "limit": -5}))) == 1


def test_huge_limit_is_clamped(perform, archive):
    for n in range(300):
        insert_message(archive.path, ALICE, f"X{n}", "bulk", ts=500)
    assert len(items(perform("read_messages", {"chat": ALICE, "limit": 10**9}))) == 200


def test_same_second_pagination_with_before_id(perform, archive):
    # Two messages in the same second: a plain ts cursor would skip one.
    insert_message(archive.path, ALICE, "A3", "same second", ts=1000)
    page1 = items(perform("read_messages", {"chat": ALICE, "limit": 1}))
    top = page1[0]
    page2 = items(perform("read_messages", {"chat": ALICE, "limit": 10, "before": top["ts"],
                                            "before_id": top["id"]}))
    ids = {m["id"] for m in page1} | {m["id"] for m in page2}
    assert ids == {"A1", "A2", "A3"}  # nothing skipped, nothing duplicated
    assert len(page1) + len(page2) == 3


def test_media_proxies_sidecar(perform, sidecar):
    r = perform("get_media", {"chat": BOB, "message_id": "B1"})
    assert r.status_code == 200
    body = r.json()
    assert base64.b64decode(body["binary_b64"]) == IMAGE_BYTES
    assert body["mime"] == "image/jpeg"
    assert sidecar.media_calls == [(BOB, "B1")]


def test_media_missing_message_is_404(perform, sidecar):
    r = perform("get_media", {"chat": BOB, "message_id": "NOPE"})
    assert r.status_code == 404


@pytest.mark.parametrize("claimed,served", [
    ("image/jpeg", "image/jpeg"), ("Image/PNG; charset=x", "image/png"),
    ("application/pdf", "application/pdf"), ("audio/ogg; codecs=opus", "audio/ogg"),
    ("text/html", "application/octet-stream"), ("image/svg+xml", "application/octet-stream"),
    ("application/xhtml+xml", "application/octet-stream"), ("", "application/octet-stream"),
    ("text/html\r\nX-Evil: 1", "application/octet-stream"),
    ("no-slash", "application/octet-stream"),
])
def test_media_type_is_never_active_content(perform, sidecar, claimed, served):
    # The sender of a WhatsApp document chooses its mime type.
    sidecar.media[(BOB, "B1")] = (b"<script>alert(1)</script>", claimed)
    r = perform("get_media", {"chat": BOB, "message_id": "B1"})
    assert r.status_code == 200 and r.json()["mime"] == served


def test_unencodable_text_is_400_before_the_sidecar(client, sidecar):
    # JSON may carry a lone surrogate; it cannot be sent, so it is refused
    # up front instead of failing mid-request as an "unknown outcome".
    raw = ('{"action": "send_message", "params": {"to": "%s", "text": "bad \\ud800"},'
           ' "scope": {}}' % ALICE)
    r = client.post("/perform", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and sidecar.requests == []
    raw = ('{"action": "get_media", "params": {"chat": "%s", "message_id": "\\udfff"},'
           ' "scope": {}}' % BOB)
    r = client.post("/perform", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and sidecar.media_calls == []


@pytest.mark.parametrize("action,params", [
    ("get_chat", {}), ("get_chat", {"chat": 5}), ("read_messages", {"chat": ALICE, "limit": "5"}),
    ("read_messages", {"chat": ALICE, "limit": True}), ("search_messages", {"query": ""}),
    ("search_messages", {"query": "   "}), ("check_new_messages", {"cursor": "7"}),
    ("get_media", {"chat": BOB}), ("get_chat", {"chat": "not a jid"}),
])
def test_bad_params_are_400(perform, action, params):
    assert perform(action, params).status_code == 400


def test_unknown_action_is_404(perform):
    assert perform("delete_everything").status_code == 404


def test_resolve_and_label_for_the_owner(client):
    # Admin-side: chats first, then address-book-only entries; no visibility.
    r = client.post("/resolve", json={"kind": "chat", "query": "alice"}).json()["items"]
    assert r == [{"id": ALICE, "label": "Alice", "kind": "chat"}]
    r = client.post("/resolve", json={"kind": "chat", "query": "stern"}).json()["items"]
    assert r == [{"id": CAROL, "label": "Carol Stern", "kind": "chat"}]
    r = client.post("/resolve", json={"kind": "contact", "query": "levi"}).json()["items"]
    assert r == [{"id": BOB, "label": "Bob Levi", "kind": "contact"}]
    assert client.post("/resolve", json={"kind": "chat", "query": "  "}).json()["items"] == []
    assert client.post("/resolve", json={"kind": "planet", "query": "x"}).status_code == 400
    labels = client.post("/label", json={"kind": "chat", "ids": [GROUP, "0@s.whatsapp.net"]})
    assert labels.json() == {"labels": {GROUP: "Family"}}


def test_normalize_endpoint(client):
    assert client.post("/normalize", json={"kind": "chat", "value": "+972501111111"}).json() == {
        "id": ALICE}
    assert client.post("/normalize", json={"kind": "contact", "value": "bad"}).status_code == 400
    assert client.post("/normalize", json={"kind": "room", "value": ALICE}).status_code == 400


def test_scope_is_required_to_be_well_formed(perform):
    bad = {"visibility": {"chat": {"deny": ALICE}}}          # a string, not a list
    assert perform("list_chats", {}, bad).status_code == 400
    assert perform("list_chats", {}, scope()).status_code == 200
