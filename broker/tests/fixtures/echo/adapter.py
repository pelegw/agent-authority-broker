"""The `echo` plugin adapter: an in-memory target for engine and runtime tests.

It is written against `aab_plugin_runtime` exactly like a real plugin, so the
same object runs in-process (broker `InProcessAdapter`) and behind the
plugin runtime over HTTP (`RemoteAdapter`), which is how the tests prove the
two paths behave the same.

Resources (all six narrowing forms are exercised by the manifest):
  rooms   r1..r4 (list dimension `room`)
  folders root -> a -> a1 -> a1x ; a -> a2 ; root -> b -> b1 (subtree `folder`)
  items   each lives in one room and one folder, has a sender (pattern `sender`)

It honours `scope["visibility"]` like a real adapter must (deny wins; a
denied get is a 404 identical to a missing one). Test hooks:
  fail_next       raise that status on the next perform (503 before any
                  side effect; 502 after it, like a timeout after sending)
  leaky           ignore visibility entirely, to prove the broker's own
                  post-filter catches what a buggy adapter returns
  calls           every perform as (action, params, scope)
"""

from pathlib import Path

import yaml
from aab_plugin_runtime import AdapterError, Result

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

PARENT = {"a": "root", "b": "root", "a1": "a", "a2": "a", "b1": "b", "a1x": "a1"}
FOLDERS = ("root", "a", "b", "a1", "a2", "b1", "a1x")
ROOMS = {"r1": "Room One", "r2": "Room Two", "r3": "Room Three", "r4": "Room Four"}


def _seed() -> dict[str, dict]:
    rows = [("i1", "r1", "a1", "s1", "hello from r1"),
            ("i2", "r2", "b1", "s2", "hello from r2"),
            ("i3", "r1", "a1x", "s2", "deep in a1x"),
            ("i4", "r3", "a2", "s1", "room three item")]
    return {i: {"id": i, "room": r, "folder": f, "sender": s, "text": t, "seq": n}
            for n, (i, r, f, s, t) in enumerate(rows, start=1)}


class EchoConnection:
    """Connection kind `none`: nothing to pair, always connected until
    `disconnect`; records what credential requirements it was asked to mint."""

    def __init__(self):
        self.connected = True
        self.minted: list[dict] = []

    def start(self, enabled_plugins: list[str]) -> dict:
        return {"kind": "none", "enabled_plugins": sorted(enabled_plugins)}

    def finish(self, code, state, installation_id) -> dict:
        self.connected = True
        return {"ok": True}

    def qr_png(self) -> bytes:
        raise AdapterError(404, "echo has no QR code")

    def disconnect(self) -> dict:
        self.connected = False
        return {"ok": True}

    def status(self) -> dict:
        return {"kind": "none", "connected": self.connected}

    def mint(self, requirements: dict):
        self.minted.append(dict(requirements or {}))
        return None


class EchoAdapter:
    def __init__(self, manifest_path: Path = MANIFEST_PATH):
        self.manifest = yaml.safe_load(Path(manifest_path).read_text(encoding="utf-8"))
        self.connection = EchoConnection()
        self.items = _seed()
        self.seq = max(i["seq"] for i in self.items.values())
        self.greeting = "hello"
        self._secrets = None
        self.fail_next: int | None = None
        self.leaky = False
        self.enforcement: str | None = None     # reported by status() when set
        self.calls: list[tuple[str, dict, dict]] = []

    # ---- lifecycle ---------------------------------------------------------

    def configure(self, config: dict, secrets) -> None:
        self.greeting = config.get("greeting", "hello")
        self._secrets = secrets

    def status(self) -> dict:
        out = {"connected": self.connection.connected, "healthy": True,
               "api_secret_set": bool(self._secrets and self._secrets.get("api_secret"))}
        if self.enforcement:
            out["enforcement"] = self.enforcement
        return out

    # ---- resources -----------------------------------------------------------

    def normalize(self, kind: str, value: str) -> str:
        v = value.strip()
        if kind == "room":
            v = v.lower()
            if not (len(v) >= 2 and v[0] == "r" and v[1:].isdigit()):
                raise AdapterError(400, f"not a room id: {value!r}")
            return v
        if kind in ("folder", "item"):
            if not v:
                raise AdapterError(400, f"empty {kind} id")
            return v
        raise AdapterError(400, f"unknown resource kind {kind!r}")

    def resolve(self, kind: str, query: str, limit: int) -> list[dict]:
        q = query.lower()
        if kind == "room":
            pool = ROOMS.items()
        elif kind == "folder":
            pool = ((f, f"Folder {f}") for f in FOLDERS)
        elif kind == "item":
            pool = ((i["id"], i["text"]) for i in self.items.values())
        else:
            raise AdapterError(400, f"unknown resource kind {kind!r}")
        out = [{"id": i, "label": label, "kind": kind} for i, label in pool
               if q in i.lower() or q in label.lower()]
        return out[:limit]

    def ancestors(self, kind: str, resource_id: str) -> list[str]:
        if kind != "folder":
            return []
        out, cur = [], PARENT.get(resource_id)
        while cur is not None:
            out.append(cur)
            cur = PARENT.get(cur)
        return out

    def label(self, kind: str, ids: list[str]) -> dict[str, str]:
        if kind == "room":
            return {i: ROOMS[i] for i in ids if i in ROOMS}
        if kind == "folder":
            return {i: f"Folder {i}" for i in ids if i in FOLDERS}
        if kind == "item":
            return {i: self.items[i]["text"] for i in ids if i in self.items}
        return {}

    # ---- visibility ------------------------------------------------------------

    @staticmethod
    def _vis(scope: dict, kind: str) -> tuple[set, set | None]:
        v = (scope.get("visibility") or {}).get(kind) or {}
        allow = v.get("allow_only")
        return set(v.get("deny") or ()), (set(allow) if allow is not None else None)

    def _folder_ok(self, folder: str, scope: dict) -> bool:
        deny, allow = self._vis(scope, "folder")
        chain = [folder, *self.ancestors("folder", folder)]
        if deny & set(chain):
            return False                     # hiding a folder hides its subtree
        return allow is None or bool(allow & set(chain))

    def _room_ok(self, room: str, scope: dict) -> bool:
        deny, allow = self._vis(scope, "room")
        return room not in deny and (allow is None or room in allow)

    def _visible(self, item: dict, scope: dict) -> bool:
        if self.leaky:
            return True
        item_deny, item_allow = self._vis(scope, "item")
        if item["id"] in item_deny or (item_allow is not None and item["id"] not in item_allow):
            return False
        sender_deny, sender_allow = self._vis(scope, "sender")
        if item["sender"] in sender_deny or (sender_allow is not None
                                             and item["sender"] not in sender_allow):
            return False
        return self._room_ok(item["room"], scope) and self._folder_ok(item["folder"], scope)

    def _get(self, item_id: str, scope: dict) -> dict:
        item = self.items.get(item_id)
        if item is None or not self._visible(item, scope):
            raise AdapterError(404, "no such item")     # hidden == missing
        return item

    @staticmethod
    def _row(item: dict, scope: dict) -> dict:
        row = {"id": item["id"], "room": item["room"], "folder": item["folder"],
               "sender": item["sender"], "resource_ref": {"kind": "item", "id": item["id"]}}
        if (scope.get("constraints") or {}).get("visibility") != "summary":
            row["text"] = item["text"]
        return row

    # ---- actions -----------------------------------------------------------------

    def perform(self, action: str, params: dict, scope: dict) -> Result:
        self.calls.append((action, dict(params), dict(scope)))
        self.connection.mint(scope.get("credential") or {})
        failure, self.fail_next = self.fail_next, None
        if failure is not None and failure != 502:
            raise AdapterError(failure, "injected failure")
        result = self._dispatch(action, params, scope)
        if failure == 502:
            # The side effect happened; the caller just never heard back.
            raise AdapterError(502, "injected unknown outcome")
        return result

    def _dispatch(self, action: str, params: dict, scope: dict) -> Result:
        if action == "list_items":
            rows = [i for i in sorted(self.items.values(), key=lambda i: i["seq"])
                    if ("room" not in params or i["room"] == params["room"])
                    and self._visible(i, scope)]
            return Result(data={"items": [self._row(i, scope)
                                          for i in rows[:params.get("limit", 20)]]})
        if action == "get_item":
            return Result(data=self._row(self._get(params["item_id"], scope), scope))
        if action == "get_blob":
            if (scope.get("constraints") or {}).get("attachments") is False:
                raise AdapterError(403, "attachments are not allowed")
            item = self._get(params["item_id"], scope)
            return Result(binary=f"blob:{item['id']}".encode(), mime="application/x-echo")
        if action == "watch":
            cursor = params.get("cursor")
            if cursor is None:
                return Result(data={"cursor": self.seq, "items": []})
            new = [self._row(i, scope) for i in sorted(self.items.values(),
                                                       key=lambda i: i["seq"])
                   if i["seq"] > cursor and self._visible(i, scope)]
            return Result(data={"cursor": self.seq, "items": new})
        if action == "post_item":
            room = params["room"]
            if not self.leaky and not self._room_ok(room, scope):
                raise AdapterError(404, "no such room")
            _, folder_allow = self._vis(scope, "folder")
            folder = sorted(folder_allow)[0] if folder_allow else "a"
            self.seq += 1
            new_id = f"i{self.seq}"
            self.items[new_id] = {"id": new_id, "room": room, "folder": folder,
                                  "sender": "echo", "text": params["text"], "seq": self.seq}
            return Result(data={"id": new_id})
        if action == "delete_item":
            item = self._get(params["item_id"], scope)
            del self.items[item["id"]]
            return Result(data={"deleted": item["id"]})
        if action == "touch_item":
            return Result(data={"touched": self._get(params["item_id"], scope)["id"]})
        raise AdapterError(404, f"unknown action {action!r}")
