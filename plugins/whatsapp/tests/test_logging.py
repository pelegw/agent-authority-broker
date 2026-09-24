"""plugin-whatsapp's log lines: one per sidecar call (method, path, status,
duration; never a message text, a recipient or /media's query), and the
broker's request id forwarded to the sidecar so its request line carries it
too."""

import logging

import httpx

from aab_plugin_runtime.logging_setup import bind
from aab_plugin_whatsapp.sidecar_client import SidecarClient

SECRET_TEXT = "a private message body 7f3a"
RECIPIENT = "+972501111111"


def sidecar_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "aab_plugin_whatsapp.sidecar"]


def test_a_send_logs_the_call_but_not_the_text_or_the_recipient(perform, caplog):
    caplog.set_level(logging.DEBUG)
    r = perform("send_message", {"to": RECIPIENT, "text": SECRET_TEXT})
    assert r.status_code == 200, r.text
    sends = [line for line in sidecar_lines(caplog) if "path=/send" in line]
    assert len(sends) == 1 and sends[0].startswith("sidecar call method=POST path=/send "
                                                   "status=200 error=- duration_ms=")
    assert SECRET_TEXT not in caplog.text and "972501111111" not in caplog.text


def test_the_request_id_reaches_the_sidecar(caplog):
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-request-id"))
        return httpx.Response(200, json={"connected": True, "logged_in": True})

    client = SidecarClient("http://whatsapp-sidecar:8081", "sidecar-token-0123",
                           transport=httpx.MockTransport(handle))
    with bind("broker-rid-wa"):
        client.status()
    client.status()                           # outside a request: no header at all
    assert seen == ["broker-rid-wa", None]


def test_media_is_logged_without_its_query(caplog):
    caplog.set_level(logging.INFO)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"bytes", headers={"content-type": "image/jpeg"})

    client = SidecarClient("http://whatsapp-sidecar:8081", "sidecar-token-0123",
                           transport=httpx.MockTransport(handle))
    client.media("120363025246125244@g.us", "3EB0MEDIA")
    [line] = sidecar_lines(caplog)
    assert line.startswith("sidecar call method=GET path=/media status=200")
    assert "120363025246125244" not in caplog.text and "3EB0MEDIA" not in caplog.text


def test_an_unreachable_sidecar_is_a_warning_with_the_error_class(caplog):
    caplog.set_level(logging.INFO)

    def handle(request):
        raise httpx.ConnectError("refused", request=request)

    client = SidecarClient("http://whatsapp-sidecar:8081", "sidecar-token-0123",
                           transport=httpx.MockTransport(handle))
    try:
        client.status()
    except Exception:
        pass
    [record] = [r for r in caplog.records if r.name == "aab_plugin_whatsapp.sidecar"]
    assert record.levelno == logging.WARNING
    assert "status=- error=ConnectError" in record.getMessage()
    assert "sidecar-token-0123" not in caplog.text
