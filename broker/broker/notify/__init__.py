"""Notification fan-out for things waiting on the owner.

Ported from WA_GW notify/: a pluggable seam (`Notifier`, base.py) with every
call NON-FATAL. A channel failure is audited and swallowed so it can never
break the agent's request or the queue insert that triggered it.

The provider list is empty in this phase; the Telegram lane appends its
module to `_PROVIDERS` (or makes `_providers()` return it when configured).
"""

from ..audit import audit

_PROVIDERS: list = []


def _providers() -> list:
    return list(_PROVIDERS)


def _fan_out(method: str, item: dict) -> None:
    # Even provider selection is guarded: reading config must not be able to
    # break the caller.
    try:
        providers = _providers()
    except Exception as exc:
        audit("system", "notify.failed", str(item.get("id", "")),
              {"phase": "select", "error": type(exc).__name__}, result="error")
        return
    for p in providers:
        try:
            getattr(p, method)(item)
        except Exception as exc:          # a channel outage never breaks the caller
            audit("system", "notify.failed", str(item.get("id", "")),
                  {"provider": getattr(p, "__name__", type(p).__name__), "method": method,
                   "error": type(exc).__name__}, result="error")


def notify_action(action: dict) -> None:
    _fan_out("notify_action", action)


def notify_grant_request(grant: dict) -> None:
    _fan_out("notify_grant_request", grant)
