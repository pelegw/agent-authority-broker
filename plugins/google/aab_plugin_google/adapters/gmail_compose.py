"""Outgoing Gmail: who a draft or send may address, and the RFC 2822 bytes.

Recipient rules, applied to to + cc + bcc before any API call:
  * every address is canonicalized (ids.email) first, so "Alice
    <ALICE@x.com>" and "alice@x.com" are the same recipient everywhere;
  * `bcc: false` refuses any bcc (403);
  * `contact` allow set: every recipient must be in it (403), and
    `domain` allow set: every recipient's domain must be in it (403);
  * a denied contact or domain is a 404, checked after the allow sets
    (callscope.Visibility.check_named explains the order).

Headers are built with the stdlib `email` package; CR/LF in any header
value is refused outright, so an agent cannot add headers of its own.
"""

import base64
from email.message import EmailMessage
from email.policy import SMTP

from aab_plugin_runtime import AdapterError

from .. import ids
from ..callscope import CallScope
from .base import strings, text
from .gmail_threads import headers

MAX_RECIPIENTS = 100


def recipients(params: dict, sv: CallScope) -> tuple[list[str], list[str], list[str]]:
    to = _addresses(params, "to")
    cc = _addresses(params, "cc")
    bcc = _addresses(params, "bcc")
    if not to:
        raise AdapterError(400, "to must name at least one recipient")
    if bcc and not sv.flag("bcc"):
        raise AdapterError(403, "bcc is not allowed by your grant")
    everyone = list(dict.fromkeys(to + cc + bcc))
    if len(everyone) > MAX_RECIPIENTS:
        raise AdapterError(400, f"more than {MAX_RECIPIENTS} recipients")
    contacts, domains = sv.vis("contact"), sv.vis("domain")
    for addr in everyone:
        contacts.check_named(addr, f"recipient {addr}")
        domains.check_named(ids.domain_of(addr), f"recipient domain {ids.domain_of(addr)}")
    return to, cc, bcc


def _addresses(params: dict, name: str) -> list[str]:
    return list(dict.fromkeys(ids.email(v) for v in strings(params, name, MAX_RECIPIENTS)))


def reply_headers(messages: list[dict]) -> dict[str, str]:
    """In-Reply-To / References / subject for a reply to the thread's last
    message, so Gmail threads it (the threadId alone is not enough)."""
    last = headers(messages[-1])
    msg_id = _one_line(last.get("message-id", ""))
    refs = _one_line(last.get("references", ""))
    out = {"subject": _one_line(headers(messages[0]).get("subject", ""))}
    if msg_id:
        out["in_reply_to"] = msg_id
        out["references"] = f"{refs} {msg_id}".strip()
    return out


def _one_line(value: str) -> str:
    return " ".join(value.split())[:998]


def build_raw(params: dict, to: list[str], cc: list[str], bcc: list[str],
              reply: dict | None) -> str:
    subject = text(params, "subject", default="", strip=False) or ""
    body = text(params, "body", default="", strip=False) or ""
    if "\r" in subject or "\n" in subject:
        raise AdapterError(400, "subject must be one line")
    if not subject and reply:
        base = reply.get("subject", "")
        subject = base if base.lower().startswith("re:") else f"Re: {base}".strip()
    msg = EmailMessage(policy=SMTP)
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    if reply and reply.get("in_reply_to"):
        msg["In-Reply-To"] = reply["in_reply_to"]
        msg["References"] = reply["references"]
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
