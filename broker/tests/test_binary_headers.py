"""Binary results over REST are always a nosniff attachment with a safe,
manifest-derived filename: target content was chosen by a third party (a
WhatsApp attachment's sender, say) and must never be sniffed into HTML or
rendered inline on the broker's origin. (The WhatsApp get_media variant is in
tests/targets/test_whatsapp.py.)"""

import pytest

from broker import engine
from broker.plugins.registry import Registry

from .conftest import cap, echo_manifest

ACT = "/v1/targets/echo/actions"


def test_binary_is_a_nosniff_attachment(client, echo, make_agent):
    a = make_agent([cap(["get_blob"])])
    r = client.post(f"{ACT}/get_blob", json={"params": {"item_id": "i1"}}, headers=a.headers)
    assert r.status_code == 200 and r.content == b"blob:i1"
    assert r.headers["x-content-type-options"] == "nosniff"
    # echo's get_blob has only its selector: the filename is the resource id.
    assert r.headers["content-disposition"] == 'attachment; filename="i1"'


def test_json_results_are_unchanged(client, echo_local, make_agent):
    a = make_agent([cap(["get_item"])])
    r = client.post(f"{ACT}/get_item", json={"params": {"item_id": "i1"}}, headers=a.headers)
    assert "content-disposition" not in r.headers


def test_the_most_specific_id_names_the_download():
    media = Registry().vendored("whatsapp").action("get_media")
    # The first required string param that is not the selector: message_id.
    assert engine.download_name(media, {"chat": "972501111111@s.whatsapp.net",
                                        "message_id": "3EB0C0FFEE"}, "x") == "3EB0C0FFEE"
    blob = echo_manifest().action("get_blob")
    assert engine.download_name(blob, {"item_id": "i1"}, "i1") == "i1"
    assert engine.download_name(blob, {}, "") == "get_blob"


@pytest.mark.parametrize("hostile,expected", [
    ('a"; filename=evil.html', "a___filename_evil.html"),
    ("x\r\nSet-Cookie: s=1", "x__Set-Cookie__s_1"),
    ("../../etc/passwd", "etc_passwd"),
    ("...", "get_media"),
    ("ü" * 300, "get_media"),                      # nothing usable left: the action
])
def test_the_filename_is_a_safe_token(hostile, expected):
    media = Registry().vendored("whatsapp").action("get_media")
    name = engine.download_name(media, {"chat": "c", "message_id": hostile}, "c")
    assert name == expected
    assert not set(name) & set('"\\\r\n;/ ')
