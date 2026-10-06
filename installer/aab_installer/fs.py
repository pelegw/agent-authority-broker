"""Two filesystem helpers the installer needs everywhere: an atomic write and a tree delete that works on git clones.

`write_atomic` matters because compose may read an overlay or the install
record at any moment (a deploy on the host, another job): a reader sees the
old file or the new one, never half of one. `rmtree` matters because git
marks pack files read-only, which a plain shutil.rmtree cannot delete on
Windows (a development machine); it clears the bit and retries.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Replace `path` with `data` in one step. World-readable by default: an
    overlay and an install record hold no secret, and the host user who runs
    `docker compose $(scripts/compose-files.sh)` must be able to read them
    (mkstemp alone would leave them 0600, root's)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _clear_readonly(func, path, _exc) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass                                  # best effort; the caller checks what remains


def rmtree(path: Path) -> None:
    """Delete `path` and everything below it, if it exists."""
    if os.path.lexists(path):
        shutil.rmtree(path, onexc=_clear_readonly)
