"""Chats the sidecar cannot send to (status@broadcast, broadcast lists,
channels): readable and hideable like any chat, refused by send_message with
a 400 before the sidecar is ever called."""

import pytest

from .conftest import items, scope
from .fakes import ALICE, CHANNEL, STATUS, add_read_only_chats, insert_message


@pytest.fixture(autouse=True)
def _read_only_chats(archive):
    add_read_only_chats(archive.path)


def test_they_normalize_as_chats_not_contacts(client):
    assert client.post("/normalize", json={"kind": "chat", "value": STATUS}).json() == {
        "id": STATUS}
    assert client.post("/normalize", json={"kind": "chat", "value": CHANNEL}).json() == {
        "id": CHANNEL}
    # A contact is a person or group: the recipient rules.
    assert client.post("/normalize", json={"kind": "contact", "value": STATUS}).status_code == 400


def test_they_can_be_read(perform):
    assert STATUS in [c["jid"] for c in items(perform("list_chats"))]
    assert perform("get_chat", {"chat": CHANNEL}).json()["data"]["name"] == "Town News"
    assert [m["id"] for m in items(perform("read_messages", {"chat": STATUS}))] == ["S1"]
    assert [m["id"] for m in items(perform("search_messages",
                                           {"query": "road", "chat": CHANNEL}))] == ["N1"]


def test_they_can_be_hidden_everywhere(perform, archive):
    hide = scope(deny=[STATUS, CHANNEL])
    cursor = perform("check_new_messages").json()["data"]["cursor"]
    insert_message(archive.path, STATUS, "S2", "new status")
    insert_message(archive.path, ALICE, "A9", "hello")
    jids = [c["jid"] for c in items(perform("list_chats", {}, hide))]
    assert STATUS not in jids and CHANNEL not in jids
    assert items(perform("search_messages", {"query": "holiday"}, hide)) == []
    assert perform("get_chat", {"chat": STATUS}, hide).status_code == 404
    assert perform("read_messages", {"chat": CHANNEL}, hide).status_code == 404
    feed = perform("check_new_messages", {"cursor": cursor}, hide).json()["data"]["items"]
    assert [m["id"] for m in feed] == ["A9"]


@pytest.mark.parametrize("to", [STATUS, CHANNEL, "1600000000@broadcast"])
def test_send_refuses_them_before_the_sidecar(perform, sidecar, to):
    # Even with a scope that allows the chat: it is simply not a recipient.
    r = perform("send_message", {"to": to, "text": "hi"}, scope(allow_only=[to]))
    assert r.status_code == 400 and "unsupported recipient" in r.json()["error"]
    assert sidecar.requests == []
