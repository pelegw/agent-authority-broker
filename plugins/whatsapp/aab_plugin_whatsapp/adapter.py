"""The WhatsApp plugin adapter: every manifest action, inside the broker's scope.

Served by `aab_plugin_runtime` in the plugin-whatsapp container; the broker
reaches it over the plugin API and never touches the sidecar or the archive
itself. Reads come from the sidecar's archive (`archive.py`, SQL-level
filtering), sends and media go through the sidecar (`sidecar_client.py`).
The read paths are ported from WA_GW `services.py`.

What this module guarantees, whatever the broker already checked:
  * Visibility is applied INSIDE every archive query: `scope["visibility"]
    ["chat"]` becomes the (deny, allow_only) pair of WA_GW's
    `visible_filter`, so a hidden chat never shows up in a list, a search
    or the new-messages feed, and pagination stays honest.
  * Hidden == missing: a get, read, chat-scoped search or media download of
    a hidden chat is the same 404 as a chat that does not exist.
  * Media: the chat's visibility is checked BEFORE the sidecar is called, so
    a hidden chat's media never leaves the sidecar at all. Its media type is
    sender-controlled, so active types (HTML, SVG, ...) are returned as
    opaque bytes (`safe_mime`).
  * Every chat and message row carries `resource_ref: {"kind": "chat", "id":
    jid}` (contacts: kind "contact"), so the broker's post-filter can drop
    anything this code ever let through by mistake.
  * Ids are normalized (`jid.py`) before any comparison or send. Chats the
    sidecar cannot send to (status@broadcast, broadcast lists, channels) are
    readable and hideable, but send_message refuses them with a 400; a send
    to a chat outside the scope is refused before the sidecar sees it.
  * 503 = not performed, 502 = outcome unknown (see sidecar_client.py). Every
    archive read in a handler happens before its sidecar call, so an archive
    failure is always a 503.

`resolve` and `label` apply no visibility: they serve the owner's console
and approval cards, and the broker filters `resolve` for agents itself.
"""

import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from aab_plugin_runtime import AdapterError, Result

from .archive import Archive
from .connection import SidecarQRConnection
from .jid import normalize_jid, normalize_recipient
from .scope import is_visible, visibility
from .sidecar_client import SidecarClient

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

MAX_LIMIT = 200                  # the largest `limit` any manifest action allows
CONTACT_LIMIT = 50               # search_contacts takes no limit (WA_GW's default)
RESOLVE_LIMIT = 50               # picker results, as WA_GW resolve_chats
# History sync re-ingests old messages with new rowids; only messages this
# fresh count as "new" (WA_GW events_freshness_seconds).
EVENTS_FRESHNESS_SECONDS = 300
NOT_FOUND = "no such chat"       # one message for missing AND hidden
RESOURCE_KINDS = ("chat", "contact")


class WhatsAppAdapter:
    """The `aab_plugin_runtime` PluginAdapter for plugin id `whatsapp`."""

    def __init__(self, sidecar: SidecarClient, archive: Archive, *,
                 manifest_path: Path = MANIFEST_PATH,
                 freshness_seconds: int = EVENTS_FRESHNESS_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.manifest = yaml.safe_load(Path(manifest_path).read_text(encoding="utf-8"))
        self.sidecar = sidecar
        self.archive = archive
        self.connection = SidecarQRConnection(sidecar)
        self._freshness = freshness_seconds
        self._clock = clock
        self._actions: dict[str, Callable[[dict, dict], Result]] = {
            "list_chats": self._list_chats,
            "get_chat": self._get_chat,
            "read_messages": self._read_messages,
            "search_messages": self._search_messages,
            "check_new_messages": self._check_new_messages,
            "search_contacts": self._search_contacts,
            "get_media": self._get_media,
            "send_message": self._send_message,
        }
        # The manifest is the contract the broker enforces against; an action
        # it declares that this code does not implement (or the reverse) is a
        # packaging bug, so the container refuses to start.
        declared = {a["name"] for a in self.manifest.get("actions", [])}
        if declared != set(self._actions):
            raise RuntimeError(f"manifest/adapter action mismatch: "
                               f"{sorted(declared ^ set(self._actions))}")

    # ---- lifecycle -------------------------------------------------------------

    def configure(self, config: dict, secrets) -> None:
        """Nothing is configurable over the network, on purpose: the sidecar
        URL, its token and the archive path come from this container's env.
        Ignoring `config` means no console session (or anything that reaches
        /configure) can point the plugin at another sidecar or database."""

    def status(self) -> dict:
        """Raises SidecarError(503) when the sidecar is unreachable (the broker
        then keeps its last known `connected`). `connected` means paired: the
        archive stays readable while WhatsApp itself reconnects."""
        link = self.connection.status()
        logged_in = link["logged_in"]
        if link["fatal"]:
            health = f"fatal: {link['fatal']}"
        elif not logged_in:
            health = "waiting for QR pairing" if link["waiting_for_qr"] else "not paired"
        elif not link["connected"]:
            health = "reconnecting to WhatsApp"
        else:
            health = "ok"
        return {"connected": logged_in, "healthy": health == "ok", "health": health,
                "enforcement": "proxy",
                "archive": "present" if self.archive.exists() else "missing"}

    # ---- resources ---------------------------------------------------------------

    def normalize(self, kind: str, value: str) -> str:
        """A chat may be read-only (broadcast, channel); a contact is a person
        or group, so it takes the recipient rules."""
        _kind(kind)
        return normalize_jid(value) if kind == "chat" else normalize_recipient(value)

    def resolve(self, kind: str, query: str, limit: int) -> list[dict]:
        """Ported from WA_GW admin_services.resolve_chats: chats first, then
        address-book entries not already listed (a contact's JID is also the
        id of the 1:1 chat with them)."""
        _kind(kind)
        q = (query or "").strip()
        if not q:
            return []
        limit = max(1, min(_as_int(limit, "limit"), RESOLVE_LIMIT))
        out, seen = [], set()
        with _archive_errors():
            if kind == "chat":
                for c in self.archive.list_chats(q, limit):
                    out.append({"id": c["jid"], "label": c["name"] or c["jid"], "kind": kind})
                    seen.add(c["jid"])
            for c in self.archive.list_contacts(q, limit):
                if c["jid"] in seen:
                    continue
                name = c["full_name"] or c["push_name"] or c["business_name"]
                out.append({"id": c["jid"], "label": name or c["jid"], "kind": kind})
        return out[:limit]

    def label(self, kind: str, ids: list[str]) -> dict[str, str]:
        _kind(kind)
        with _archive_errors():
            return self.archive.names(list(ids))

    # ---- actions -------------------------------------------------------------------

    def perform(self, action: str, params: dict, scope: dict) -> Result:
        handler = self._actions.get(action)
        if handler is None:
            raise AdapterError(404, f"unknown action {action!r}")
        if not isinstance(params, dict):
            raise AdapterError(400, "params must be an object")
        with _archive_errors():
            return handler(params, scope)

    def _chat_scope(self, scope: dict) -> tuple[list[str], list[str] | None]:
        """(deny, allow_only) for chats. One method so tests can make the
        adapter 'leak' and prove the broker's post-filter still holds."""
        return visibility(scope, "chat")

    def _require_visible_chat(self, jid: str, deny, allow_only) -> None:
        # A hidden chat is indistinguishable from a nonexistent one (same 404).
        if not self.archive.chat_is_visible(jid, deny, allow_only):
            raise AdapterError(404, NOT_FOUND)

    def _list_chats(self, params: dict, scope: dict) -> Result:
        query = _str(params, "query", default="")
        deny, allow_only = self._chat_scope(scope)
        rows = self.archive.list_chats(query, _limit(params, 20), 0, deny, allow_only)
        return Result(data={"items": [_chat(r) for r in rows]})

    def _get_chat(self, params: dict, scope: dict) -> Result:
        jid = normalize_jid(_str(params, "chat", required=True))
        deny, allow_only = self._chat_scope(scope)
        chat = self.archive.get_chat(jid, deny, allow_only)
        if chat is None:
            raise AdapterError(404, NOT_FOUND)
        return Result(data=_chat(chat))

    def _read_messages(self, params: dict, scope: dict) -> Result:
        jid = normalize_jid(_str(params, "chat", required=True))
        limit = _limit(params, 30)
        before = _int(params, "before")
        before_id = _str(params, "before_id", default="")
        deny, allow_only = self._chat_scope(scope)
        # WA_GW answered [] for a hidden chat's messages; hidden == missing
        # here means the same 404 a nonexistent chat gets.
        self._require_visible_chat(jid, deny, allow_only)
        rows = self.archive.list_messages(jid, limit, before, None, before_id or None, None,
                                          deny, allow_only)
        return Result(data={"items": [_message(r) for r in rows]})

    def _search_messages(self, params: dict, scope: dict) -> Result:
        query = _str(params, "query", required=True)
        chat = _str(params, "chat")
        deny, allow_only = self._chat_scope(scope)
        jid = None
        if chat:
            jid = normalize_jid(chat)
            self._require_visible_chat(jid, deny, allow_only)
        # Without a chat the search spans the archive: the visibility clause
        # is what keeps hidden chats out (the bypass most likely to be missed).
        rows = self.archive.search_messages(query, jid, _limit(params, 20), deny, allow_only)
        return Result(data={"items": [_message(r) for r in rows]})

    def _check_new_messages(self, params: dict, scope: dict) -> Result:
        """New messages since `cursor` (a rowid from a previous call).

        No cursor bootstraps: the current top of the archive and no backlog,
        so an agent starts "from now" (WA_GW services.list_events)."""
        cursor = _int(params, "cursor")
        limit = _limit(params, 50)
        if cursor is None:
            return Result(data={"cursor": self.archive.max_message_rowid(), "items": []})
        deny, allow_only = self._chat_scope(scope)
        cutoff = int(self._clock()) - self._freshness
        events, next_cursor = self.archive.list_events(max(0, cursor), limit, cutoff,
                                                       deny, allow_only)
        return Result(data={"cursor": next_cursor, "items": [_message(e) for e in events]})

    def _search_contacts(self, params: dict, scope: dict) -> Result:
        # Hiding a CHAT does not hide the CONTACT (WA_GW's decision: contacts
        # are the name -> JID resolver). A key's own contact denies do apply.
        query = _str(params, "query", required=True)
        deny, allow_only = visibility(scope, "contact")
        rows = self.archive.list_contacts(query, CONTACT_LIMIT, deny, allow_only)
        return Result(data={"items": [
            {**r, "resource_ref": {"kind": "contact", "id": r["jid"]}} for r in rows]})

    def _get_media(self, params: dict, scope: dict) -> Result:
        jid = normalize_jid(_str(params, "chat", required=True))
        message_id = _str(params, "message_id", required=True)
        deny, allow_only = self._chat_scope(scope)
        # Visibility check BEFORE the sidecar call: a hidden chat's media must
        # never leave the sidecar, not merely be withheld from the response.
        self._require_visible_chat(jid, deny, allow_only)
        data, mime = self.sidecar.media(jid, message_id)
        return Result(binary=data, mime=safe_mime(mime))

    def _send_message(self, params: dict, scope: dict) -> Result:
        # Recipient rules, not chat rules: a status/broadcast/channel chat is
        # a valid chat id but the sidecar cannot send to it (400, not a 502
        # from the sidecar after the fact).
        to = normalize_recipient(_str(params, "to", required=True))
        text = _str(params, "text", required=True, strip=False)
        deny, allow_only = self._chat_scope(scope)
        # The recipient may be a chat the archive has never seen, so this is
        # an id check, not an archive lookup. Hidden or out of scope: 404,
        # and the sidecar is never called.
        if not is_visible(to, deny, allow_only):
            raise AdapterError(404, NOT_FOUND)
        res = self.sidecar.send_text(to, text)
        # No resource_ref on purpose: the message has been sent, and a
        # post-filter 404 on the result would tell the agent it was not.
        return Result(data={"status": "sent", **res})


# ---- helpers ---------------------------------------------------------------------

@contextmanager
def _archive_errors():
    """Map SQLite failures (locked, mid-creation, unreadable) to 503: every
    handler reads the archive before it calls the sidecar, so nothing has
    been performed yet."""
    try:
        yield
    except sqlite3.Error as exc:
        raise AdapterError(503, "message archive temporarily unavailable") from exc


# A media type is whatever the SENDER of a WhatsApp document claimed. Types a
# browser would execute or render as a page are served as opaque bytes, so
# attacker-supplied HTML/SVG can never be delivered as such from the broker's
# origin; anything malformed is opaque too.
_MIME_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}")
_ACTIVE_MIME = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml",
    "text/javascript", "application/javascript", "application/x-javascript",
    "application/ecmascript", "text/ecmascript", "text/xsl", "application/xslt+xml",
    "multipart/x-mixed-replace",
})
OPAQUE_MIME = "application/octet-stream"


def safe_mime(mime: str | None) -> str:
    base = (mime or "").split(";", 1)[0].strip().lower()
    if not _MIME_RE.fullmatch(base) or base in _ACTIVE_MIME:
        return OPAQUE_MIME
    return base


def _kind(kind: str) -> None:
    if kind not in RESOURCE_KINDS:
        raise AdapterError(400, f"unknown resource kind {kind!r}")


def _chat(row: dict) -> dict:
    return {**row, "resource_ref": {"kind": "chat", "id": row["jid"]}}


def _message(row: dict) -> dict:
    return {**row, "resource_ref": {"kind": "chat", "id": row["chat_jid"]}}


def _str(params: dict, name: str, *, required: bool = False, default: str | None = None,
         strip: bool = True) -> str | None:
    value = params.get(name)
    if value is None:
        if required:
            raise AdapterError(400, f"{name} is required")
        return default
    if not isinstance(value, str):
        raise AdapterError(400, f"{name} must be a string")
    try:
        # JSON can carry lone surrogates; they cannot be encoded to send, and
        # failing later (mid-request) would misreport "not sent" as unknown.
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise AdapterError(400, f"{name} is not valid text") from None
    if required and not (value.strip() if strip else value):
        raise AdapterError(400, f"{name} must not be empty")
    return value


def _as_int(value: Any, name: str) -> int:
    # bool is an int subclass; True must not become a limit of 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdapterError(400, f"{name} must be an integer")
    return value


def _int(params: dict, name: str) -> int | None:
    value = params.get(name)
    return None if value is None else _as_int(value, name)


def _limit(params: dict, default: int) -> int:
    """Clamp into [1, MAX_LIMIT]: SQLite treats LIMIT -1 as 'unlimited', so a
    negative value must never reach a query (WA_GW services._clamp)."""
    value = _int(params, "limit")
    return max(1, min(default if value is None else value, MAX_LIMIT))
