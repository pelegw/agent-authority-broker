"""The notifier seam. A provider (notify/telegram.py is the one channel so
far) implements these two functions; `notify._providers()` selects the live
ones.

Both receive plain dicts:
  notify_action(action)       a queued action awaiting a human: the `actions`
                              row plus `key_name`, `summary` (rendered from
                              the manifest's summary_template with labels)
                              and `display_name` of the target
  notify_grant_request(grant) a pending expansion grant: id, key_id,
                              key_name, capabilities (JSON), reason,
                              expires_at, created_at
"""

from typing import Protocol


class Notifier(Protocol):
    def notify_action(self, action: dict) -> None: ...
    def notify_grant_request(self, grant: dict) -> None: ...
