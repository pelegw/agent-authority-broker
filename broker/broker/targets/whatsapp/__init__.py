"""WhatsApp: the broker's vendored manifest only.

The adapter (JID rules, archive reader, sidecar client) runs in its own
container, plugins/whatsapp; the manifest there is the source of truth and
this copy must stay byte-identical (broker/tests/targets/test_whatsapp.py).
"""
