"""The `sidecar_qr` connection: WhatsApp pairing and link status via the sidecar.

The credential is the sidecar's whatsmeow session (`session.db` in the
`wa_data` volume, which this container mounts read-only). So this
connection owns nothing itself: it relays the pairing QR, reports the
sidecar's link state, and mints nothing (there is no token to narrow;
WhatsApp enforcement is proxy-only).

  start(enabled)  {"kind": "qr"} while not paired, {"kind": "none"} once paired
  qr_png()        the sidecar's current QR (409 once paired, 503 until the
                  first code exists); the runtime serves it with no-store
  finish(...)     no-op {"ok": true}: pairing completes on the phone. The
                  console calls it after a scan so the broker refreshes health
  disconnect()    409: the session cannot be wiped from here (read-only
                  volume, and by design). Unlink the device on the phone; the
                  sidecar sees LoggedOut, clears its session, exits, and
                  Docker restarts it into a fresh QR flow
  status()        the sidecar's /status, reduced to known fields
"""

from typing import Any

from aab_plugin_runtime import AdapterError

from .sidecar_client import SidecarClient

DISCONNECT_HINT = ("unlink from the phone (Linked devices); the sidecar exits and re-pairs")


class SidecarQRConnection:
    kind = "sidecar_qr"

    def __init__(self, sidecar: SidecarClient):
        self.sidecar = sidecar

    def start(self, enabled_plugins: list[str]) -> dict:
        return {"kind": "none"} if self.status()["logged_in"] else {"kind": "qr"}

    def finish(self, code: str | None, state: str | None,
               installation_id: str | None) -> dict:
        # Nothing to exchange: a QR pairing has no code/state. Ignored on purpose.
        return {"ok": True}

    def qr_png(self) -> bytes:
        return self.sidecar.qr_png()

    def disconnect(self) -> dict:
        raise AdapterError(409, DISCONNECT_HINT)

    def status(self) -> dict:
        """Raises SidecarError(503) when the sidecar is unreachable: whether the
        session is still linked is then unknown, and the broker keeps its last
        known answer rather than recording a guess."""
        raw = self.sidecar.status()
        return {
            "kind": self.kind,
            "connected": raw.get("connected") is True,
            "logged_in": raw.get("logged_in") is True,
            "jid": _text(raw.get("jid")),
            "push_name": _text(raw.get("push_name")),
            "waiting_for_qr": raw.get("waiting_for_qr") is True,
            "fatal": _text(raw.get("fatal")),
        }

    def mint(self, requirements: dict) -> Any:
        return None            # proxy-only: there is no narrower credential to mint


def _text(value: Any) -> str:
    return value[:200] if isinstance(value, str) else ""
