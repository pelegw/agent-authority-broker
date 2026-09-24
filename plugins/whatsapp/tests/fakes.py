"""Test doubles for the WhatsApp plugin: a seeded archive and a scripted sidecar.

Shared by this package's tests and the broker's end-to-end test
(broker/tests/targets/conftest.py loads this file by path, the way the
runtime's tests load the broker's echo adapter), so both suites exercise the
same archive and the same sidecar behaviour.

The fake sidecar sits behind `httpx.MockTransport`, so the real
`SidecarClient` (headers, error mapping, timeouts) runs unmodified and no
socket is ever opened.
"""

import json
import sqlite3
import time
from pathlib import Path

import httpx

from aab_plugin_whatsapp.sidecar_client import SidecarClient

# Mirrors the sidecar's messages.db schema (sidecars/whatsapp/internal/store/store.go).
# If that schema changes, update this copy and the archive.py queries together.
ARCHIVE_SCHEMA = """
CREATE TABLE chats (
    jid TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    is_group INTEGER NOT NULL DEFAULT 0, last_message_ts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE contacts (
    jid TEXT PRIMARY KEY, push_name TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '', business_name TEXT NOT NULL DEFAULT '');
CREATE TABLE messages (
    chat_jid TEXT NOT NULL, id TEXT NOT NULL, sender_jid TEXT NOT NULL,
    ts INTEGER NOT NULL, is_from_me INTEGER NOT NULL, kind TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '', media_ref TEXT, PRIMARY KEY (chat_jid, id));
"""

ALICE = "972501111111@s.whatsapp.net"
BOB = "972502222222@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
CAROL = "972503333333@s.whatsapp.net"      # in the address book only, no chat yet

SIDECAR_URL = "http://whatsapp-sidecar.test:8081"
SIDECAR_TOKEN = "test-sidecar-token-0123456789"
IMAGE_BYTES = b"IMAGEBYTES"


def seed_archive(path: str | Path) -> None:
    """A message archive with two DMs and a group, as the sidecar would write
    (WA_GW's `archive` fixture, plus one address-book-only contact)."""
    conn = sqlite3.connect(str(path))
    conn.executescript(ARCHIVE_SCHEMA)
    conn.executemany("INSERT INTO chats VALUES (?, ?, ?, ?)", [
        (ALICE, "Alice", 0, 1000),
        (BOB, "Bob", 0, 3000),
        (GROUP, "Family", 1, 2000),
    ])
    conn.executemany("INSERT INTO contacts VALUES (?, ?, ?, ?)", [
        (ALICE, "Alice", "Alice Cohen", ""),
        (BOB, "Bob", "Bob Levi", ""),
        (CAROL, "Caz", "Carol Stern", ""),
    ])
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (ALICE, "A1", ALICE, 900, 0, "text", "hi, lunch tomorrow?", None),
        (ALICE, "A2", "me", 1000, 1, "text", "sure, 13:00", None),
        (BOB, "B1", BOB, 3000, 0, "image", "check this out",
         '{"media_type":"image","mime_type":"image/jpeg","direct_path":"/v/x",'
         '"media_key":"AQI=","file_sha256":"Aw==","file_enc_sha256":"BA==","file_length":9}'),
        (GROUP, "G1", ALICE, 2000, 0, "text", "who brings dessert to dinner?", None),
    ])
    conn.commit()
    conn.close()


def insert_message(path: str | Path, chat_jid: str, msg_id: str, text: str,
                   ts: int | None = None) -> None:
    """Append a message the way the sidecar would (a fresh live row)."""
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, 0, 'text', ?, NULL)",
                 (chat_jid, msg_id, chat_jid, ts or int(time.time()), text))
    conn.commit()
    conn.close()


def execute(path: str | Path, sql: str, args: tuple = ()) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(sql, args)
    conn.commit()
    conn.close()


class FakeSidecar:
    """The Go sidecar's internal API, scripted. Records every request.

    fail_next[path] = (outcome, performed)
        outcome    an int status answered with {"error": "injected"}, or an
                   httpx exception class raised as the transport failure
        performed  True: the side effect happens first (a send that went
                   out before the connection broke), False: nothing happens
    """

    def __init__(self, token: str = SIDECAR_TOKEN):
        self.token = token
        self.requests: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.media_calls: list[tuple[str, str]] = []
        self.status_body = {"connected": True, "logged_in": True,
                            "jid": "972500000000@s.whatsapp.net", "push_name": "Me",
                            "waiting_for_qr": False}
        self.qr: bytes | None = b"\x89PNG-fake-qr"
        self.media = {(BOB, "B1"): (IMAGE_BYTES, "image/jpeg")}
        self.fail_next: dict[str, tuple] = {}

    # ---- wiring --------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, token: str | None = None) -> SidecarClient:
        return SidecarClient(SIDECAR_URL, token or self.token, transport=self.transport())

    def paired(self, logged_in: bool = True, **extra) -> None:
        self.status_body = {**self.status_body, "logged_in": logged_in,
                            "connected": logged_in, "waiting_for_qr": not logged_in,
                            **({"jid": "", "push_name": ""} if not logged_in else {}),
                            **extra}

    # ---- the API ---------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if request.headers.get("X-Internal-Token") != self.token:
            return httpx.Response(401, json={"error": "missing or invalid X-Internal-Token"})
        failure = self.fail_next.pop(path, None)
        if failure is not None:
            outcome, performed = failure
            if performed:
                self._perform(request)
            if isinstance(outcome, int):
                return httpx.Response(outcome, json={"error": "injected"})
            raise outcome("injected transport failure", request=request)
        return self._perform(request)

    def _perform(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/status":
            return httpx.Response(200, json=self.status_body)
        if request.method == "GET" and path == "/qr":
            if self.status_body.get("logged_in"):
                return httpx.Response(409, json={"error": "already logged in — no QR available"})
            if self.qr is None:
                return httpx.Response(503, json={"error": "no QR code available yet, retry shortly"})
            return httpx.Response(200, content=self.qr, headers={"Content-Type": "image/png"})
        if request.method == "POST" and path == "/send":
            body = json.loads(request.content)
            if not self.status_body.get("logged_in"):
                return httpx.Response(503, json={"error": "not logged in to WhatsApp"})
            self.sent.append((body["to"], body["text"]))
            return httpx.Response(200, json={"message_id": f"MSG{len(self.sent)}",
                                             "ts": 1700000000})
        if request.method == "GET" and path == "/media":
            key = (request.url.params.get("chat_jid"), request.url.params.get("message_id"))
            self.media_calls.append(key)
            if key not in self.media:
                return httpx.Response(404, json={"error": "message not found"})
            data, mime = self.media[key]
            return httpx.Response(200, content=data, headers={"Content-Type": mime})
        return httpx.Response(404, json={"error": "no such route"})
