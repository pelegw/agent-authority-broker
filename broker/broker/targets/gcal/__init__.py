"""Google Calendar: the broker's vendored manifest only.

The adapter and the google_oauth connection run in their own container,
plugins/google; plugins/google/aab_plugin_google/manifests/gcal.yaml is the
source of truth and this copy must stay byte-identical
(broker/tests/targets/test_google.py).
"""
