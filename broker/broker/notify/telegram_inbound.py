"""Telegram channel, inbound half: the poll loop, linking, and button taps.

This is the only module where a Telegram update can turn into an owner
decision, and it is deliberately NOT reachable from the agent surface
(nothing under notify/__init__ imports it; main.py and the admin router
do). A tap is honoured only when every one of these holds:
  * the channel is enabled (the kill switch; taps are refused while off);
  * the callback came from the linked chat AND from the linked user
    (a linked group could otherwise let any member approve);
  * the linked user is bound to an owner principal that still exists and
    is not disabled; the decision then runs as
    `AdminContext(owner, username, via="telegram", credential_id=<user id>)`
    through services.admin, exactly like a console click, so the atomic
    claim there makes a tap racing a click (or a double tap) a no-op;
  * approving an action or grant whose card would not fit is refused: the
    card is re-rendered from the stored row, never trusted from the chat.

Linking needs a one-time code (notify/telegram.py `start_linking`) echoed
from a PRIVATE chat within its TTL; the chat and the sender are then bound
to the owner who started the link.

The poll loop resumes from the persisted offset so a restart doesn't replay
buffered taps, backs off on errors, and never logs the token. `supervise()`
runs in the app lifespan and starts or stops the loop whenever the stored
token appears, changes, or is cleared, so no restart is ever needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets

import anyio.to_thread

from .. import db
from ..actions import queue
from ..audit import audit
from ..authority import store
from ..deps import AdminContext
from ..errors import PolicyError
from ..identity import principals
from ..services import admin
from . import cards, telegram

POLL_TIMEOUT = 25                # getUpdates long-poll seconds
SUPERVISE_INTERVAL = 2.0         # how quickly a stored/cleared token takes effect
MAX_BACKOFF = 60
# Taps whose failure leaves the item pending (released, retryable): the card
# keeps its buttons instead of being edited to an outcome.
_KEEP_BUTTONS = {429, 503}


# ---- linking ----------------------------------------------------------------------

def _link_code_from(text: str) -> str:
    """The code in "/start <code>" or a bare "<code>"; "" otherwise."""
    parts = (text or "").strip().split()
    if len(parts) == 2 and parts[0].split("@", 1)[0] == "/start":
        return parts[1]
    return parts[0] if len(parts) == 1 else ""


def _try_link(msg: dict) -> None:
    """Link only for a valid, unexpired code sent FROM A PRIVATE CHAT by a
    human account. The code reached the owner over the authenticated admin
    channel, so presenting it binds this Telegram user to that owner; a
    stranger messaging the bot, or a group chat, cannot link."""
    pending = telegram.pending_link()
    if pending is None:
        return
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    if chat.get("type") != "private" or sender.get("is_bot"):
        return
    supplied = _link_code_from(msg.get("text", ""))
    if not supplied or not secrets.compare_digest(supplied.encode(), pending["code"].encode()):
        return
    chat_id, user_id = str(chat.get("id", "")), str(sender.get("id", ""))
    # In a private chat the chat id IS the user id; anything else is not a
    # genuine private chat with this person.
    if not chat_id or chat_id != user_id:
        return
    telegram.consume_link()
    db.set_config(telegram.CFG_CHAT, chat_id)
    db.set_config(telegram.CFG_USER, user_id)
    db.set_config(telegram.CFG_PRINCIPAL, pending["principal_id"])
    audit(pending["username"], "telegram.linked", "telegram",
          {"chat_id": chat_id, "user_id": user_id},
          actor_principal=pending["principal_id"], actor_via="telegram")
    with contextlib.suppress(telegram.TelegramError):
        telegram._api_send_message("✅ This chat is now linked for Agent Authority Broker "
                                   "approvals. Enable the channel in the console to receive cards.")


# ---- taps -------------------------------------------------------------------------

def _refuse(cb_id: str, chat_id, from_id: str, why: str) -> None:
    telegram._answer_callback(cb_id, "not authorized")
    audit("system", "telegram.rejected_chat", "telegram",
          {"chat_id": str(chat_id), "from_id": from_id, "why": why}, result="denied")


def _owner_ctx(from_id: str) -> AdminContext | None:
    """The owner this Telegram user stands for, or None (fail closed)."""
    pid = telegram.principal_id()
    owner = principals.get(pid) if pid else None
    if owner is None or owner.disabled:
        return None
    return AdminContext(owner.id, owner.username, "telegram", from_id)


def _fits(kind: str, item_id: str) -> bool:
    """Re-render the stored item's card: False when it would be oversized.
    A missing item passes, so the service answers 404 ("already handled")."""
    if kind == "a":
        row = queue.get_row(item_id)
        return row is None or cards.action_card(cards.action_from_row(row)).approvable
    grant = store.get(item_id)
    return grant is None or cards.grant_card(cards.grant_from_store(grant)).approvable


def _current_status(kind: str, item_id: str) -> str | None:
    if kind == "a":
        row = queue.get_row(item_id)
        return row["status"] if row else None
    grant = store.get(item_id)
    return grant.status if grant else None


def _decide(ctx: AdminContext, kind: str, verb: str, item_id: str) -> str:
    if kind == "a":
        fn = admin.approve_action if verb == "approve" else admin.reject_action
        return fn(ctx, item_id)["status"]
    return admin.decide_grant(ctx, item_id, "active" if verb == "approve" else "rejected")["status"]


def _handle_callback(cq: dict) -> None:
    cb_id = str(cq.get("id", ""))
    # The kill switch: while the channel is disabled no tap is honoured.
    if not telegram.enabled():
        telegram._answer_callback(cb_id, "approvals are disabled")
        return
    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    from_id = str((cq.get("from") or {}).get("id", ""))
    # BOTH the linked chat and the linked user; an unset link matches nothing.
    linked_chat, linked_user = telegram.chat_id(), telegram.user_id()
    if not (linked_chat and linked_user and str(chat_id) == linked_chat
            and from_id == linked_user):
        _refuse(cb_id, chat_id, from_id, "chat or user mismatch")
        return
    ctx = _owner_ctx(from_id)
    if ctx is None:
        _refuse(cb_id, chat_id, from_id, "no active owner bound to this link")
        return
    parsed = cards.parse_callback(cq.get("data"))
    if parsed is None:
        telegram._answer_callback(cb_id, "bad request")
        return
    kind, verb, item_id = parsed
    if verb == "approve" and not _fits(kind, item_id):
        telegram._answer_callback(cb_id, "Review and approve the complete request in the console")
        return
    try:
        status = _decide(ctx, kind, verb, item_id)
    except PolicyError as exc:
        if exc.status in _KEEP_BUTTONS or exc.code == "held":
            # Still pending (released, or held while its plugin is off): the
            # card keeps its buttons so the owner can try again.
            telegram._answer_callback(cb_id, str(exc))
            return
        # 404/409: decided elsewhere (console, CLI, an earlier tap), which is
        # not an error. Anything else ended the item. Either way the card now
        # shows what the item actually became, and loses its buttons.
        handled = exc.status in (404, 409)
        telegram._answer_callback(cb_id, "already handled" if handled else str(exc))
        if message_id is not None:
            now = _current_status(kind, item_id)
            label = now if now and now != "pending" else ("already handled" if handled
                                                          else "failed")
            telegram._edit_message(message_id, cards.outcome_text(kind, label, item_id))
        return
    telegram._answer_callback(cb_id, status)
    if message_id is not None:
        telegram._edit_message(message_id, cards.outcome_text(kind, status, item_id))


def _handle_update(update: dict) -> None:
    """Dispatch one Telegram update. Sync (the loop runs it in a thread)."""
    if update.get("message"):
        _try_link(update["message"])
    elif update.get("callback_query"):
        _handle_callback(update["callback_query"])


# ---- the loop ---------------------------------------------------------------------

async def poll_loop() -> None:
    """Long-poll getUpdates and dispatch; runs while a token is stored."""
    telegram.poll_running(True)
    offset = int(db.get_config(telegram.CFG_OFFSET, "0") or "0")
    backoff = 1
    with contextlib.suppress(Exception):
        # A webhook left over from another deployment makes getUpdates 409.
        await anyio.to_thread.run_sync(lambda: telegram._api("deleteWebhook"))
    try:
        while True:
            try:
                # abandon_on_cancel: a restart or shutdown must not wait out a
                # 25 s long poll; the abandoned call's result is simply dropped
                # (nothing was confirmed, the next call fetches it again).
                updates = await anyio.to_thread.run_sync(
                    telegram._get_updates, offset, POLL_TIMEOUT, abandon_on_cancel=True)
                if telegram.poll_ok():
                    audit("system", "telegram.poll_recovered", "telegram")
                backoff = 1
                for u in updates or []:
                    uid = u.get("update_id") if isinstance(u, dict) else None
                    if not isinstance(uid, int):
                        continue
                    offset = max(offset, uid + 1)
                    try:
                        await anyio.to_thread.run_sync(_handle_update, u)
                    except Exception as exc:        # one bad update never stops the loop
                        audit("system", "telegram.update_error", "telegram",
                              {"error": type(exc).__name__}, result="error")
                if updates:
                    await anyio.to_thread.run_sync(db.set_config, telegram.CFG_OFFSET,
                                                   str(offset))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                streak = telegram.poll_failed(exc)
                if streak == 1:
                    # Once per error streak, type and status only: an outage
                    # must not flood the audit log, and messages can carry URLs.
                    audit("system", "telegram.poll_error", "telegram",
                          {"error": type(exc).__name__, "status": getattr(exc, "status", None)},
                          result="error")
                await asyncio.sleep(min(backoff, MAX_BACKOFF))
                backoff = min(backoff * 2, MAX_BACKOFF)
    finally:
        telegram.poll_running(False)


async def _stop(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def supervise(interval: float | None = None) -> None:
    """Keep exactly one poll loop running for the stored token, none without.

    Watches `telegram.desired_state()`: a token appearing starts the loop, a
    different token restarts it, a cleared or unreadable token stops it. A
    loop that died unexpectedly is restarted on the next tick.
    """
    task: asyncio.Task | None = None
    current: str | None = None
    try:
        while True:
            try:
                want = await anyio.to_thread.run_sync(telegram.desired_state)
            except Exception:
                want = None               # state unreadable: stop, never guess
            if task is not None and task.done():
                task, current = None, None
            if want != current:
                await _stop(task)
                task = asyncio.create_task(poll_loop()) if want is not None else None
                current = want
            await asyncio.sleep(SUPERVISE_INTERVAL if interval is None else interval)
    finally:
        await _stop(task)
