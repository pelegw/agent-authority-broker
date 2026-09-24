"""The Gmail adapter: every action of manifests/gmail.yaml, inside the call's scope.

Target-enforced: each call's token carries only its action's scopes (the
broker's `credential` requirements), so a read runs on `gmail.readonly` and
cannot change anything at Google, whatever this code did.

Proxy-enforced here (gmail_threads.check_thread and gmail_compose):
  * every thread (search results, get, attachment, reply target, relabel,
    archive, trash, delete) is fetched and checked BEFORE anything about it
    is returned or changed; a hidden thread is the same 404 as a missing one;
  * labels: injected into the search query as a hint AND checked on every
    returned thread; labels named in a relabel must be in the allow set;
  * contacts / domains: read visibility and recipient allow sets;
  * `date_window_days`, `attachments`, `bcc`, `mark_read`.

Rows naming a thread or label carry `resource_ref`, so the broker's own
post-filter drops anything this code ever let through by mistake. Write
results carry none: a post-filter 404 on "sent" would tell the agent the
email was not sent.
"""

import threading

from aab_plugin_runtime import AdapterError, Result

from .. import ids
from ..callscope import CallScope
from ..client import GMAIL
from . import gmail_compose as compose
from .base import GoogleAdapter, integer, strings, text
from .gmail_threads import (METADATA_HEADERS, NOT_FOUND, check_thread, decode_attachment,
                            find_part, headers, message_row, search_terms, summary_row)

READ = {"permissions": {"gmail.readonly": "read"}}
LABEL_CACHE_SECONDS = 60
# System labels an agent may not set or clear through label_thread: trash
# has its own action, the rest are Gmail's own bookkeeping.
PROTECTED_LABELS = frozenset({"TRASH", "SPAM", "DRAFT", "SENT", "CHAT"})
UNREAD = "UNREAD"


class GmailAdapter(GoogleAdapter):
    plugin_id = "gmail"
    LOOKUP = READ
    RESOURCE_KINDS = ("label", "contact", "thread")

    def __init__(self, connection, client, **kw):
        self._label_cache: tuple[float, list[dict]] | None = None
        self._label_lock = threading.Lock()
        super().__init__(connection, client, **kw)

    def handlers(self) -> dict:
        return {"search_threads": self._search_threads, "get_thread": self._get_thread,
                "list_labels": self._list_labels, "get_attachment": self._get_attachment,
                "create_draft": self._create_draft, "send": self._send,
                "label_thread": self._label_thread, "archive_thread": self._archive_thread,
                "trash_thread": self._trash_thread, "delete_thread": self._delete_thread}

    # ---- labels (shared lookups) -------------------------------------------------------

    def _labels(self, req: dict) -> list[dict]:
        with self._label_lock:
            hit = self._label_cache
            if hit and hit[0] > self.now():
                return hit[1]
        body = self.client.request("GET", f"{GMAIL}/labels", req)
        labels = [lb for lb in body.get("labels") or []
                  if isinstance(lb, dict) and isinstance(lb.get("id"), str)]
        with self._label_lock:
            self._label_cache = (self.now() + LABEL_CACHE_SECONDS, labels)
        return labels

    def _label_id(self, value: str, req: dict) -> str:
        """A label id, or a label name resolved to its id; unknown is 404."""
        v = value.strip() if isinstance(value, str) else ""
        if not v:
            raise AdapterError(400, "empty label")
        labels = self._labels(req)
        for lb in labels:
            if lb["id"] == v:
                return v
        for lb in labels:
            # System ids are uppercase words; accept them in any case.
            if lb.get("type") == "system" and lb["id"].lower() == v.lower():
                return lb["id"]
        for lb in labels:
            if str(lb.get("name", "")).casefold() == v.casefold():
                return lb["id"]
        raise AdapterError(404, NOT_FOUND)

    # ---- resources --------------------------------------------------------------------------

    def normalize(self, kind: str, value: str) -> str:
        self._kind(kind)
        if kind == "label":
            return self._label_id(value, READ)
        if kind == "contact":
            return ids.email(value)
        return ids.hex_id(value, "thread id")

    def resolve(self, kind: str, query: str, limit: int) -> list[dict]:
        self._kind(kind)
        if kind != "label":
            return []                       # contacts and threads are not resolvable
        q = (query or "").strip().casefold()
        out = [{"id": lb["id"], "label": str(lb.get("name", lb["id"])), "kind": "label"}
               for lb in self._labels(READ)
               if q in str(lb.get("name", "")).casefold() or q in lb["id"].casefold()]
        return out[:max(1, min(int(limit), 50))]

    def label(self, kind: str, id_list: list[str]) -> dict[str, str]:
        self._kind(kind)
        if kind == "contact":
            return {i: i for i in id_list}
        if kind == "label":
            names = {lb["id"]: str(lb.get("name", lb["id"])) for lb in self._labels(READ)}
            return {i: names[i] for i in id_list if i in names}
        out = {}
        for tid in id_list[:20]:
            try:
                t = self.client.request("GET", f"{GMAIL}/threads/{ids.hex_id(tid)}", READ,
                                        params={"format": "metadata",
                                                "metadataHeaders": ["Subject"]})
                msgs = t.get("messages") or []
                out[tid] = headers(msgs[0]).get("subject", "") if msgs else ""
            except AdapterError:
                continue                    # a label is cosmetic; skip what fails
        return out

    # ---- thread fetch + check ------------------------------------------------------------

    def _thread(self, raw_id, sv: CallScope, fmt: str, *, window: bool) -> tuple[str, dict, list]:
        tid = ids.hex_id(raw_id, "thread id")
        if not sv.vis("thread").admits(tid):
            raise AdapterError(404, NOT_FOUND)      # never ask Google about a hidden id
        params = {"format": fmt}
        if fmt == "metadata":
            params["metadataHeaders"] = METADATA_HEADERS
        thread = self.client.request("GET", f"{GMAIL}/threads/{tid}", sv.requirements,
                                     params=params)
        messages = check_thread(thread, sv, self.now(), window=window)
        return tid, thread, messages

    # ---- reads -------------------------------------------------------------------------------

    def _search_threads(self, params: dict, sv: CallScope) -> Result:
        query = text(params, "query", default="") or ""
        limit = integer(params, "limit", 20, 1, 50)
        lv, cv = sv.vis("label"), sv.vis("contact")
        if lv.allow == frozenset() or cv.allow == frozenset():
            return Result(data={"items": [], "next_page_token": None})   # nothing allowed
        req = sv.requirements
        names = {lb["id"]: str(lb.get("name", "")) for lb in self._labels(req)} \
            if lv.restricted else {}
        terms = search_terms(names, lv, cv, sv.bound("date_window_days"))
        # The agent's query is grouped in parentheses; the injected terms only
        # narrow what Google returns; check_thread below is the enforcement.
        q = " ".join(([f"({query})"] if query.strip() else []) + terms)
        listing = self.client.request("GET", f"{GMAIL}/threads", req, params={
            "q": q or None, "maxResults": limit, "pageToken": text(params, "page_token")})
        items = []
        for entry in listing.get("threads") or []:
            tid = entry.get("id") if isinstance(entry, dict) else None
            if not isinstance(tid, str):
                continue
            try:
                _, thread, messages = self._thread(tid, sv, "metadata", window=True)
            except AdapterError as exc:
                if exc.status in (400, 404):
                    continue                # hidden, outside the grant, gone, or malformed
                raise
            items.append(summary_row(thread, messages))
        return Result(data={"items": items, "next_page_token": listing.get("nextPageToken")})

    def _get_thread(self, params: dict, sv: CallScope) -> Result:
        tid, thread, messages = self._thread(params.get("thread_id"), sv, "full", window=True)
        rows = [message_row(m, sv.flag("attachments")) for m in messages]
        return Result(data={"id": tid, "subject": rows[0]["subject"], "messages": rows,
                            "resource_ref": {"kind": "thread", "id": tid}})

    def _list_labels(self, params: dict, sv: CallScope) -> Result:
        lv = sv.vis("label")
        rows = [{"id": lb["id"], "name": str(lb.get("name", "")), "type": lb.get("type", ""),
                 "resource_ref": {"kind": "label", "id": lb["id"]}}
                for lb in self._labels(sv.requirements) if lv.admits(lb["id"])]
        return Result(data={"items": rows})

    def _get_attachment(self, params: dict, sv: CallScope) -> Result:
        if not sv.flag("attachments"):
            raise AdapterError(403, "attachments are not allowed by your grant")
        mid = ids.hex_id(params.get("message_id"), "message id")
        pid = ids.part_id(params.get("part_id"))
        tid, _, messages = self._thread(params.get("thread_id"), sv, "metadata", window=True)
        if mid not in {str(m.get("id", "")).lower() for m in messages}:
            raise AdapterError(404, NOT_FOUND)
        req = sv.requirements
        msg = self.client.request("GET", f"{GMAIL}/messages/{mid}", req,
                                  params={"format": "full"})
        if str(msg.get("threadId", "")).lower() != tid:
            raise AdapterError(404, NOT_FOUND)
        part = find_part(msg.get("payload") or {}, pid)
        if part is None:
            raise AdapterError(404, NOT_FOUND)
        body = part.get("body") or {}
        if isinstance(body.get("data"), str):
            data = decode_attachment(body["data"])
        else:
            att = self.client.request(
                "GET", f"{GMAIL}/messages/{mid}/attachments/{body.get('attachmentId')}", req)
            data = decode_attachment(att.get("data"))
        return Result(binary=data, mime=ids.safe_mime(part.get("mimeType")))

    # ---- writes ------------------------------------------------------------------------------

    def _outgoing(self, params: dict, sv: CallScope) -> dict:
        """Recipient checks, the optional reply target's check, the raw message."""
        to, cc, bcc = compose.recipients(params, sv)
        reply, tid = None, None
        if params.get("thread_id") is not None:
            # Replies go only into a thread this key can see (labels, contacts,
            # hidden); the date window is a read limit and does not apply.
            tid, _, messages = self._thread(params["thread_id"], sv, "metadata", window=False)
            reply = compose.reply_headers(messages)
        message = {"raw": compose.build_raw(params, to, cc, bcc, reply)}
        if tid:
            message["threadId"] = tid
        return message

    def _create_draft(self, params: dict, sv: CallScope) -> Result:
        message = self._outgoing(params, sv)
        out = self.client.request("POST", f"{GMAIL}/drafts", sv.requirements,
                                  json={"message": message})
        msg = out.get("message") or {}
        return Result(data={"status": "drafted", "draft_id": out.get("id"),
                            "message_id": msg.get("id"), "thread_id": msg.get("threadId")})

    def _send(self, params: dict, sv: CallScope) -> Result:
        message = self._outgoing(params, sv)
        out = self.client.request("POST", f"{GMAIL}/messages/send", sv.requirements,
                                  json=message)
        return Result(data={"status": "sent", "message_id": out.get("id"),
                            "thread_id": out.get("threadId")})

    def _label_thread(self, params: dict, sv: CallScope) -> Result:
        tid, _, _ = self._thread(params.get("thread_id"), sv, "metadata", window=True)
        req = sv.requirements
        add = [self._label_id(v, req) for v in strings(params, "add", 50)]
        remove = [self._label_id(v, req) for v in strings(params, "remove", 50)]
        if not add and not remove:
            raise AdapterError(400, "name at least one label to add or remove")
        lv = sv.vis("label")
        for lid in dict.fromkeys(add + remove):
            if lid == UNREAD:
                # Read state is governed by mark_read, not by the label set.
                if not sv.flag("mark_read"):
                    raise AdapterError(403, "changing read state is not allowed by your grant")
            elif lid in PROTECTED_LABELS:
                raise AdapterError(400, f"{lid} cannot be set with label_thread")
            else:
                lv.check_named(lid, f"label {lid}")
        self.client.request("POST", f"{GMAIL}/threads/{tid}/modify", req,
                            json={"addLabelIds": add, "removeLabelIds": remove})
        return Result(data={"status": "labeled", "thread_id": tid})

    def _archive_thread(self, params: dict, sv: CallScope) -> Result:
        tid, _, _ = self._thread(params.get("thread_id"), sv, "metadata", window=True)
        self.client.request("POST", f"{GMAIL}/threads/{tid}/modify", sv.requirements,
                            json={"addLabelIds": [], "removeLabelIds": ["INBOX"]})
        return Result(data={"status": "archived", "thread_id": tid})

    def _trash_thread(self, params: dict, sv: CallScope) -> Result:
        tid, _, _ = self._thread(params.get("thread_id"), sv, "metadata", window=True)
        self.client.request("POST", f"{GMAIL}/threads/{tid}/trash", sv.requirements)
        return Result(data={"status": "trashed", "thread_id": tid})

    def _delete_thread(self, params: dict, sv: CallScope) -> Result:
        tid, _, _ = self._thread(params.get("thread_id"), sv, "metadata", window=True)
        self.client.request("DELETE", f"{GMAIL}/threads/{tid}", sv.requirements)
        return Result(data={"status": "deleted", "thread_id": tid})
