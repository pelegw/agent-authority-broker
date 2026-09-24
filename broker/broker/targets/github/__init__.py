"""GitHub: the broker's vendored manifest only.

The adapter and the github_app connection (App key, installation, per-call
installation tokens) run in their own container, plugins/github; the
manifest there is the source of truth and this copy must stay byte-identical
(broker/tests/targets/test_github.py).
"""
