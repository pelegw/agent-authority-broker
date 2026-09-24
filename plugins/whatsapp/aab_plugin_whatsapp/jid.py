"""JID normalization: the one canonical id for a WhatsApp chat or person.

Two entry points, because not every chat can be written to:

  normalize_recipient  the SEND path: people (@s.whatsapp.net, @lid) and groups
                       (@g.us), or an international phone number. Ported from
                       WA_GW `gateway/app/policy.normalize_jid` (the body is
                       verbatim; a refusal is the runtime's AdapterError(400)
                       instead of WA_GW's PolicyError). It mirrors the Go
                       sidecar's `ParseRecipient`
                       (sidecars/whatsapp/internal/wa/actions.go), so the id the
                       broker compares against grants and hidden lists is
                       exactly the id the sidecar delivers to;
                       tests/test_jid.py runs the Go test's own vectors on it.
  normalize_jid        any CHAT id: everything above, plus the read-only chats
                       the archive also holds, status updates
                       (`status@broadcast`), broadcast lists (`<digits>@broadcast`)
                       and channels (`<digits>@newsletter`). They must normalize
                       so the owner can hide them and agents can read them; the
                       sidecar cannot send to them, so send_message uses
                       normalize_recipient and refuses them with a 400.

One deliberate addition after the verbatim body: the user part must be a
canonical WhatsApp id (digits, or digits-digits for an old-style group).
Grants and hidden lists match by exact string, so a spelling the WhatsApp
servers might still route to the same account ("+9725...@s.whatsapp.net",
"9725 0...@...", an invisible character inside the digits) would otherwise be
a second name for a hidden chat. Refusing every non-canonical spelling closes
that door structurally; it only ever rejects more than the sidecar would,
never less.
"""

import re

from aab_plugin_runtime import AdapterError

_PHONE_RE = re.compile(r"^\+?[0-9]{6,20}$")
# Phone users and @lid ids are digits; groups are digits or "creator-created".
_CANONICAL_USER_RE = re.compile(r"[0-9]+(?:-[0-9]+)?")
_DIGITS_RE = re.compile(r"[0-9]+")

# Readable (archived, hideable) but never sendable through the sidecar.
READ_ONLY_SERVERS = ("broadcast", "newsletter")


def normalize_recipient(to: str) -> str:
    """Mirror the sidecar's recipient parsing so allowlists match exactly.

    Accepts full JIDs (user or group) or international phone numbers.
    """
    if not isinstance(to, str):
        raise AdapterError(400, "recipient must be a string")
    jid = _normalize_jid_wa_gw(to)
    user = jid.partition("@")[0]
    if not _CANONICAL_USER_RE.fullmatch(user):
        raise AdapterError(400, f"recipient {to!r} is not a canonical WhatsApp id "
                                "(digits only, or digits-digits for a group)")
    return jid


def normalize_jid(value: str) -> str:
    """Canonical id of any chat: a recipient, or a read-only broadcast /
    status / channel chat."""
    if isinstance(value, str) and "@" in value:
        user, _, server = value.strip().partition("@")
        if server in READ_ONLY_SERVERS:
            # Same suffix stripping as the send path, for one spelling per chat.
            user = user.split(":", 1)[0].split(".", 1)[0]
            canonical = (_DIGITS_RE.fullmatch(user) is not None
                         or (server == "broadcast" and user == "status"))
            if not canonical:
                raise AdapterError(400, f"chat {value!r} is not a canonical WhatsApp id "
                                        "(status@broadcast, <digits>@broadcast or "
                                        "<digits>@newsletter)")
            return f"{user}@{server}"
    return normalize_recipient(value)


def _normalize_jid_wa_gw(to: str) -> str:
    # ---- verbatim from WA_GW policy.normalize_jid (PolicyError -> AdapterError)
    to = to.strip()
    if "@" in to:
        user, _, server = to.partition("@")
        # @lid is WhatsApp's hidden-user addressing; archived chats use it, so
        # it must be sendable. Strip both the ":N" device suffix and the ".N"
        # agent suffix, exactly as the sidecar's ToNonAD() does — otherwise the
        # allowlist/approved JID would differ from what actually gets delivered.
        # Legitimate user parts (phone numbers, group and lid ids) never contain
        # "." or ":", so this only ever removes those routing suffixes.
        user = user.split(":", 1)[0].split(".", 1)[0]
        if server not in ("s.whatsapp.net", "g.us", "lid") or not user:
            raise AdapterError(400, f"unsupported recipient {to!r} (want @s.whatsapp.net, @g.us, @lid, or a phone number)")
        return f"{user}@{server}"
    if not _PHONE_RE.match(to):
        raise AdapterError(400, f"recipient {to!r} is neither a JID nor an international phone number")
    return to.lstrip("+") + "@s.whatsapp.net"
