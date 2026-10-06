"""Fetching a plugin repository: the allowlist, the ref rules, and the clone itself.

The installer clones exactly one kind of thing: an allowlisted repository at
a release tag (`vN.N.N`) or a full 40-hex commit, over https, one commit
deep. The commit it resolves is what the owner reviews at inspect time and
what install and upgrade must find again; a tag that moved in between fails
the job instead of installing something nobody reviewed.

Fail closed throughout:
  * an empty INSTALLER_ALLOWED_SOURCES allows nothing;
  * a pattern segment `*` matches exactly one path segment, never a `/`;
  * git runs with no system or global config, no terminal prompt, only the
    allowed transports (https in production; GIT_ALLOW_PROTOCOL), and
    `core.symlinks=false`, so a symlink in a repository is checked out as a
    plain file and can never point a read at the host's files;
  * a `vN.N.N` that resolves to a branch rather than a tag is refused.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from .fs import rmtree

HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                     r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
TAG_RE = re.compile(r"^v\d{1,6}\.\d{1,6}\.\d{1,6}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MIN_SEGMENTS, MAX_SEGMENTS = 2, 4         # owner/repo, or a group path (GitLab)
CLONE_TIMEOUT = 180


class GitError(Exception):
    """A refusal or failure with the HTTP status the installer answers."""

    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def normalize_source(raw: object) -> str:
    """`github.com/owner/repo` from what a person may paste (an https URL, a
    trailing `.git` or `/`). Raises GitError(400) for anything else."""
    if not isinstance(raw, str):
        raise GitError(400, "source must be a string like github.com/owner/repo", "bad_request")
    s = raw.strip()
    if s.startswith("https://"):
        s = s[len("https://"):]
    s = s.rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    parts = s.split("/")
    host, path = parts[0].lower(), parts[1:]
    if (not HOST_RE.match(host) or not MIN_SEGMENTS <= len(path) <= MAX_SEGMENTS
            or not all(SEGMENT_RE.match(p) and p not in (".", "..") and not p.endswith(".")
                       for p in path)):
        raise GitError(400, "source must look like github.com/owner/repo (https only, "
                            "no credentials, no query)", "bad_request")
    return "/".join([host, *path])


def parse_allowlist(raw: str) -> tuple[str, ...]:
    """INSTALLER_ALLOWED_SOURCES as patterns. Malformed entries are dropped
    (they could only ever match nothing); empty means allow nothing."""
    out = []
    for item in re.split(r"[,\s]+", raw or ""):
        item = item.strip().rstrip("/")
        if not item:
            continue
        parts = item.split("/")
        host, path = parts[0].lower(), parts[1:]
        if (HOST_RE.match(host) and MIN_SEGMENTS <= len(path) <= MAX_SEGMENTS
                and all(p == "*" or (SEGMENT_RE.match(p) and p not in (".", ".."))
                        for p in path)):
            out.append("/".join([host, *path]))
    return tuple(out)


def allowed(source: str, patterns: Sequence[str]) -> bool:
    """Does `source` (normalized) match one pattern? Host case-insensitive,
    path exact; `*` stands for exactly one segment."""
    s = source.split("/")
    for pattern in patterns:
        p = pattern.split("/")
        if len(p) == len(s) and p[0] == s[0].lower() and all(
                a == "*" or a == b for a, b in zip(p[1:], s[1:])):
            return True
    return False


def ref_kind(ref: object) -> str:
    """'tag' or 'commit'. Raises GitError(400) for any other ref (a branch
    can move under a reviewed install)."""
    if isinstance(ref, str) and TAG_RE.match(ref):
        return "tag"
    if isinstance(ref, str) and COMMIT_RE.match(ref):
        return "commit"
    raise GitError(400, "ref must be a release tag vN.N.N or a full 40-hex commit", "bad_request")


# (argv, cwd, env, timeout) -> CompletedProcess
GitRunner = Callable[[Sequence[str], Path | None, dict, float], subprocess.CompletedProcess]


def _run(argv: Sequence[str], cwd: Path | None, env: dict,
         timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout)


class Git:
    """Clones a source at a ref into a directory. `url_for` and `protocols`
    are the test seam (a local bare repository over file://); production
    is https only."""

    def __init__(self, url_for: Callable[[str], str] | None = None,
                 protocols: Sequence[str] = ("https",), runner: GitRunner | None = None,
                 timeout: float = CLONE_TIMEOUT):
        self._url_for = url_for or (lambda source: f"https://{source}.git")
        self._protocols = ":".join(protocols)
        self._run = runner or _run
        self._timeout = timeout

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")}
        env.update({
            "GIT_TERMINAL_PROMPT": "0",          # never wait for credentials
            "GIT_ALLOW_PROTOCOL": self._protocols,
            "GIT_CONFIG_NOSYSTEM": "1",          # no host or image config can change a clone
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
        })
        return env

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        argv = ["git", "-c", "core.symlinks=false", "-c", "advice.detachedHead=false",
                "-c", "core.hooksPath=" + os.devnull, *args]
        try:
            return self._run(argv, cwd, self._env(), self._timeout)
        except subprocess.TimeoutExpired as exc:
            raise GitError(502, "git timed out", "clone_failed") from exc
        except OSError as exc:
            raise GitError(503, "git is not available in the installer", "unavailable") from exc

    def fetch(self, source: str, ref: str, dest: Path) -> str:
        """Check out `source` at `ref` into `dest` (which must not exist) and
        return the resolved commit. Raises GitError; `dest` is removed on
        failure."""
        kind = ref_kind(ref)
        dest = Path(dest)
        url = self._url_for(source)
        try:
            if kind == "tag":
                r = self._git("clone", "--quiet", "--depth", "1", "--branch", ref,
                              "--single-branch", url, str(dest))
                self._check(r, f"could not fetch {source} at {ref}")
                tag = self._git("rev-parse", "--verify", "--quiet", f"refs/tags/{ref}^{{commit}}",
                                cwd=dest)
                if tag.returncode != 0:
                    raise GitError(400, f"{ref} is not a tag in {source}", "bad_request")
            else:
                dest.mkdir(parents=True)
                self._check(self._git("init", "--quiet", cwd=dest), "git init failed")
                r = self._git("fetch", "--quiet", "--depth", "1", url, ref, cwd=dest)
                self._check(r, f"could not fetch {source} at {ref}")
                self._check(self._git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=dest),
                            "checkout failed")
            head = self._git("rev-parse", "HEAD", cwd=dest)
            self._check(head, "rev-parse failed")
            commit = head.stdout.strip()
            if not COMMIT_RE.match(commit):
                raise GitError(502, "git returned no commit", "clone_failed")
            if kind == "commit" and commit != ref:
                raise GitError(502, "the fetched commit is not the one asked for", "clone_failed")
            return commit
        except BaseException:
            rmtree(dest)
            raise

    @staticmethod
    def _check(r: subprocess.CompletedProcess, message: str) -> None:
        if r.returncode != 0:
            # git's own words would name the URL and maybe more; the exit
            # code and our message are what the owner sees.
            raise GitError(502, f"{message} (git exit {r.returncode})", "clone_failed")
