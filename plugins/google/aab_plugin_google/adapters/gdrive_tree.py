"""Drive folders as a subtree: the parent-chain walk and the file visibility rule.

The `folder` narrowing is a subtree: a file is inside when it or any
ancestor is one of the allowed folder ids, and hiding a folder hides
everything under it. The only trustworthy ancestry is Drive's own `parents`
field, walked by id one hop at a time; a path or a name an agent supplies is
never consulted. Parent lists are cached for 60 s per id (the plan's
ancestors cache), so a listing of fifty files in one folder costs one walk.

When a hop cannot be read (the account cannot see that parent, or it was
deleted), the chain is INCOMPLETE. An allow check then simply fails (the
allowed root was not found), and while any folder is hidden an incomplete
chain also counts as hidden: a hidden folder above the unreadable hop could
not be ruled out, and not ruling it out is exactly how a deny fails open.
"""

import threading
from collections.abc import Callable

from aab_plugin_runtime import AdapterError

from ..callscope import CallScope
from ..client import DRIVE

FOLDER_MIME = "application/vnd.google-apps.folder"
CACHE_SECONDS = 60
MAX_NODES = 200                         # a walk larger than this is incomplete


class Tree:
    def __init__(self, client, clock: Callable[[], float], ttl: int = CACHE_SECONDS):
        self._client = client
        self._clock = clock
        self._ttl = ttl
        self._parents: dict[str, tuple[float, tuple[str, ...]]] = {}
        self._lock = threading.Lock()

    def remember(self, meta: dict) -> None:
        """Cache the parents of a file whose metadata was just fetched anyway."""
        if isinstance(meta, dict) and isinstance(meta.get("id"), str):
            self._store(meta["id"], meta.get("parents"))

    def _store(self, file_id: str, parents: object) -> tuple[str, ...]:
        value = tuple(p for p in parents or [] if isinstance(p, str)) \
            if isinstance(parents, list) else ()
        with self._lock:
            self._parents[file_id] = (self._clock() + self._ttl, value)
        return value

    def parents(self, file_id: str, req: dict, all_drives: bool) -> tuple[str, ...] | None:
        """Parent ids of one file, or None when Drive will not say (404/403)."""
        with self._lock:
            hit = self._parents.get(file_id)
            if hit and hit[0] > self._clock():
                return hit[1]
        try:
            meta = self._client.request("GET", f"{DRIVE}/files/{file_id}", req, params={
                "fields": "id,parents", "supportsAllDrives": "true" if all_drives else None})
        except AdapterError as exc:
            if exc.status in (403, 404):
                return None                 # unreadable hop: not cached, chain incomplete
            raise
        return self._store(file_id, meta.get("parents"))

    def ancestors(self, file_id: str, req: dict, all_drives: bool) -> tuple[list[str], bool]:
        """(ancestor ids nearest first, complete?) for `file_id`."""
        first = self.parents(file_id, req, all_drives)
        if first is None:
            return [], False
        out: list[str] = []
        seen = {file_id}
        frontier = list(first)
        complete = True
        while frontier:
            node = frontier.pop(0)
            if node in seen:
                continue                    # a cycle or a diamond; walk each node once
            seen.add(node)
            out.append(node)
            if len(out) > MAX_NODES:
                return out, False
            ups = self.parents(node, req, all_drives)
            if ups is None:
                complete = False
                continue
            frontier.extend(ups)
        return out, complete


def visible(meta: object, sv: CallScope, tree: Tree, req: dict) -> bool:
    """May this call see (or act on) the file or folder `meta` describes?"""
    if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
        return False
    fid = meta["id"]
    if meta.get("driveId") and not sv.flag("shared_drives"):
        return False
    files, folders, mimes = sv.vis("file"), sv.vis("folder"), sv.vis("mime")
    hidden = files.deny | folders.deny
    if fid in hidden:
        return False
    is_folder = meta.get("mimeType") == FOLDER_MIME
    if not is_folder:
        # Folders are governed by the folder subtree, not by the mime list.
        mime = str(meta.get("mimeType", "")).lower()
        if not mimes.admits(mime):
            return False
    if not hidden and folders.allow is None:
        return True                         # nothing depends on ancestry
    tree.remember(meta)
    chain, complete = tree.ancestors(fid, req, sv.flag("shared_drives"))
    if hidden and (hidden & set(chain) or not complete):
        return False
    if folders.allow is not None and not ({fid} | set(chain)) & folders.allow:
        return False
    return True
