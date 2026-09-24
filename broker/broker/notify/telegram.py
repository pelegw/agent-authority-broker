"""Telegram channel, outbound half: sending cards, and the admin-managed state.

Ported from WA_GW notify/telegram.py with these changes:
  * the bot TOKEN is entered in the console and stored encrypted through
    crypto.py (slot `broker`, name `telegram_bot_token`), never in env;
  * the linked Telegram user is bound to the owner principal who started
    the link (`app_config.telegram_principal_id`), so a tap becomes an
    `AdminContext(via="telegram")` for that owner;
  * cards come from notify/cards.py (manifest-derived, oversized refused).

Split in two on purpose. This module is reachable from the agent surface
(actions.queue -> notify -> here), so it must never import the approve
path; notify/telegram_inbound.py (poll loop, linking, taps that call
services.admin) is imported only by main.py and the admin router.
tests/test_telegram.py walks the import graph to keep it that way.

Runtime state lives in app_config: telegram_enabled (the kill switch),
telegram_chat_id / telegram_user_id / telegram_principal_id (the link),
telegram_offset (getUpdates offset), telegram_bot_id (which bot the link and
offset belong to; the numeric id before the token's colon, not a secret).
The poll loop runs whenever a token is stored (linking and taps need it);
`enabled` gates whether cards are pushed and taps honoured.

The token never leaves this module except inside the request URL: it is
not logged, audited, returned by any route, or put in an exception message.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import secrets
import time

import httpx

from .. import crypto, db
from ..audit import audit
from ..errors import PolicyError
from ..logging_setup import kv
from . import cards

log = logging.getLogger(__name__)

TOKEN_NAME = "telegram_bot_token"
LINK_TTL = 300                     # seconds a one-time link code stays valid
SEND_TIMEOUT = 15.0                # bounds a notification inside an agent's request
_API = "https://api.telegram.org"
# The only shape a Telegram bot token has. Enforced on entry AND on read, so
# nothing else can ever be interpolated into the API URL path.
TOKEN_RE = re.compile(r"^[0-9]{5,16}:[A-Za-z0-9_-]{30,80}$")

CFG_ENABLED = "telegram_enabled"
CFG_CHAT = "telegram_chat_id"
CFG_USER = "telegram_user_id"
CFG_PRINCIPAL = "telegram_principal_id"
CFG_OFFSET = "telegram_offset"
CFG_BOT_ID = "telegram_bot_id"

# ---- in-process state (one uvicorn worker -> one instance) ---------------------
# The pending link: {code, expires, principal_id, username}. In memory only,
# so a restart simply cancels a half-finished link.
_link: dict | None = None
_bot_username: str | None = None
_poll: dict = {"running": False, "last_ok_at": None, "last_error": None,
               "consecutive_errors": 0}


# httpx logs every request URL at INFO ("HTTP Request: POST https://api.telegram.org/
# bot<token>/getUpdates ..."). Any deployment that turns INFO logging on
# would print the token, so the httpx logger rewrites that path segment for
# every record before a handler can see it.
_BOT_PATH_RE = re.compile(r"/bot[0-9]{5,16}:[A-Za-z0-9_-]+")


class _RedactBotToken(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:          # a malformed record is not ours to fix
            return True
        if "/bot" in message:
            record.msg, record.args = _BOT_PATH_RE.sub("/bot<redacted>", message), ()
        return True


if not any(isinstance(f, _RedactBotToken) for f in logging.getLogger("httpx").filters):
    logging.getLogger("httpx").addFilter(_RedactBotToken())


class TelegramError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---- state accessors -------------------------------------------------------------

def _token() -> str:
    """The stored bot token, or "" when absent, unreadable or malformed.

    Every failure reads as "no token": Telegram then stays off (no poll loop,
    no cards) rather than running on something unverified.
    """
    try:
        token = crypto.get(crypto.BROKER_SLOT, TOKEN_NAME) or ""
    except (crypto.SecretsUnavailable, crypto.SecretsUnreadable):
        return ""
    return token if TOKEN_RE.match(token) else ""


def token_state() -> str:
    """unset | set | unreadable (stored, but not under the current key)."""
    return crypto.state(crypto.BROKER_SLOT, TOKEN_NAME)


def chat_id() -> str:
    return db.get_config(CFG_CHAT, "") or ""


def user_id() -> str:
    return db.get_config(CFG_USER, "") or ""


def principal_id() -> str:
    return db.get_config(CFG_PRINCIPAL, "") or ""


def enabled() -> bool:
    return db.get_config(CFG_ENABLED, "0") == "1"


def linked() -> bool:
    """A chat, the user allowed to tap in it, and the owner they stand for."""
    return bool(chat_id() and user_id() and principal_id())


def active() -> bool:
    """Cards are pushed only when enabled, linked, and a token is stored."""
    return enabled() and linked() and bool(_token())


def desired_state() -> str | None:
    """What the poll loop should be running for: a fingerprint of the stored
    token (in memory only), or None when there is no usable token. The
    supervisor restarts the loop whenever this changes."""
    token = _token()
    return hashlib.sha256(token.encode()).hexdigest() if token else None


# ---- poll health (written by the inbound loop, read by status()) ----------------

def poll_running(running: bool) -> None:
    _poll["running"] = running


def poll_ok() -> bool:
    """Record a successful getUpdates; True when it ends an error streak."""
    recovered = _poll["consecutive_errors"] > 0
    _poll.update(last_ok_at=int(time.time()), last_error=None, consecutive_errors=0)
    return recovered


def poll_failed(exc: BaseException) -> int:
    """Record a failed poll (type and status only, never the message)."""
    _poll["consecutive_errors"] += 1
    _poll["last_error"] = {"type": type(exc).__name__, "status": getattr(exc, "status", None),
                           "at": int(time.time())}
    return _poll["consecutive_errors"]


# ---- low-level HTTP (these are what tests monkeypatch) ---------------------------

def _client(token: str, http_timeout: float) -> httpx.Client:
    return httpx.Client(base_url=f"{_API}/bot{token}", timeout=http_timeout)


def _api(method: str, _http_timeout: float = SEND_TIMEOUT, **payload) -> dict:
    # _http_timeout bounds the call itself: short for cards (a Telegram
    # brown-out must not hold an agent's request for long), long only for the
    # getUpdates long poll. Error messages never carry str(exc): httpx puts
    # the request URL, and so the token, into some of them.
    token = _token()
    if not token:
        raise TelegramError(503, "no Telegram bot token configured")
    try:
        with _client(token, _http_timeout) as c:
            resp = c.post(f"/{method}", json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise TelegramError(503, f"telegram unreachable ({type(exc).__name__})") from None
    except httpx.HTTPError as exc:
        raise TelegramError(502, f"telegram request failed ({type(exc).__name__})") from None
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        raise TelegramError(502, f"telegram answered {resp.status_code} with a non-JSON body") \
            from None
    if not isinstance(body, dict) or not body.get("ok"):
        desc = str(body.get("description", "")) if isinstance(body, dict) else ""
        raise TelegramError(resp.status_code, desc.replace(token, "<redacted>")[:200]
                            or f"telegram answered {resp.status_code}")
    return body.get("result", {})


def _api_send_message(text: str, keyboard: dict | None = None) -> dict:
    payload = {"chat_id": chat_id(), "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = keyboard
    return _api("sendMessage", **payload)


def _answer_callback(cb_id: str, text: str = "") -> None:
    with contextlib.suppress(TelegramError):
        _api("answerCallbackQuery", callback_query_id=cb_id, text=text[:200])


def _edit_message(message_id: int, text: str) -> None:
    with contextlib.suppress(TelegramError):
        _api("editMessageText", chat_id=chat_id(), message_id=message_id, text=text,
             parse_mode="HTML")


def _get_updates(offset: int, timeout: int) -> list:
    return _api("getUpdates", _http_timeout=timeout + 10.0, offset=offset, timeout=timeout,
                allowed_updates=["message", "callback_query"])


def _get_me() -> str | None:
    global _bot_username
    if _bot_username:
        return _bot_username
    if not _token():
        return None
    with contextlib.suppress(TelegramError):
        # Short: this runs inside the console's status request.
        _bot_username = _api("getMe", _http_timeout=5.0).get("username")
    return _bot_username


def _send_card(card: cards.Card, kind: str, item_id: str) -> dict:
    out = _api_send_message(card.text, card.keyboard)
    # A card too long for Telegram goes out without buttons, pointing at the
    # console: the owner reviews the full request there.
    log.info("telegram card sent %s", kv(kind=kind, id=item_id, buttons=card.approvable))
    if not card.approvable:
        log.info("telegram card too long to approve in chat; sent a console pointer %s",
                 kv(kind=kind, id=item_id))
    return out


# ---- Notifier interface (called from notify._fan_out, which swallows errors) -----

def notify_action(action: dict) -> None:
    _send_card(cards.action_card(action), "action", str(action.get("id", "")))


def notify_grant_request(grant: dict) -> None:
    _send_card(cards.grant_card(grant), "grant", str(grant.get("id", "")))


# ---- admin operations (each takes the acting AdminContext) -----------------------

def _audit(ctx, action: str, detail: dict | None = None, result: str = "ok") -> None:
    audit(ctx.username, action, "telegram", detail, result,
          actor_principal=ctx.principal_id, actor_via=ctx.via)


def _clear_link() -> None:
    for key in (CFG_CHAT, CFG_USER, CFG_PRINCIPAL):
        db.set_config(key, "")
    db.set_config(CFG_ENABLED, "0")


def pending_link() -> dict | None:
    """The live link attempt, or None (expired attempts are dropped)."""
    global _link
    if _link is not None and time.time() > _link["expires"]:
        _link = None
    return _link


def consume_link() -> None:
    global _link
    _link = None


def status() -> dict:
    """What the console's Channels view shows. Never the token."""
    state = token_state()
    return {
        "token": state,                           # unset | set | unreadable
        "re_enter_required": state == "unreadable",
        "secrets_key_configured": crypto.key_configured(),
        "bot_username": _get_me() if state == "set" else None,
        "enabled": enabled(),
        "linked": linked(),
        "chat_id": chat_id() or None,
        "user_id": user_id() or None,
        "linking": pending_link() is not None,
        "active": active(),
        "poll": dict(_poll),
    }


def set_token(ctx, token: str) -> dict:
    """Store the bot token (write-only). A token for a different bot drops
    the link and the update offset: a new bot is a new channel, its update ids
    start elsewhere, and the owner must prove the chat through it again."""
    global _bot_username
    token = (token or "").strip()
    if not TOKEN_RE.match(token):
        raise PolicyError(400, "that is not a Telegram bot token (expected 123456789:AA...)",
                          "invalid_token")
    if not crypto.key_configured():
        raise PolicyError(409, "BROKER_SECRETS_KEY is not set, so the token cannot be stored; "
                               "run scripts/init_secrets.py and restart", "secrets_key_missing")
    bot_id = token.split(":", 1)[0]
    crypto.put(crypto.BROKER_SLOT, TOKEN_NAME, token)
    changed = (db.get_config(CFG_BOT_ID, "") or "") != bot_id
    if changed:
        _clear_link()
        db.set_config(CFG_OFFSET, "0")
        db.set_config(CFG_BOT_ID, bot_id)
    _bot_username = None
    consume_link()
    _audit(ctx, "telegram.token_set", {"bot_id": bot_id, "bot_changed": changed})
    log.info("telegram bot token stored %s", kv(bot_changed=changed, by=ctx.username,
                                                via=ctx.via))
    return status()


def clear_token(ctx) -> dict:
    """Delete the stored token. The poll loop stops within a supervisor tick.
    The link is kept, so re-entering the same bot's token resumes it."""
    global _bot_username
    removed = crypto.delete(crypto.BROKER_SLOT, TOKEN_NAME)
    _bot_username = None
    consume_link()
    _audit(ctx, "telegram.token_cleared", {"removed": removed})
    log.info("telegram bot token cleared %s", kv(removed=removed, by=ctx.username,
                                                 via=ctx.via))
    return status()


def start_linking(ctx) -> dict:
    """Issue a one-time code the owner sends to the bot from a PRIVATE chat
    within LINK_TTL seconds. The code travels over the authenticated admin
    channel, which is what binds the Telegram account to this owner."""
    global _link
    if not _token():
        raise PolicyError(400, "store the bot token first", "no_token")
    code = secrets.token_hex(8)
    _link = {"code": code, "expires": time.time() + LINK_TTL,
             "principal_id": ctx.principal_id, "username": ctx.username}
    username = _get_me()
    _audit(ctx, "telegram.link_started")
    # Never the one-time code: whoever sends it to the bot becomes the approver.
    log.info("telegram link started %s", kv(ttl_seconds=LINK_TTL, by=ctx.username,
                                            via=ctx.via))
    out = {"linking": True, "code": code, "expires_in": LINK_TTL, "bot_username": username,
           "instructions": (f"Within {LINK_TTL // 60} minutes, from your PRIVATE Telegram chat "
                            f"with @{username or 'your bot'}, send: /start {code}")}
    if username:
        out["link"] = f"https://t.me/{username}?start={code}"
    return out


def set_enabled(ctx, value: bool) -> dict:
    if value and not linked():
        raise PolicyError(400, "link a Telegram chat first", "not_linked")
    db.set_config(CFG_ENABLED, "1" if value else "0")
    _audit(ctx, "telegram.enabled" if value else "telegram.disabled")
    log.info("telegram channel switched %s", kv(enabled=value, by=ctx.username, via=ctx.via))
    return status()


def send_test(ctx) -> dict:
    if not linked():
        raise PolicyError(400, "link a Telegram chat first", "not_linked")
    try:
        _api_send_message("✅ Agent Authority Broker test message. Approvals will arrive here.")
    except TelegramError as exc:
        log.warning("telegram test message failed %s", kv(status=exc.status))
        raise PolicyError(503 if exc.status == 503 else 502,
                          f"Telegram did not accept the message: {exc}", "telegram_error") \
            from None
    _audit(ctx, "telegram.test")
    log.info("telegram test message sent %s", kv(by=ctx.username, via=ctx.via))
    return {"sent": True}


def unlink(ctx) -> dict:
    _clear_link()
    consume_link()
    _audit(ctx, "telegram.unlinked")
    log.info("telegram chat unlinked %s", kv(by=ctx.username, via=ctx.via))
    return status()
