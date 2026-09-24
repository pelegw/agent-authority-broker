"""Canonical GitHub ids: repositories, branches, refs and file paths.

Hidden lists, grants and denies compare EXACT strings, so every id that
reaches a comparison must have exactly one spelling. Anything that GitHub
would resolve to the same object under a second spelling is either folded
into the canonical form or refused with 400; it is never passed through,
because a second name for a hidden repository would be a way around the
hide.

  repo    `owner/name`, lowercased. GitHub resolves both parts
          case-insensitively (`Octo/Hello` is `octo/hello`), so lowercasing
          the owner alone would leave `octo/Hello` as a second name for a
          hidden `octo/hello`. Only ASCII input is accepted *before*
          lowercasing: `str.lower()` folds some non-ASCII letters into ASCII
          ones (the Kelvin sign becomes `k`). Names ending in `.git` are
          refused (GitHub strips that suffix, which would be another alias).
  branch  exact and case-sensitive (git refs are), validated against
          git's check-ref-format rules plus two of ours: no `refs/` prefix
          and no `HEAD`, so a branch name can never be read as another ref.
  ref     the same rules without our two extras (tags, shas, `refs/...`).
  path    a path inside the repository; `.`/`..` segments, empty
          segments, backslashes and control characters are refused, and
          each segment is percent-encoded, so a path can never walk out of
          the contents API into another API route.

Every function raises AdapterError(400) with a message that never echoes
more than the parameter's name.
"""

import re
from urllib.parse import quote

from aab_plugin_runtime import AdapterError

# GitHub logins: alphanumerics and hyphens, not starting with a hyphen, at
# most 39 characters. Repository names: alphanumerics, '.', '-', '_'.
_OWNER_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,38}")
_NAME_RE = re.compile(r"[a-z0-9._-]{1,100}")
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REF_FORBIDDEN = set(" ~^:?*[\\")
MAX_REF = 250
MAX_PATH = 1024


def normalize_repo(value) -> str:
    """`owner/name` in canonical (lowercase) form, or AdapterError(400)."""
    if not isinstance(value, str):
        raise AdapterError(400, "repo must be a string")
    raw = value.strip()
    if not raw.isascii():
        raise AdapterError(400, "repo must be owner/name (ASCII)")
    parts = raw.lower().split("/")
    if len(parts) != 2:
        raise AdapterError(400, "repo must be owner/name")
    owner, name = parts
    if not _OWNER_RE.fullmatch(owner):
        raise AdapterError(400, "repo owner is not a valid GitHub login")
    if not _NAME_RE.fullmatch(name) or name in (".", "..") or name.endswith(".git"):
        raise AdapterError(400, "repo name is not a valid GitHub repository name")
    return f"{owner}/{name}"


def normalize_owner(value) -> str:
    """A GitHub login (user or organization) in canonical lowercase form."""
    if not isinstance(value, str) or not value.isascii():
        raise AdapterError(400, "owner must be a GitHub login")
    owner = value.strip().lower()
    if not _OWNER_RE.fullmatch(owner):
        raise AdapterError(400, "owner must be a GitHub login")
    return owner


def split_repo(repo_id: str) -> tuple[str, str]:
    """(owner, name) of an already-normalized id."""
    owner, name = repo_id.split("/", 1)
    return owner, name


def repo_path(repo_id: str) -> str:
    """`/repos/{owner}/{name}` for a normalized id (both parts are URL-safe
    by construction: the regexes above admit nothing that needs encoding)."""
    owner, name = split_repo(normalize_repo(repo_id))
    return f"/repos/{owner}/{name}"


def check_ref(value, name: str = "ref") -> str:
    """A git ref name (branch, tag, sha or refs/...), returned unchanged."""
    if not isinstance(value, str) or not value:
        raise AdapterError(400, f"{name} must be a non-empty string")
    if len(value) > MAX_REF:
        raise AdapterError(400, f"{name} is too long")
    bad = (
        any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)
        or any(c in _REF_FORBIDDEN for c in value)
        or ".." in value or "@{" in value or value == "@" or "//" in value
        or value.startswith(("/", "-")) or value.endswith(("/", "."))
        or any(seg.startswith(".") or seg.endswith(".lock") for seg in value.split("/"))
    )
    if bad:
        raise AdapterError(400, f"{name} is not a valid git ref name")
    return value


def check_branch(value, name: str = "branch") -> str:
    """A branch name, exact (no case folding: git branches are case-sensitive)."""
    value = check_ref(value, name)
    if value.startswith("refs/") or value == "HEAD":
        raise AdapterError(400, f"{name} must be a plain branch name")
    return value


def check_sha(value, name: str = "sha") -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise AdapterError(400, f"{name} must be a hex object sha")
    return value


def check_path(value, name: str = "path") -> str:
    """A repository-relative file path, returned unchanged."""
    if not isinstance(value, str) or not value:
        raise AdapterError(400, f"{name} must be a non-empty string")
    if len(value) > MAX_PATH:
        raise AdapterError(400, f"{name} is too long")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value) or "\\" in value:
        raise AdapterError(400, f"{name} contains characters a path cannot have")
    segments = value.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise AdapterError(400, f"{name} must be relative, without empty, '.' or '..' parts")
    return value


def encode_path(path: str) -> str:
    """Percent-encode each segment of a checked path ('/' stays a separator)."""
    return "/".join(quote(seg, safe="") for seg in check_path(path).split("/"))


def encode_ref(ref: str) -> str:
    """Percent-encode a checked ref for use in a URL path ('/' kept)."""
    return quote(check_ref(ref), safe="/")
