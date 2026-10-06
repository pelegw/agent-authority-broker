"""Reading a checked-out plugin package: its descriptor, its manifests, its Dockerfile.

The installer runs as root with the gateway's `.env` (every service's
secrets) on the same filesystem, and `/inspect` returns file contents to the
broker. So a file is read from a checkout only when every component of its
path is a real directory or regular file inside the checkout (never a
symlink, even though git.py already checks symlinks out as plain files), and
only up to a size cap. Nothing from the repository is executed here.

The installer checks only what it needs (the descriptor fully, each manifest
as a YAML mapping whose `id` is the plugin id it is listed for); the broker
validates every manifest with its own strict schema before the owner can pin
it, and the pin, not the installer, is the authority decision.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

import yaml

from .descriptor import DESCRIPTOR_FILE, MAX_DESCRIPTOR_BYTES, Descriptor, DescriptorError, parse

MAX_MANIFEST_BYTES = 256 * 1024
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class PackageError(ValueError):
    """The checkout is not a valid plugin package; the message says why."""


@dataclass(frozen=True)
class Package:
    descriptor: Descriptor
    manifests: tuple[dict, ...]       # {"plugin", "path", "version", "text"}


def read_file(root: Path, rel: str, limit: int) -> bytes:
    """The bytes of `root/rel`, refusing symlinks, non-regular files, paths
    that leave `root`, and files over `limit` bytes."""
    root = Path(root)
    path = root
    st = None
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            raise PackageError(f"{rel}: not a plain relative path")
        path = path / part
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise PackageError(f"{rel} is missing from the repository") from None
        if stat.S_ISLNK(st.st_mode):
            raise PackageError(f"{rel} is (or passes through) a symlink")
    if st is None or not stat.S_ISREG(st.st_mode):
        raise PackageError(f"{rel} is not a regular file")
    if st.st_size > limit:
        raise PackageError(f"{rel} is larger than {limit} bytes")
    if not path.resolve().is_relative_to(root.resolve()):
        raise PackageError(f"{rel} is outside the repository")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise PackageError(f"{rel} is larger than {limit} bytes")
    return data


def read_package(root: Path) -> Package:
    """Validate the checkout at `root` as a plugin package."""
    try:
        d = parse(read_file(root, DESCRIPTOR_FILE, MAX_DESCRIPTOR_BYTES))
    except DescriptorError as exc:
        raise PackageError(str(exc)) from exc
    read_file(root, d.build.dockerfile, 1024 * 1024)       # present, regular, sane size
    manifests = []
    for pid, rel in zip(d.plugins, d.manifests):
        raw = read_file(root, rel, MAX_MANIFEST_BYTES)
        try:
            text = raw.decode("utf-8")
            data = yaml.safe_load(text)
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise PackageError(f"{rel} is not valid UTF-8 YAML") from exc
        if not isinstance(data, dict):
            raise PackageError(f"{rel} must be a mapping")
        if data.get("id") != pid:
            raise PackageError(f"{rel} declares id {data.get('id')!r}, but the descriptor "
                               f"lists it for {pid!r}")
        version = data.get("version")
        if not isinstance(version, str) or not VERSION_RE.match(version):
            raise PackageError(f"{rel}: version must be MAJOR.MINOR.PATCH")
        manifests.append({"plugin": pid, "path": rel, "version": version, "text": text})
    return Package(d, tuple(manifests))
