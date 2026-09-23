"""Shared test helpers: manifests, a folder tree for subtree ancestry, and
direct DB setup the phase-1 identity code would normally do."""

import time
import uuid
from pathlib import Path

from broker import auth, db
from broker.authority.grant import Lattice
from broker.plugins.manifest import load_manifest

BROKER_DIR = Path(__file__).resolve().parents[2]
ECHO = load_manifest(BROKER_DIR / "tests" / "fixtures" / "echo" / "manifest.yaml")
WHATSAPP = load_manifest(BROKER_DIR / "broker" / "targets" / "whatsapp" / "manifest.yaml")
GITHUB = load_manifest(BROKER_DIR / "broker" / "targets" / "github" / "manifest.yaml")
ALL = (ECHO, WHATSAPP, GITHUB)

# Echo folder tree:  root -> a -> a1 -> a1x ; a -> a2 ; root -> b -> b1
PARENT = {"a": "root", "b": "root", "a1": "a", "a2": "a", "b1": "b", "a1x": "a1"}
FOLDERS = ("root", "a", "b", "a1", "a2", "b1", "a1x")


def tree_ancestors(kind: str, resource_id: str) -> list[str]:
    """Ancestors of an echo folder, nearest first (only for kind 'folder')."""
    if kind != "folder":
        return []
    out, cur = [], PARENT.get(resource_id)
    while cur is not None:
        out.append(cur)
        cur = PARENT.get(cur)
    return out


LATTICE = Lattice.from_manifests(ALL, tree_ancestors)
MANIFESTS = {m.id: m for m in ALL}


def insert_principal(username: str = "owner") -> str:
    pid = str(uuid.uuid4())
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO principals (id, username, password_hash, password_salt, created_at)"
            " VALUES (?, ?, 'x', 'x', ?)", (pid, username, int(time.time())))
    return pid


def make_key(principal_id: str, name: str | None = None, role: str = "full",
             rate_per_min: int = 60, expires_at: int | None = None,
             parent: int | None = None, denies: dict | None = None) -> auth.NewKey:
    return auth.create_key(principal_id, name or f"k-{uuid.uuid4().hex[:8]}", role,
                           rate_per_min, expires_at, parent_key_id=parent,
                           created_by="delegation" if parent else "owner", denies=denies)


def bearer(plaintext: str) -> str:
    return f"Bearer {plaintext}"


def all_on(*manifests):
    """plugin_states with every given plugin enabled and connected."""
    return [(m, True, True) for m in manifests]
