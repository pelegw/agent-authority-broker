"""aab_plugin_github: the GitHub plugin service for the Agent Authority Broker.

Runs in its own container (plugin-github) behind `aab_plugin_runtime`. It
holds the GitHub App's private key and installation (or a PAT fallback) in
its own encrypted volume, and mints, per call, an installation token
narrowed to exactly the call's repository and permissions. The broker holds
no GitHub credential. See docs/plugins/github.md.
"""

from .adapter import GitHubAdapter
from .main import create_app

__all__ = ["GitHubAdapter", "create_app"]
