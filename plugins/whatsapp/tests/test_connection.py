"""The sidecar_qr connection and plugin status, directly and through the
runtime's /connect/* and /status endpoints."""

import httpx
import pytest

from aab_plugin_runtime import AdapterError
from aab_plugin_whatsapp.connection import DISCONNECT_HINT


def test_start_is_qr_until_paired(adapter, sidecar):
    sidecar.paired(False)
    assert adapter.connection.start(["whatsapp"]) == {"kind": "qr"}
    sidecar.paired(True)
    assert adapter.connection.start(["whatsapp"]) == {"kind": "none"}


def test_finish_is_a_noop(adapter, sidecar):
    assert adapter.connection.finish("code", "state", "inst") == {"ok": True}
    assert sidecar.requests == []


def test_disconnect_is_409_with_the_unlink_hint(adapter, sidecar):
    with pytest.raises(AdapterError) as e:
        adapter.connection.disconnect()
    assert e.value.status == 409 and e.value.message == DISCONNECT_HINT
    assert sidecar.requests == []                  # nothing to wipe from here


def test_mint_has_nothing_to_mint(adapter):
    assert adapter.connection.mint({"permissions": {"x": "y"}}) is None


def test_status_is_reduced_to_known_fields(adapter, sidecar):
    sidecar.status_body = {**sidecar.status_body, "fatal": "temporary ban: code=1",
                           "extra": "ignored", "logged_in": "yes"}
    st = adapter.connection.status()
    assert st == {"kind": "sidecar_qr", "connected": True, "logged_in": False,
                  "jid": "972500000000@s.whatsapp.net", "push_name": "Me",
                  "waiting_for_qr": False, "fatal": "temporary ban: code=1"}


# ---- through the runtime -----------------------------------------------------------

def test_qr_is_proxied_uncached_while_pairing(client, sidecar):
    sidecar.paired(False)
    r = client.get("/connect/qr.png")
    assert r.status_code == 200 and r.content == sidecar.qr
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"] == "image/png"


def test_qr_after_pairing_is_409_and_before_first_code_503(client, sidecar):
    assert client.get("/connect/qr.png").status_code == 409
    sidecar.paired(False)
    sidecar.qr = None
    assert client.get("/connect/qr.png").status_code == 503


def test_connect_endpoints(client, sidecar):
    sidecar.paired(False)
    assert client.post("/connect/start", json={"enabled_plugins": ["whatsapp"]}).json() == {
        "kind": "qr"}
    assert client.post("/connect/finish", json={}).json() == {"ok": True}
    r = client.post("/disconnect")
    assert r.status_code == 409 and r.json() == {"error": DISCONNECT_HINT}


@pytest.mark.parametrize("body,connected,healthy,health", [
    ({"connected": True, "logged_in": True}, True, True, "ok"),
    ({"connected": False, "logged_in": True}, True, False, "reconnecting to WhatsApp"),
    ({"connected": True, "logged_in": False, "waiting_for_qr": True}, False, False,
     "waiting for QR pairing"),
    ({"connected": False, "logged_in": False}, False, False, "not paired"),
    ({"connected": True, "logged_in": True, "fatal": "temporary ban: code=402"}, True, False,
     "fatal: temporary ban: code=402"),
])
def test_status_maps_the_sidecar(client, sidecar, body, connected, healthy, health):
    sidecar.status_body = body
    out = client.get("/status").json()
    assert (out["connected"], out["healthy"], out["health"]) == (connected, healthy, health)
    assert out["enforcement"] == "proxy" and out["archive"] == "present"
    assert out["connection"]["kind"] == "sidecar_qr"
    assert out["connection"]["logged_in"] is body["logged_in"]


def test_status_when_sidecar_unreachable_is_503(client, sidecar):
    # Linked or not is then unknown: the broker keeps its last known answer
    # instead of recording a guess.
    sidecar.fail_next["/status"] = (httpx.ConnectError, False)
    r = client.get("/status")
    assert r.status_code == 503


def test_status_reports_a_missing_archive(sidecar, tmp_path):
    from aab_plugin_whatsapp.adapter import WhatsAppAdapter
    from aab_plugin_whatsapp.archive import Archive
    a = WhatsAppAdapter(sidecar.client(), Archive(str(tmp_path / "none.db")))
    assert a.status()["archive"] == "missing"
