"""The Drive adapter: every action of manifests/gdrive.yaml, inside the call's scope.

Target-enforced: reads run on a `drive.readonly` token, writes on `drive`
(the broker's requirements). `drive.file` would not help: it reaches only
files this app created or the user opened with it, not "a folder and
everything under it".

Proxy-enforced here, on EVERY get, list, search and write, for every file
Google returns or the agent names (gdrive_tree.visible):
  * the folder subtree, walked by id through Drive's own `parents`
    (never a client-supplied path); a hidden file or folder, or anything
    under a hidden folder, is the same 404 as a missing one;
  * mime allow/deny lists (folders are governed by the subtree instead);
  * `shared_drives: false`: no `supportsAllDrives`, and shared-drive files
    dropped anyway (belt and braces);
  * `file_content: false` (metadata only), `max_download_mb`,
    `external_sharing: false`.
The aliases `root` (My Drive) is resolved to its real id before any
comparison, and shortcuts are never followed (a shortcut to a file outside
the subtree would otherwise be a way out of it).

List rows carry `resource_ref` (`folder` for folders, `file` otherwise),
so the broker's post-filter re-checks them.
"""

import base64
import binascii
import json
import secrets
import threading

from aab_plugin_runtime import AdapterError, Result

from .. import ids
from ..callscope import CallScope
from ..client import DRIVE, DRIVE_UPLOAD
from .base import GoogleAdapter, boolean, integer, text
from .gdrive_tree import FOLDER_MIME, Tree, visible

READ = {"permissions": {"drive.readonly": "read"}}
FIELDS = ("id,name,mimeType,parents,size,modifiedTime,driveId,trashed,webViewLink,"
          "owners(emailAddress)")
NATIVE_PREFIX = "application/vnd.google-apps."
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
EXPORT_MIME = "application/pdf"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MIB = 1024 * 1024
ALIAS_CACHE_SECONDS = 600
# Consumer accounts have no organization: nobody else is "internal".
CONSUMER_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
NOT_FOUND = "not found"


def _escape(value: str) -> str:
    """A Drive query string literal (single quotes, backslash escapes)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _drives(sv: CallScope) -> dict:
    # shared_drives false: never even ask Drive to include shared drives.
    return {"supportsAllDrives": "true"} if sv.flag("shared_drives") else {}


def row(meta: dict) -> dict:
    is_folder = meta.get("mimeType") == FOLDER_MIME
    size = meta.get("size")
    return {"id": meta["id"], "name": meta.get("name", ""),
            "mime_type": meta.get("mimeType", ""), "is_folder": is_folder,
            "size": int(size) if isinstance(size, str) and size.isdigit() else None,
            "modified_time": meta.get("modifiedTime"),
            "parents": [p for p in meta.get("parents") or [] if isinstance(p, str)],
            "shared_drive": bool(meta.get("driveId")), "trashed": meta.get("trashed") is True,
            "owners": [o.get("emailAddress", "") for o in meta.get("owners") or []
                       if isinstance(o, dict)],
            "web_view_link": meta.get("webViewLink"),
            "resource_ref": {"kind": "folder" if is_folder else "file", "id": meta["id"]}}


class GdriveAdapter(GoogleAdapter):
    plugin_id = "gdrive"
    LOOKUP = READ
    RESOURCE_KINDS = ("folder", "file")

    def __init__(self, connection, client, **kw):
        self._alias: dict[str, tuple[float, str]] = {}
        self._alias_lock = threading.Lock()
        super().__init__(connection, client, **kw)
        self.tree = Tree(client, self.now)

    def handlers(self) -> dict:
        return {"list_files": self._list_files, "search_files": self._search_files,
                "get_file_metadata": self._get_file_metadata,
                "download_file": self._download_file, "create_folder": self._create_folder,
                "upload_file": self._upload_file, "move_file": self._move_file,
                "share_file": self._share_file, "trash_file": self._trash_file,
                "delete_file": self._delete_file}

    # ---- ids ----------------------------------------------------------------------------

    def _cached(self, key: str, fetch) -> str:
        with self._alias_lock:
            hit = self._alias.get(key)
            if hit and hit[0] > self.now():
                return hit[1]
        value = fetch()
        with self._alias_lock:
            self._alias[key] = (self.now() + ALIAS_CACHE_SECONDS, value)
        return value

    def file_id(self, value: object) -> str:
        """Canonical id; `root` becomes My Drive's real id, so the alias can
        never name a hidden or out-of-grant folder a second way."""
        if isinstance(value, str) and value.strip().lower() == "root":
            return self._cached("root", lambda: ids.drive_id(self.client.request(
                "GET", f"{DRIVE}/files/root", READ, params={"fields": "id"}).get("id")))
        return ids.drive_id(value)

    def _owner_domain(self, req: dict) -> str | None:
        email = self._cached("owner", lambda: ids.email(
            (self.client.request("GET", f"{DRIVE}/about", req,
                                 params={"fields": "user(emailAddress)"}).get("user") or {})
            .get("emailAddress")))
        domain = ids.domain_of(email)
        return None if domain in CONSUMER_DOMAINS else domain

    # ---- resources ------------------------------------------------------------------------

    def normalize(self, kind: str, value: str) -> str:
        self._kind(kind)
        return self.file_id(value)

    def resolve(self, kind: str, query: str, limit: int) -> list[dict]:
        self._kind(kind)
        q = f"name contains '{_escape((query or '').strip())}' and trashed = false"
        if kind == "folder":
            q += f" and mimeType = '{FOLDER_MIME}'"
        body = self.client.request("GET", f"{DRIVE}/files", READ, params={
            "q": q, "fields": "files(id,name,mimeType)", "pageSize": max(1, min(int(limit), 50)),
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})
        return [{"id": f["id"], "label": f.get("name", f["id"]), "kind": kind}
                for f in body.get("files") or [] if isinstance(f, dict) and f.get("id")]

    def label(self, kind: str, id_list: list[str]) -> dict[str, str]:
        self._kind(kind)
        out = {}
        for fid in id_list[:20]:
            try:
                meta = self.client.request("GET", f"{DRIVE}/files/{ids.drive_id(fid)}", READ,
                                           params={"fields": "name",
                                                   "supportsAllDrives": "true"})
                out[fid] = str(meta.get("name", ""))
            except AdapterError:
                continue                    # a label is cosmetic
        return out

    def ancestors(self, kind: str, resource_id: str) -> list[str]:
        """For the broker's subtree lattice (nearest first). A lookup: runs on
        the read-only scope and sees every drive; it returns ids only."""
        self._kind(kind)
        chain, _ = self.tree.ancestors(self.file_id(resource_id), READ, True)
        return chain

    # ---- the one visibility gate ------------------------------------------------------------

    def _meta(self, raw_id: object, sv: CallScope, *, folder: bool | None = None) -> dict:
        fid = self.file_id(raw_id)
        if fid in sv.vis("file").deny or fid in sv.vis("folder").deny:
            raise AdapterError(404, NOT_FOUND)      # never ask Google about a hidden id
        meta = self.client.request("GET", f"{DRIVE}/files/{fid}", sv.requirements,
                                   params={"fields": FIELDS, **_drives(sv)})
        if meta.get("id") != fid or not visible(meta, sv, self.tree, sv.requirements):
            raise AdapterError(404, NOT_FOUND)
        if folder is True and meta.get("mimeType") != FOLDER_MIME:
            raise AdapterError(400, "that id is not a folder")
        return meta

    def _listing(self, q: str, params: dict, sv: CallScope, limit_default: int) -> Result:
        extra = {"includeItemsFromAllDrives": "true", "corpora": "allDrives"} \
            if sv.flag("shared_drives") else {}
        body = self.client.request("GET", f"{DRIVE}/files", sv.requirements, params={
            "q": q, "fields": f"nextPageToken,files({FIELDS})",
            "pageSize": integer(params, "limit", limit_default, 1, 100),
            "pageToken": text(params, "page_token"), **_drives(sv), **extra})
        rows = [row(f) for f in body.get("files") or []
                if visible(f, sv, self.tree, sv.requirements)]
        return Result(data={"items": rows, "next_page_token": body.get("nextPageToken")})

    def _mime_hint(self, sv: CallScope) -> str:
        allow = sv.vis("mime").allow
        if allow is None:
            return ""
        options = [f"mimeType = '{_escape(m)}'" for m in sorted(allow)] + \
                  [f"mimeType = '{FOLDER_MIME}'"]
        return " and (" + " or ".join(options) + ")"

    # ---- reads --------------------------------------------------------------------------------

    def _list_files(self, params: dict, sv: CallScope) -> Result:
        folder = self._meta(params.get("folder_id", "root"), sv, folder=True)
        q = f"'{folder['id']}' in parents and trashed = false" + self._mime_hint(sv)
        return self._listing(q, params, sv, 50)

    def _search_files(self, params: dict, sv: CallScope) -> Result:
        words = _escape(text(params, "query", required=True))
        clause = f"name contains '{words}'"
        if sv.flag("file_content"):
            clause += f" or fullText contains '{words}'"
        q = f"({clause}) and trashed = false" + self._mime_hint(sv)
        return self._listing(q, params, sv, 25)

    def _get_file_metadata(self, params: dict, sv: CallScope) -> Result:
        return Result(data=row(self._meta(params.get("file_id"), sv)))

    def _download_file(self, params: dict, sv: CallScope) -> Result:
        if not sv.flag("file_content"):
            raise AdapterError(403, "your grant covers file metadata only")
        meta = self._meta(params.get("file_id"), sv)
        mime = str(meta.get("mimeType", ""))
        if mime == FOLDER_MIME:
            raise AdapterError(400, "a folder has no content")
        if mime == SHORTCUT_MIME:
            raise AdapterError(400, "shortcuts are not followed; use the target file's id")
        mib = sv.bound("max_download_mb")
        limit = None if mib is None else mib * MIB
        size = meta.get("size")
        if limit is not None and isinstance(size, str) and size.isdigit() and int(size) > limit:
            raise AdapterError(403, f"the file is larger than your {mib} MiB download limit")
        url = f"{DRIVE}/files/{meta['id']}"
        if mime.startswith(NATIVE_PREFIX):
            data = self.client.request("GET", f"{url}/export", sv.requirements,
                                       params={"mimeType": EXPORT_MIME}, raw=True)
            out_mime = EXPORT_MIME
        else:
            data = self.client.request("GET", url, sv.requirements,
                                       params={"alt": "media", **_drives(sv)}, raw=True)
            out_mime = ids.safe_mime(mime)
        if limit is not None and len(data) > limit:
            # The declared size lied (or an export was bigger): still refused.
            raise AdapterError(403, f"the file is larger than your {mib} MiB download limit")
        return Result(binary=data, mime=out_mime)

    # ---- writes -------------------------------------------------------------------------------

    def _create_folder(self, params: dict, sv: CallScope) -> Result:
        parent = self._meta(params.get("parent_id", "root"), sv, folder=True)
        name = text(params, "name", required=True)
        out = self.client.request("POST", f"{DRIVE}/files", sv.requirements,
                                  params={"fields": "id,name", **_drives(sv)},
                                  json={"name": name, "mimeType": FOLDER_MIME,
                                        "parents": [parent["id"]]})
        return Result(data={"status": "created", "id": out.get("id"), "name": out.get("name")})

    def _upload_file(self, params: dict, sv: CallScope) -> Result:
        mime = ids.mime_type(params.get("mime_type"))
        if mime.startswith(NATIVE_PREFIX):
            raise AdapterError(400, "upload a regular file type, not a Google-native one")
        sv.vis("mime").check_named(mime, f"mime type {mime}")
        name = text(params, "name", required=True)
        try:
            data = base64.b64decode(text(params, "content_b64", required=True, strip=False),
                                    validate=True)
        except (binascii.Error, ValueError):
            raise AdapterError(400, "content_b64 is not valid base64") from None
        if len(data) > MAX_UPLOAD_BYTES:
            raise AdapterError(400, "uploads are limited to 10 MiB")
        parent = self._meta(params.get("parent_id", "root"), sv, folder=True)
        boundary = "aab-" + secrets.token_hex(16)
        meta = json.dumps({"name": name, "parents": [parent["id"]], "mimeType": mime})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{meta}\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + \
            data + f"\r\n--{boundary}--\r\n".encode()
        out = self.client.request(
            "POST", f"{DRIVE_UPLOAD}/files", sv.requirements, content=body,
            params={"uploadType": "multipart", "fields": "id,name", **_drives(sv)},
            headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return Result(data={"status": "uploaded", "id": out.get("id"), "name": out.get("name")})

    def _move_file(self, params: dict, sv: CallScope) -> Result:
        meta = self._meta(params.get("file_id"), sv)
        dest = self._meta(params.get("new_parent_id"), sv, folder=True)
        if dest["id"] == meta["id"]:
            raise AdapterError(400, "cannot move a folder into itself")
        old = ",".join(p for p in meta.get("parents") or [] if isinstance(p, str))
        self.client.request("PATCH", f"{DRIVE}/files/{meta['id']}", sv.requirements, json={},
                            params={"addParents": dest["id"], "removeParents": old or None,
                                    "fields": "id,parents", **_drives(sv)})
        return Result(data={"status": "moved", "id": meta["id"], "parent_id": dest["id"]})

    def _share_file(self, params: dict, sv: CallScope) -> Result:
        role = text(params, "role", required=True)
        kind = text(params, "type", required=True)
        if role not in ("reader", "commenter", "writer"):
            raise AdapterError(400, "role must be reader, commenter or writer")
        if kind not in ("user", "group", "domain", "anyone"):
            raise AdapterError(400, "type must be user, group, domain or anyone")
        permission: dict = {"role": role, "type": kind}
        principal_domain = None
        if kind in ("user", "group"):
            permission["emailAddress"] = ids.email(params.get("email_address"))
            principal_domain = ids.domain_of(permission["emailAddress"])
        elif kind == "domain":
            permission["domain"] = principal_domain = ids.domain(params.get("domain"))
        meta = self._meta(params.get("file_id"), sv)
        if not sv.flag("external_sharing"):
            if kind == "anyone":
                raise AdapterError(403, "sharing with anyone is not allowed by your grant")
            own = self._owner_domain(sv.requirements)
            if own is None or principal_domain != own:
                raise AdapterError(403, "sharing outside the account's own domain is not "
                                        "allowed by your grant")
        extra = {"sendNotificationEmail": "true" if boolean(params, "notify") else "false"} \
            if kind in ("user", "group") else {}
        out = self.client.request("POST", f"{DRIVE}/files/{meta['id']}/permissions",
                                  sv.requirements, json=permission,
                                  params={"fields": "id", **extra, **_drives(sv)})
        return Result(data={"status": "shared", "id": meta["id"], "permission_id": out.get("id")})

    def _trash_file(self, params: dict, sv: CallScope) -> Result:
        meta = self._meta(params.get("file_id"), sv)
        self.client.request("PATCH", f"{DRIVE}/files/{meta['id']}", sv.requirements,
                            json={"trashed": True}, params={"fields": "id", **_drives(sv)})
        return Result(data={"status": "trashed", "id": meta["id"]})

    def _delete_file(self, params: dict, sv: CallScope) -> Result:
        meta = self._meta(params.get("file_id"), sv)
        self.client.request("DELETE", f"{DRIVE}/files/{meta['id']}", sv.requirements,
                            params=_drives(sv))
        return Result(data={"status": "deleted", "id": meta["id"]})
