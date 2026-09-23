"""Agent Authority Broker: agents hold no credentials; the broker holds them
and decides, per call, what each agent's grant chain lets it do.

`__version__` is read from the repo-root VERSION file, which is the single
place the number lives. We walk up from this file so it works from a source
checkout, an editable install, and the Docker image (which copies VERSION in
next to the package). A built wheel has no VERSION file nearby, so we fall
back to the installed distribution's metadata.
"""

from pathlib import Path


def _read_version() -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "VERSION"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    try:
        from importlib.metadata import version
        return version("agent-authority-broker")
    except Exception:  # not installed either: never crash an import over this
        return "0+unknown"


__version__ = _read_version()
