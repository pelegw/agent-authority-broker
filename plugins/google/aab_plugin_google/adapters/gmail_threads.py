"""Gmail threads: who may see one, the search terms that approximate that,
and the compact shape an agent gets back.

`check_thread` is the enforcement. Every thread the Gmail API returns goes
through it before anything about it leaves the plugin, whatever query
produced it; `search_terms` only narrows what Google sends back (fewer
wasted fetches, honest pages), and is never trusted on its own: an agent's
query text sits next to the injected terms and could try to re-group them.

A thread is visible iff all of these hold (deny always wins):
  * its id is not hidden or denied;
  * none of its labels (the union over its messages) is hidden or denied,
    and when the capability names labels, it carries at least one of them;
  * none of its participants (From/To/Cc of any message) is a denied
    contact, and when the capability names contacts, one of them takes part;
  * under `date_window_days`, at least one message is inside the window;
    only those messages are shown.
"""

import base64
import html
import re
from email.utils import getaddresses

from aab_plugin_runtime import AdapterError

from .. import ids
from ..callscope import CallScope, Visibility

METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Message-ID", "References",
                    "In-Reply-To"]
BODY_LIMIT = 50_000                     # characters of body text per message
NOT_FOUND = "not found"
_QUERY_UNSAFE = re.compile(r"[\s/(){}\"]+")


def headers(message: dict) -> dict[str, str]:
    """First value of each header, by lowercase name."""
    out: dict[str, str] = {}
    payload = message.get("payload") if isinstance(message, dict) else None
    for h in (payload or {}).get("headers") or []:
        if isinstance(h, dict) and isinstance(h.get("name"), str) and \
                isinstance(h.get("value"), str):
            out.setdefault(h["name"].lower(), h["value"])
    return out


def participants(messages: list[dict]) -> set[str]:
    values = []
    for m in messages:
        hdr = headers(m)
        values += [hdr.get(n, "") for n in ("from", "to", "cc")]
    out = set()
    for _, addr in getaddresses(values):
        addr = addr.strip().lower()
        if ids.EMAIL_RE.fullmatch(addr):
            out.add(addr)
    return out


def labels_of(messages: list[dict]) -> set[str]:
    out: set[str] = set()
    for m in messages:
        out.update(x for x in m.get("labelIds") or [] if isinstance(x, str))
    return out


def internal_ms(message: dict) -> int:
    try:
        return int(message.get("internalDate"))
    except (TypeError, ValueError):
        return 0                            # unknown date: outside any window


def check_thread(thread: object, sv: CallScope, now: float, *, window: bool) -> list[dict]:
    """The thread's messages this call may see; 404 when the thread is hidden."""
    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
        raise AdapterError(404, NOT_FOUND)
    tid = thread["id"].lower()
    if not sv.vis("thread").admits(tid):
        raise AdapterError(404, NOT_FOUND)
    messages = [m for m in thread.get("messages") or [] if isinstance(m, dict)]
    if not messages:
        raise AdapterError(404, NOT_FOUND)
    lv = sv.vis("label")
    labels = labels_of(messages)
    if labels & lv.deny or (lv.allow is not None and not labels & lv.allow):
        raise AdapterError(404, NOT_FOUND)
    cv = sv.vis("contact")
    people = participants(messages)
    if people & cv.deny or (cv.allow is not None and not people & cv.allow):
        raise AdapterError(404, NOT_FOUND)
    days = sv.bound("date_window_days") if window else None
    if days is not None:
        cutoff = int((now - days * 86400) * 1000)
        messages = [m for m in messages if internal_ms(m) >= cutoff]
        if not messages:
            raise AdapterError(404, NOT_FOUND)
    return messages


# ---- search terms (a narrowing hint, never the enforcement) ----------------------

def label_term(label_id: str, name: str) -> str:
    if label_id.startswith("CATEGORY_"):
        return "category:" + label_id[len("CATEGORY_"):].lower()
    return "label:" + _QUERY_UNSAFE.sub("-", name).strip("-").lower()


def search_terms(label_names: dict[str, str], lv: Visibility, cv: Visibility,
                 window_days: int | None) -> list[str]:
    terms = []
    allowed = sorted((lv.allow or set()) - lv.deny)
    known = [label_term(i, label_names[i]) for i in allowed if i in label_names]
    if known:
        terms.append("{" + " ".join(known) + "}")      # {a b} is OR in Gmail search
    terms += ["-" + label_term(i, label_names[i]) for i in sorted(lv.deny) if i in label_names]
    people = sorted(a for a in (cv.allow or set()) - cv.deny if ids.EMAIL_RE.fullmatch(a))
    if people:
        terms.append("{" + " ".join(f"from:{a} to:{a} cc:{a}" for a in people) + "}")
    for a in sorted(cv.deny):
        if ids.EMAIL_RE.fullmatch(a):
            terms += [f"-from:{a}", f"-to:{a}", f"-cc:{a}"]
    if window_days is not None:
        terms.append(f"newer_than:{max(window_days, 1)}d")
    return terms


# ---- rendering ---------------------------------------------------------------------

def summary_row(thread: dict, messages: list[dict]) -> dict:
    last = messages[-1]
    first_hdr = headers(messages[0])
    return {"id": thread["id"].lower(),
            "subject": first_hdr.get("subject", ""),
            "from": headers(last).get("from", ""),
            "date": headers(last).get("date", ""),
            "snippet": last.get("snippet", "") if isinstance(last.get("snippet"), str) else "",
            "message_count": len(messages),
            "labels": sorted(labels_of(messages)),
            "resource_ref": {"kind": "thread", "id": thread["id"].lower()}}


def message_row(message: dict, attachments: bool) -> dict:
    hdr = headers(message)
    text, truncated = body_text(message.get("payload") or {})
    row = {"id": str(message.get("id", "")).lower(),
           "from": hdr.get("from", ""), "to": hdr.get("to", ""), "cc": hdr.get("cc", ""),
           "date": hdr.get("date", ""), "subject": hdr.get("subject", ""),
           "labels": sorted(labels_of([message])), "body": text}
    if truncated:
        row["body_truncated"] = True
    if attachments:
        row["attachments"] = attachment_list(message.get("payload") or {})
    return row


def _walk(part: dict):
    yield part
    for sub in part.get("parts") or []:
        if isinstance(sub, dict):
            yield from _walk(sub)


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def body_text(payload: dict) -> tuple[str, bool]:
    """Plain-text body (HTML stripped when there is no text part), capped."""
    plain, rich = [], []
    for part in _walk(payload):
        if part.get("filename"):
            continue                        # an attachment, not the body
        data = (part.get("body") or {}).get("data")
        if not isinstance(data, str):
            continue
        try:
            decoded = _decode(data).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            continue
        mime = str(part.get("mimeType", "")).lower()
        if mime == "text/plain":
            plain.append(decoded)
        elif mime == "text/html":
            rich.append(_strip_html(decoded))
    text = "\n".join(plain) if plain else "\n".join(rich)
    return (text[:BODY_LIMIT], True) if len(text) > BODY_LIMIT else (text, False)


def _strip_html(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", markup)
    return html.unescape(re.sub(r"(?s)<[^>]+>", " ", markup)).strip()


def attachment_list(payload: dict) -> list[dict]:
    out = []
    for part in _walk(payload):
        body = part.get("body") or {}
        if part.get("filename") and (body.get("attachmentId") or body.get("data")):
            out.append({"part_id": str(part.get("partId", "")),
                        "filename": str(part["filename"])[:255],
                        "mime_type": ids.safe_mime(part.get("mimeType")),
                        "size": body.get("size") if isinstance(body.get("size"), int) else None})
    return out


def find_part(payload: dict, part_id: str) -> dict | None:
    for part in _walk(payload):
        if str(part.get("partId", "")) == part_id and part.get("filename"):
            return part
    return None


def decode_attachment(data: object) -> bytes:
    if not isinstance(data, str):
        raise AdapterError(503, "Google returned no attachment data")
    try:
        return _decode(data)
    except (ValueError, TypeError) as exc:
        raise AdapterError(503, "Google returned malformed attachment data") from exc
