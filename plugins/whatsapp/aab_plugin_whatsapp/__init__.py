"""aab_plugin_whatsapp: the WhatsApp plugin service for the Agent Authority Broker.

Runs in its own container (plugin-whatsapp) behind `aab_plugin_runtime`:
the broker calls it over the internal plugin API; it reads the sidecar's
message archive read-only and drives the Go sidecar for sends, media and
pairing. See docs/plugins/whatsapp.md.
"""

from .adapter import WhatsAppAdapter
from .main import create_app

__all__ = ["WhatsAppAdapter", "create_app"]
