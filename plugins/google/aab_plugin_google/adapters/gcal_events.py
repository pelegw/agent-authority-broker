"""Calendar events: times, the visibility rules, and the shapes agents get.

An event is visible to a call iff:
  * its calendar is admitted (not hidden or denied; inside the allow set);
  * neither its id nor, for an instance of a recurring event, the recurring
    event's id is hidden or denied;
  * under `time_window_days` N it overlaps [now - N days, now + N days];
  * it is not private/confidential, unless `private_events` is true.
With `visibility: freebusy` a visible event is reduced to start, end and
busy; nothing else about it (not even its id) leaves the plugin.

Times the agent sends must carry a UTC offset (or be an all-day
YYYY-MM-DD date), so every window comparison is between absolute instants.
"""

from datetime import UTC, date, datetime, timedelta

from aab_plugin_runtime import AdapterError

from ..callscope import CallScope

NOT_FOUND = "not found"
PRIVATE = frozenset({"private", "confidential"})


def parse_instant(value: object, name: str) -> datetime:
    """RFC 3339 with an offset, or YYYY-MM-DD (midnight UTC)."""
    if not isinstance(value, str):
        raise AdapterError(400, f"{name} must be a string")
    v = value.strip()
    try:
        if len(v) == 10:
            return datetime.combine(date.fromisoformat(v), datetime.min.time(), UTC)
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise AdapterError(400, f"{name} is not an RFC 3339 time or YYYY-MM-DD date") from None
    if dt.tzinfo is None:
        raise AdapterError(400, f"{name} needs a UTC offset, e.g. Z or +03:00")
    return dt


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_time(value: object) -> dict:
    """An agent's start/end as the API's {dateTime} or {date} object."""
    v = value.strip() if isinstance(value, str) else ""
    parse_instant(v, "start/end")           # validates shape and offset
    return {"date": v} if len(v) == 10 else {"dateTime": v}


def window(sv: CallScope, now: float) -> tuple[datetime, datetime] | None:
    days = sv.bound("time_window_days")
    if days is None:
        return None
    center = datetime.fromtimestamp(now, UTC)
    return center - timedelta(days=days), center + timedelta(days=days)


def clamp(sv: CallScope, now: float, time_min: str | None,
          time_max: str | None) -> tuple[datetime | None, datetime | None]:
    """The agent's range cut to the time window. (None, None) = unbounded."""
    lo = parse_instant(time_min, "time_min") if time_min else None
    hi = parse_instant(time_max, "time_max") if time_max else None
    win = window(sv, now)
    if win is not None:
        lo = win[0] if lo is None or lo < win[0] else lo
        hi = win[1] if hi is None or hi > win[1] else hi
    return lo, hi


def _instant(field: object) -> datetime | None:
    if not isinstance(field, dict):
        return None
    try:
        if isinstance(field.get("dateTime"), str):
            return parse_instant(field["dateTime"], "event time")
        if isinstance(field.get("date"), str):
            return parse_instant(field["date"], "event date")
    except AdapterError:
        return None
    return None


def event_hidden(event_id: object, sv: CallScope) -> bool:
    return isinstance(event_id, str) and sv.vis("event").denied(event_id)


def event_visible(event: object, sv: CallScope, now: float) -> bool:
    if not isinstance(event, dict) or not isinstance(event.get("id"), str):
        return False
    if event_hidden(event["id"], sv) or event_hidden(event.get("recurringEventId"), sv):
        return False
    if str(event.get("visibility", "")).lower() in PRIVATE and not sv.flag("private_events"):
        return False
    win = window(sv, now)
    if win is not None:
        start, end = _instant(event.get("start")), _instant(event.get("end"))
        if start is None or end is None:
            return False                    # unknown times: outside any window
        if not (end > win[0] and start < win[1]):
            return False
    return True


def require_visible(event: object, sv: CallScope, now: float) -> dict:
    if not event_visible(event, sv, now):
        raise AdapterError(404, NOT_FOUND)
    return event


def _person(value: object) -> str:
    return str(value.get("email", "")).lower() if isinstance(value, dict) else ""


def render(event: dict, calendar_id: str, sv: CallScope) -> dict:
    ref = {"kind": "calendar", "id": calendar_id}
    if sv.level("visibility", "full") == "freebusy":
        return {"start": event.get("start"), "end": event.get("end"),
                "busy": event.get("transparency") != "transparent", "resource_ref": ref}
    return {
        "id": event.get("id"), "calendar_id": calendar_id,
        "summary": event.get("summary", ""), "description": event.get("description", ""),
        "location": event.get("location", ""), "start": event.get("start"),
        "end": event.get("end"), "status": event.get("status", ""),
        "visibility": event.get("visibility", "default"),
        "organizer": _person(event.get("organizer")), "creator": _person(event.get("creator")),
        "attendees": [{"email": _person(a), "response_status": a.get("responseStatus", "")}
                      for a in event.get("attendees") or [] if isinstance(a, dict)],
        "recurring_event_id": event.get("recurringEventId"),
        "resource_ref": ref,
    }
