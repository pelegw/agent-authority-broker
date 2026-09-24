"""Notification fan-out for things waiting on the owner.

Ported from WA_GW notify/: a pluggable seam (`Notifier`, base.py) with every
call NON-FATAL. A channel failure is audited and swallowed so it can never
break the agent's request or the queue insert that triggered it.

Providers: anything listed in `_PROVIDERS` (tests use it), plus Telegram
whenever it is live: a bot token stored, the channel enabled, and the
owner's chat linked (all managed from the console, no restart). Only the
outbound half of Telegram is imported here; the approve path lives in
notify/telegram_inbound.py, which nothing on the agent surface imports.
"""

import logging

from ..audit import audit
from ..logging_setup import kv

_PROVIDERS: list = []
log = logging.getLogger(__name__)


def _providers() -> list:
    provs = list(_PROVIDERS)
    # Imported lazily: telegram pulls in cards and crypto, which the many
    # importers of this package (the action queue) should not pay for at load.
    from . import telegram
    if telegram.active():
        provs.append(telegram)
    return provs


def _fan_out(method: str, item: dict) -> None:
    # Even provider selection is guarded: reading config must not be able to
    # break the caller.
    try:
        providers = _providers()
    except Exception as exc:
        audit("system", "notify.failed", str(item.get("id", "")),
              {"phase": "select", "error": type(exc).__name__}, result="error")
        log.warning("notification failed %s", kv(phase="select", error=type(exc).__name__))
        return
    for p in providers:
        try:
            getattr(p, method)(item)
        except Exception as exc:          # a channel outage never breaks the caller
            provider = getattr(p, "__name__", type(p).__name__)
            audit("system", "notify.failed", str(item.get("id", "")),
                  {"provider": provider, "method": method,
                   "error": type(exc).__name__}, result="error")
            # The class and status only: a Telegram error text can quote the chat.
            log.warning("notification failed %s", kv(
                provider=provider, method=method, id=item.get("id"),
                error=type(exc).__name__, status=getattr(exc, "status", None)))


def notify_action(action: dict) -> None:
    _fan_out("notify_action", action)


def notify_grant_request(grant: dict) -> None:
    _fan_out("notify_grant_request", grant)
