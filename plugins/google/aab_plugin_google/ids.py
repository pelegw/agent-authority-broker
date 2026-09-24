"""Canonical ids for the resources the three manifests declare.

Hidden lists, grants and denies compare exact strings, so every id is put
in ONE canonical form before any comparison, and a spelling that Google
would still resolve to the same object must never act as a second name
for a hidden one:

  email / contact / attendee   lowercase address; "Name <a@b>" accepted,
                               the display name is dropped (labels only)
  gmail thread / message id    lowercase hex (Gmail ids are hex)
  drive file / folder id       Drive's own charset, case kept (ids are
                               case-sensitive); the alias `root` is resolved
                               by the adapter, never compared as a string
  calendar id                  lowercase; the alias `primary` is resolved by
                               the adapter

Everything that fails the shape is a 400 before any API call.
"""

import re
from email.utils import parseaddr

from aab_plugin_runtime import AdapterError

# Deliberately plain: no quoted local parts, no comments, no IP literals.
EMAIL_RE = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@[a-z0-9-]+(\.[a-z0-9-]+)+")
DOMAIN_RE = re.compile(r"[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+")
HEX_ID_RE = re.compile(r"[0-9a-f]{1,40}")
DRIVE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,200}")
CALENDAR_ID_RE = re.compile(r"[a-z0-9._%+#-]{1,200}@[a-z0-9.-]{1,200}")
EVENT_ID_RE = re.compile(r"[A-Za-z0-9_@.:-]{1,1024}")
PART_ID_RE = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6}){0,8}")


def email(value: object) -> str:
    """Canonical lowercase address, or 400."""
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise AdapterError(400, "not an email address")
    _, addr = parseaddr(value.strip())
    addr = addr.strip().lower()
    if not EMAIL_RE.fullmatch(addr):
        raise AdapterError(400, f"not an email address: {value[:80]!r}")
    return addr


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[1]


def domain(value: object) -> str:
    if not isinstance(value, str) or not DOMAIN_RE.fullmatch(value.strip().lower()):
        raise AdapterError(400, "not a domain name")
    return value.strip().lower()


def hex_id(value: object, what: str = "id") -> str:
    v = value.strip().lower() if isinstance(value, str) else ""
    if not HEX_ID_RE.fullmatch(v):
        raise AdapterError(400, f"not a Gmail {what}")
    return v


def drive_id(value: object) -> str:
    v = value.strip() if isinstance(value, str) else ""
    if not DRIVE_ID_RE.fullmatch(v):
        raise AdapterError(400, "not a Drive id")
    return v


def calendar_id(value: object) -> str:
    v = value.strip().lower() if isinstance(value, str) else ""
    if not CALENDAR_ID_RE.fullmatch(v):
        raise AdapterError(400, "not a calendar id")
    return v


def event_id(value: object) -> str:
    v = value.strip() if isinstance(value, str) else ""
    if not EVENT_ID_RE.fullmatch(v):
        raise AdapterError(400, "not an event id")
    return v


def part_id(value: object) -> str:
    v = value.strip() if isinstance(value, str) else ""
    if not PART_ID_RE.fullmatch(v):
        raise AdapterError(400, "not a message part id")
    return v


# A type is whatever the sender of an email or the uploader of a file
# claimed. Types a browser would execute or render as a page are served as
# opaque bytes, so attacker-supplied HTML/SVG can never be delivered as such
# from the broker's origin (same list as the WhatsApp plugin).
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


def mime_type(value: object) -> str:
    """A declared mime type (uploads), lowercased, or 400."""
    base = value.split(";", 1)[0].strip().lower() if isinstance(value, str) else ""
    if not _MIME_RE.fullmatch(base):
        raise AdapterError(400, "not a mime type")
    return base
