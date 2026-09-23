"""The owner account: who the broker acts for, and how they prove it.

`principals` (the owner row + password hashing), `setup` (one-time owner
creation), `sessions` (console login cookies), `admin_tokens` (aab_admin_
bearer tokens for the CLI and scripts) and `ratelimit` (failed-login throttle).
The request guard that ties them together is `deps.require_admin`.
"""
