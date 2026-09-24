"""aab_plugin_google: the Google plugin service (gmail, gcal, gdrive).

Runs in its own container (plugin-google) behind `aab_plugin_runtime`. One
process hosts all three plugin ids because they share one Google account:
one OAuth client, one refresh token, one encrypted secret slot (`google`).
See docs/plugins/google.md.
"""

from .main import build_adapters, create_app

__all__ = ["build_adapters", "create_app"]
