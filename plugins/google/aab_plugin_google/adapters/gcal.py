"""The Calendar adapter: every action of manifests/gcal.yaml, inside the call's scope.

Target-enforced: reads run on a `calendar.readonly` token, writes on
`calendar.events` (the broker's requirements), so a read-only key's calls
cannot change a calendar at Google whatever this code did.

Proxy-enforced here: the calendar allow/deny sets (the alias `primary` is
resolved to the real id first, so it can never name a hidden calendar a
second way), the time window, private events, free/busy-only visibility
(gcal_events), attendee allow sets on create/update, and "only events the
account owner created" on update/delete (`others_events: false`).

Every event and calendar row carries `resource_ref: {"kind": "calendar"}`
so the broker's post-filter drops rows of a calendar the call may not see.
"""

import threading
from urllib.parse import quote

from aab_plugin_runtime import AdapterError, Result

from .. import ids
from ..callscope import CallScope
from ..client import CALENDAR
from . import gcal_events as ev
from .base import GoogleAdapter, boolean, integer, strings, text

READ = {"permissions": {"calendar.readonly": "read"}}
PRIMARY_CACHE_SECONDS = 600
MAX_FREEBUSY_CALENDARS = 20


def _cal(cid: str) -> str:
    return f"{CALENDAR}/calendars/{quote(cid, safe='')}"


class GcalAdapter(GoogleAdapter):
    plugin_id = "gcal"
    LOOKUP = READ
    RESOURCE_KINDS = ("calendar", "event")

    def __init__(self, connection, client, **kw):
        self._primary: tuple[float, str] | None = None
        self._primary_lock = threading.Lock()
        super().__init__(connection, client, **kw)

    def handlers(self) -> dict:
        return {"list_calendars": self._list_calendars, "list_events": self._list_events,
                "get_event": self._get_event, "freebusy": self._freebusy,
                "create_event": self._create_event, "update_event": self._update_event,
                "respond": self._respond, "delete_event": self._delete_event}

    # ---- calendars -----------------------------------------------------------------------

    def calendar_id(self, value: object) -> str:
        """Canonical calendar id; `primary` becomes the account's real id."""
        if isinstance(value, str) and value.strip().lower() == "primary":
            with self._primary_lock:
                hit = self._primary
                if hit and hit[0] > self.now():
                    return hit[1]
            body = self.client.request("GET", f"{CALENDAR}/users/me/calendarList/primary", READ)
            cid = ids.calendar_id(body.get("id"))
            with self._primary_lock:
                self._primary = (self.now() + PRIMARY_CACHE_SECONDS, cid)
            return cid
        return ids.calendar_id(value)

    def _calendars(self, req: dict) -> list[dict]:
        body = self.client.request("GET", f"{CALENDAR}/users/me/calendarList", req,
                                   params={"maxResults": 250})
        out = []
        for c in body.get("items") or []:
            if isinstance(c, dict) and isinstance(c.get("id"), str):
                try:
                    out.append({**c, "id": ids.calendar_id(c["id"])})
                except AdapterError:
                    continue                # an id we cannot canonicalize is not shown
        return out

    def _visible_calendar(self, raw: object, sv: CallScope) -> str:
        cid = self.calendar_id(raw)
        if not sv.vis("calendar").admits(cid):
            raise AdapterError(404, ev.NOT_FOUND)
        return cid

    # ---- resources ------------------------------------------------------------------------

    def normalize(self, kind: str, value: str) -> str:
        self._kind(kind)
        return self.calendar_id(value) if kind == "calendar" else ids.event_id(value)

    def resolve(self, kind: str, query: str, limit: int) -> list[dict]:
        self._kind(kind)
        if kind != "calendar":
            return []
        q = (query or "").strip().casefold()
        out = [{"id": c["id"], "label": str(c.get("summary") or c["id"]), "kind": "calendar"}
               for c in self._calendars(READ)
               if q in str(c.get("summary", "")).casefold() or q in c["id"]]
        return out[:max(1, min(int(limit), 50))]

    def label(self, kind: str, id_list: list[str]) -> dict[str, str]:
        self._kind(kind)
        if kind != "calendar":
            return {}                       # an event id alone does not name its calendar
        names = {c["id"]: str(c.get("summary") or c["id"]) for c in self._calendars(READ)}
        return {i: names[i] for i in id_list if i in names}

    # ---- reads ----------------------------------------------------------------------------

    def _list_calendars(self, params: dict, sv: CallScope) -> Result:
        cv = sv.vis("calendar")
        rows = [{"id": c["id"], "summary": c.get("summary", ""),
                 "primary": c.get("primary") is True, "access_role": c.get("accessRole", ""),
                 "resource_ref": {"kind": "calendar", "id": c["id"]}}
                for c in self._calendars(sv.requirements) if cv.admits(c["id"])]
        return Result(data={"items": rows})

    def _list_events(self, params: dict, sv: CallScope) -> Result:
        cid = self._visible_calendar(params.get("calendar_id", "primary"), sv)
        lo, hi = ev.clamp(sv, self.now(), text(params, "time_min"), text(params, "time_max"))
        if lo is not None and hi is not None and lo >= hi:
            return Result(data={"items": [], "next_page_token": None})
        body = self.client.request("GET", f"{_cal(cid)}/events", sv.requirements, params={
            "singleEvents": "true", "orderBy": "startTime",
            "timeMin": ev.rfc3339(lo) if lo else None, "timeMax": ev.rfc3339(hi) if hi else None,
            "q": text(params, "query") or None,
            "maxResults": integer(params, "limit", 50, 1, 250),
            "pageToken": text(params, "page_token")})
        rows = [ev.render(e, cid, sv) for e in body.get("items") or []
                if ev.event_visible(e, sv, self.now())]
        return Result(data={"items": rows, "next_page_token": body.get("nextPageToken")})

    def _event(self, params: dict, sv: CallScope) -> tuple[str, str, dict]:
        cid = self._visible_calendar(params.get("calendar_id", "primary"), sv)
        eid = ids.event_id(params.get("event_id"))
        if ev.event_hidden(eid, sv):
            raise AdapterError(404, ev.NOT_FOUND)    # never ask Google about a hidden id
        event = self.client.request("GET", f"{_cal(cid)}/events/{quote(eid, safe='')}",
                                    sv.requirements)
        return cid, eid, ev.require_visible(event, sv, self.now())

    def _get_event(self, params: dict, sv: CallScope) -> Result:
        cid, _, event = self._event(params, sv)
        return Result(data=ev.render(event, cid, sv))

    def _freebusy(self, params: dict, sv: CallScope) -> Result:
        raw = strings(params, "calendars", MAX_FREEBUSY_CALENDARS)
        if not raw:
            raise AdapterError(400, "calendars must name at least one calendar")
        cv = sv.vis("calendar")
        cids = []
        for value in raw:
            cid = self.calendar_id(value)
            cv.check_named(cid, f"calendar {cid}")
            cids.append(cid)
        lo, hi = ev.clamp(sv, self.now(), text(params, "time_min", required=True),
                          text(params, "time_max", required=True))
        cids = list(dict.fromkeys(cids))
        if lo >= hi:
            return Result(data={"calendars": [
                {"id": c, "busy": [], "resource_ref": {"kind": "calendar", "id": c}}
                for c in cids]})
        body = self.client.request("POST", f"{CALENDAR}/freeBusy", sv.requirements, json={
            "timeMin": ev.rfc3339(lo), "timeMax": ev.rfc3339(hi),
            "items": [{"id": c} for c in cids]})
        answered = {str(k).lower(): v for k, v in (body.get("calendars") or {}).items()}
        rows = []
        for c in cids:
            entry = answered.get(c) or {}
            busy = [{"start": b.get("start"), "end": b.get("end")}
                    for b in entry.get("busy") or [] if isinstance(b, dict)]
            rows.append({"id": c, "busy": busy, "resource_ref": {"kind": "calendar", "id": c}})
        return Result(data={"calendars": rows})

    # ---- writes ------------------------------------------------------------------------------

    def _attendees(self, params: dict, sv: CallScope) -> list[dict] | None:
        if params.get("attendees") is None:
            return None
        av = sv.vis("attendee")
        out = []
        for value in strings(params, "attendees", 100):
            addr = ids.email(value)
            av.check_named(addr, f"attendee {addr}")
            out.append({"email": addr})
        return out

    def _fields(self, params: dict, sv: CallScope, *, create: bool) -> dict:
        body: dict = {}
        for name in ("summary", "description", "location"):
            value = text(params, name, required=create and name == "summary", strip=False)
            if value is not None:
                body[name] = value
        tz = text(params, "time_zone")
        for name in ("start", "end"):
            if params.get(name) is not None or create:
                body[name] = ev.event_time(params.get(name))
                if tz and "dateTime" in body[name]:
                    body[name]["timeZone"] = tz
        attendees = self._attendees(params, sv)
        if attendees is not None:
            body["attendees"] = attendees
        return body

    @staticmethod
    def _updates(params: dict) -> str:
        return "all" if boolean(params, "notify_attendees") else "none"

    def _create_event(self, params: dict, sv: CallScope) -> Result:
        cid = self._visible_calendar(params.get("calendar_id", "primary"), sv)
        body = self._fields(params, sv, create=True)
        out = self.client.request("POST", f"{_cal(cid)}/events", sv.requirements, json=body,
                                  params={"sendUpdates": self._updates(params)})
        return Result(data={"status": "created", "id": out.get("id"),
                            "calendar_id": cid, "html_link": out.get("htmlLink")})

    def _own(self, event: dict, sv: CallScope) -> None:
        creator = event.get("creator")
        if not sv.flag("others_events") and not (isinstance(creator, dict)
                                                 and creator.get("self") is True):
            raise AdapterError(403, "your grant only covers events the account owner created")

    def _update_event(self, params: dict, sv: CallScope) -> Result:
        cid, eid, event = self._event(params, sv)
        self._own(event, sv)
        body = self._fields(params, sv, create=False)
        if not body:
            raise AdapterError(400, "name at least one field to change")
        self.client.request("PATCH", f"{_cal(cid)}/events/{quote(eid, safe='')}",
                            sv.requirements, json=body,
                            params={"sendUpdates": self._updates(params)})
        return Result(data={"status": "updated", "id": eid, "calendar_id": cid})

    def _respond(self, params: dict, sv: CallScope) -> Result:
        response = text(params, "response", required=True)
        if response not in ("accepted", "declined", "tentative"):
            raise AdapterError(400, "response must be accepted, declined or tentative")
        cid, eid, event = self._event(params, sv)
        attendees = [dict(a) for a in event.get("attendees") or [] if isinstance(a, dict)]
        mine = [a for a in attendees if a.get("self") is True]
        if not mine:
            raise AdapterError(409, "the account is not an attendee of this event")
        for a in mine:
            a["responseStatus"] = response
        self.client.request("PATCH", f"{_cal(cid)}/events/{quote(eid, safe='')}",
                            sv.requirements, json={"attendees": attendees},
                            params={"sendUpdates": "all"})
        return Result(data={"status": "responded", "response": response, "id": eid})

    def _delete_event(self, params: dict, sv: CallScope) -> Result:
        cid, eid, event = self._event(params, sv)
        self._own(event, sv)
        self.client.request("DELETE", f"{_cal(cid)}/events/{quote(eid, safe='')}",
                            sv.requirements, params={"sendUpdates": "none"})
        return Result(data={"status": "deleted", "id": eid, "calendar_id": cid})
