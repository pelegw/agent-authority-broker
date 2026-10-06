"""aab-installer: installs external broker plugins from their own git repositories.

A separate, opt-in container (docker-compose.installer.yml) that holds the
Docker socket, so it is root on the host. It is reachable only from the
broker (net_installer) and only with INSTALLER_TOKEN; it clones only
allowlisted sources at a tag or commit, renders each plugin's compose overlay
from the plugin's descriptor through a fixed template (overlay.py), and runs
`docker compose` for it. Nothing from a plugin repository runs on the host:
the plugin's Dockerfile runs inside `docker build`, like any image.

`__version__` is read from the repo-root VERSION file (the single place the
number lives), walking up from this file so it works from a checkout, an
editable install and the image; a built wheel falls back to its metadata.
"""

from pathlib import Path


def _read_version() -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "VERSION"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    try:
        from importlib.metadata import version
        return version("aab-installer")
    except Exception:  # not installed either: never crash an import over this
        return "0+unknown"


__version__ = _read_version()
