"""Strict parameter readers for the action handlers.

The broker validates params against the manifest before calling; these
checks repeat the parts a handler depends on, because the plugin API is a
trust boundary of its own (a bug or a hand-written request must get a 400,
never a half-understood call to GitHub).
"""

from typing import Any

from aab_plugin_runtime import AdapterError

from .api import GitHubError


def text(params: dict, name: str, *, required: bool = False, default: str | None = None,
         allow_empty: bool = False, max_len: int | None = None) -> str | None:
    value = params.get(name)
    if value is None:
        if required:
            raise AdapterError(400, f"{name} is required")
        return default
    if not isinstance(value, str):
        raise AdapterError(400, f"{name} must be a string")
    try:
        # JSON can carry lone surrogates, which cannot be encoded for GitHub;
        # failing later (mid-request) would misreport "not done" as unknown.
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise AdapterError(400, f"{name} is not valid text") from None
    if not allow_empty and not value.strip():
        raise AdapterError(400, f"{name} must not be empty")
    if max_len is not None and len(value) > max_len:
        raise AdapterError(400, f"{name} is too long")
    return value


def integer(params: dict, name: str, *, default: int | None = None, lo: int = 1,
            hi: int = 2**31 - 1, required: bool = False) -> int | None:
    value = params.get(name)
    if value is None:
        if required:
            raise AdapterError(400, f"{name} is required")
        return default
    # bool is an int subclass; True must not become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdapterError(400, f"{name} must be an integer")
    if not lo <= value <= hi:
        raise AdapterError(400, f"{name} must be between {lo} and {hi}")
    return value


def boolean(params: dict, name: str, *, default: bool) -> bool:
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise AdapterError(400, f"{name} must be true or false")
    return value


def choice(params: dict, name: str, options: tuple[str, ...], default: str) -> str:
    value = params.get(name, default)
    if value not in options:
        raise AdapterError(400, f"{name} must be one of {list(options)}")
    return value


def page_params(params: dict) -> tuple[int, int]:
    """(per_page, page) for GitHub list endpoints."""
    return (integer(params, "limit", default=30, lo=1, hi=100),
            integer(params, "page", default=1, lo=1, hi=100))


def as_dict(value: Any, what: str, *, before_effect: bool = False) -> dict:
    """A GitHub object we must read; anything else is an unreadable answer
    (unknown outcome, or plainly "not performed" for a lookup that precedes
    the action's side effect)."""
    if not isinstance(value, dict):
        raise GitHubError(503 if before_effect else 502, f"GitHub returned an unexpected {what}")
    return value


def as_list(value: Any, what: str) -> list:
    if not isinstance(value, list):
        raise GitHubError(502, f"GitHub returned an unexpected {what}")
    return value
