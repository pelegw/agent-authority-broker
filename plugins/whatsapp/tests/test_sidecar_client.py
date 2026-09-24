"""The sidecar client: token header, the 503/502 transport contract, how
sidecar HTTP answers map onto it, and that the token never leaks."""

import json

import httpx
import pytest

from aab_plugin_whatsapp.sidecar_client import MAX_SEND_BODY_BYTES, SidecarClient, SidecarError

from .fakes import ALICE, BOB, IMAGE_BYTES, SIDECAR_TOKEN, SIDECAR_URL, FakeSidecar


def _answer(status, body=None, content=None, headers=None):
    def handler(request):
        if content is not None:
            return httpx.Response(status, content=content, headers=headers or {})
        return httpx.Response(status, json=body if body is not None else {"error": "boom"})
    return SidecarClient(SIDECAR_URL, SIDECAR_TOKEN, transport=httpx.MockTransport(handler))


def test_every_call_carries_the_internal_token(sidecar):
    c = sidecar.client()
    c.status()
    c.send_text(ALICE, "hi")
    assert sidecar.sent == [(ALICE, "hi")]
    wrong = sidecar.client(token="not-the-token")
    with pytest.raises(SidecarError) as e:
        wrong.status()
    # Refused before any handler ran: not performed, and never shown as if
    # the calling agent were unauthorized.
    assert e.value.status == 503


def test_media_returns_bytes_and_type(sidecar):
    assert sidecar.client().media(BOB, "B1") == (IMAGE_BYTES, "image/jpeg")
    assert sidecar.media_calls == [(BOB, "B1")]


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout])
def test_never_reached_is_503(sidecar, exc):
    sidecar.fail_next["/send"] = (exc, False)
    with pytest.raises(SidecarError) as e:
        sidecar.client().send_text(ALICE, "x")
    assert e.value.status == 503 and sidecar.sent == []


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.RemoteProtocolError,
                                 httpx.ReadError, httpx.WriteTimeout])
def test_on_the_wire_is_502(sidecar, exc):
    # The send went out, then the connection broke: the outcome is unknown.
    sidecar.fail_next["/send"] = (exc, True)
    with pytest.raises(SidecarError) as e:
        sidecar.client().send_text(ALICE, "x")
    assert e.value.status == 502 and sidecar.sent == [(ALICE, "x")]


@pytest.mark.parametrize("upstream,expected", [
    (400, 400), (404, 404), (409, 409),          # 4xx pass through
    (503, 503), (502, 502),                      # the contract statuses pass through
    (401, 503), (403, 503),                      # token refused: not performed
    (500, 502), (504, 502),                      # other 5xx: unknown outcome
    (302, 502), (307, 502),                      # never follow, never trust
])
def test_http_answers_map_onto_the_contract(upstream, expected):
    with pytest.raises(SidecarError) as e:
        _answer(upstream, headers={"Location": "http://elsewhere.test/"}).status()
    assert e.value.status == expected


def test_redirects_are_not_followed():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "elsewhere.test":
            return httpx.Response(200, json={"message_id": "stolen"})
        return httpx.Response(307, headers={"Location": "http://elsewhere.test/send"})

    c = SidecarClient(SIDECAR_URL, SIDECAR_TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(SidecarError):
        c.send_text(ALICE, "x")
    assert seen == [f"{SIDECAR_URL}/send"]       # the token never went elsewhere


def test_proxy_env_is_ignored(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.evil.test:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.evil.test:3128")
    c = SidecarClient(SIDECAR_URL, SIDECAR_TOKEN)
    with c._client() as client:
        assert client.trust_env is False


def test_malformed_success_is_unknown_outcome():
    with pytest.raises(SidecarError) as e:
        _answer(200, content=b"not json").send_text(ALICE, "x")
    assert e.value.status == 502
    with pytest.raises(SidecarError) as e:
        _answer(200, body={"ok": True}).send_text(ALICE, "x")
    assert e.value.status == 502
    with pytest.raises(SidecarError) as e:
        _answer(200, body=["x"]).status()
    assert e.value.status == 502


def test_error_text_comes_from_the_sidecar_body():
    with pytest.raises(SidecarError) as e:
        _answer(503, {"error": "not logged in to WhatsApp"}).send_text(ALICE, "x")
    assert e.value.message == "not logged in to WhatsApp"


def test_oversized_message_is_refused_before_the_network(sidecar):
    text = "é" * (MAX_SEND_BODY_BYTES // 2)            # 2 bytes each in UTF-8
    with pytest.raises(SidecarError) as e:
        sidecar.client().send_text(ALICE, text)
    assert e.value.status == 400 and sidecar.requests == []
    fits = "x" * (MAX_SEND_BODY_BYTES - 200)
    sidecar.client().send_text(ALICE, fits)
    assert sidecar.sent == [(ALICE, fits)]


def test_send_body_is_plain_json(sidecar):
    got = []

    def handler(request):
        got.append((request.headers["content-type"], json.loads(request.content)))
        return httpx.Response(200, json={"message_id": "M1", "ts": 1})

    c = SidecarClient(SIDECAR_URL, SIDECAR_TOKEN, transport=httpx.MockTransport(handler))
    assert c.send_text(ALICE, "שלום") == {"message_id": "M1", "ts": 1}
    assert got == [("application/json", {"to": ALICE, "text": "שלום"})]


def test_token_never_leaks(sidecar):
    c = sidecar.client()
    assert SIDECAR_TOKEN not in repr(c)
    for exc in (httpx.ConnectError, httpx.ReadTimeout):
        sidecar.fail_next["/status"] = (exc, False)
        with pytest.raises(SidecarError) as e:
            c.status()
        assert SIDECAR_TOKEN not in e.value.message and SIDECAR_TOKEN not in str(e.value)


@pytest.mark.parametrize("url,token", [(SIDECAR_URL, ""), (SIDECAR_URL, "   "),
                                       ("whatsapp-sidecar:8081", SIDECAR_TOKEN),
                                       ("file:///etc/passwd", SIDECAR_TOKEN)])
def test_unsafe_configuration_refuses_at_boot(url, token):
    with pytest.raises(ValueError):
        SidecarClient(url, token)


def test_the_fake_is_the_real_contract():
    # Sanity: the fake answers 401 without the token, like the Go middleware.
    fake = FakeSidecar()
    r = fake.handle(httpx.Request("GET", f"{SIDECAR_URL}/status"))
    assert r.status_code == 401
