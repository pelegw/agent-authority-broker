"""Calendar: every narrowing and constraint, both the allow and the refuse side.

The fake's events.list ignores timeMin/timeMax and returns every event of
the calendar, so the window and privacy rules are proven on the plugin's
own filter, not on Google's."""

import pytest

from . import fake_google as fg
from .conftest import items, vis

C = "gcal"
OWNER = fg.OWNER
TEAM = "team@group.calendar.google.com"
SECRET = "secret@group.calendar.google.com"


def event_ids(response) -> list[str]:
    return [e["id"] for e in items(response)]


def events_query(google) -> dict:
    return [q for m, p, q in google.requests if p.endswith("/events") and m == "GET"][-1]


# ---- scopes ------------------------------------------------------------------------------

def test_reads_use_calendar_readonly_and_writes_calendar_events(perform, google):
    perform(C, "list_events", {"calendar_id": OWNER})
    perform(C, "create_event", {"calendar_id": OWNER, "summary": "x",
                                "start": "2026-09-22T10:00:00Z", "end": "2026-09-22T11:00:00Z"})
    assert google.refreshes == [(fg.CAL_RO,), (fg.CAL_EVENTS,)]


# ---- calendar (list, proxy) ----------------------------------------------------------------

def test_list_calendars_applies_deny_and_allow(perform):
    assert [c["id"] for c in items(perform(C, "list_calendars"))] == [OWNER, TEAM, SECRET]
    assert [c["id"] for c in items(perform(C, "list_calendars",
                                           visibility={"calendar": vis(deny=[SECRET])}))] == [
        OWNER, TEAM]
    assert [c["id"] for c in items(perform(C, "list_calendars",
                                           visibility={"calendar": vis(allow=[TEAM])}))] == [TEAM]


def test_hidden_calendar_is_404_and_never_fetched(perform, google):
    before = len(google.requests)
    r = perform(C, "list_events", {"calendar_id": SECRET}, {"calendar": vis(deny=[SECRET])})
    assert r.status_code == 404 and r.json() == {"error": "not found"}
    assert not any("secret@" in p for _, p, _ in google.requests[before:])


def test_calendar_outside_the_allow_set_is_404(perform):
    v = {"calendar": vis(allow=[TEAM])}
    assert perform(C, "list_events", {"calendar_id": TEAM}, v).status_code == 200
    assert perform(C, "list_events", {"calendar_id": OWNER}, v).status_code == 404


def test_event_rows_carry_their_calendar_ref(perform):
    rows = items(perform(C, "list_events", {"calendar_id": TEAM}))
    assert rows[0]["resource_ref"] == {"kind": "calendar", "id": TEAM}


@pytest.mark.parametrize("value", ["primary", "PRIMARY", "Owner@Example.com"])
def test_primary_alias_normalizes_to_the_real_id(connected, value):
    r = connected.post("/normalize", headers={"X-Plugin-Id": C},
                       json={"kind": "calendar", "value": value})
    assert r.json() == {"id": OWNER}


def test_primary_cannot_bypass_a_hidden_calendar(perform):
    r = perform(C, "list_events", {"calendar_id": "primary"}, {"calendar": vis(deny=[OWNER])})
    assert r.status_code == 404


# ---- visibility (level freebusy < full) -----------------------------------------------------

def test_freebusy_visibility_returns_only_times(perform):
    rows = items(perform(C, "list_events", {"calendar_id": OWNER},
                         constraints={"visibility": "freebusy"}))
    assert rows and all(set(r) == {"start", "end", "busy", "resource_ref"} for r in rows)
    one = perform(C, "get_event", {"calendar_id": OWNER, "event_id": "e1"},
                  constraints={"visibility": "freebusy"}).json()["data"]
    assert "summary" not in one and one["busy"] is True


def test_full_visibility_returns_details(perform):
    one = perform(C, "get_event", {"calendar_id": OWNER, "event_id": "e1"},
                  constraints={"visibility": "full"}).json()["data"]
    assert one["summary"] == "Standup" and one["attendees"][1]["email"] == "alice@example.com"


# ---- time_window_days (range) ---------------------------------------------------------------

def test_time_window_bounds_the_query_and_the_results(perform, google):
    rows = event_ids(perform(C, "list_events", {"calendar_id": OWNER},
                             constraints={"time_window_days": 30, "private_events": True}))
    assert "e3" not in rows and "e1" in rows          # e3 is 60 days out
    q = events_query(google)
    assert q["timeMin"] == "2026-08-22T14:13:20Z" and q["timeMax"] == "2026-10-21T14:13:20Z"


def test_an_agent_range_is_clamped_to_the_window(perform, google):
    perform(C, "list_events", {"calendar_id": OWNER, "time_min": "2020-01-01T00:00:00Z",
                               "time_max": "2030-01-01T00:00:00Z"},
            constraints={"time_window_days": 7})
    q = events_query(google)
    assert q["timeMin"] == "2026-09-14T14:13:20Z" and q["timeMax"] == "2026-09-28T14:13:20Z"


def test_without_a_window_far_events_are_visible(perform):
    assert "e3" in event_ids(perform(C, "list_events", {"calendar_id": OWNER}))


def test_get_event_outside_the_window_is_404(perform):
    p = {"calendar_id": OWNER, "event_id": "e3"}
    assert perform(C, "get_event", p, constraints={"time_window_days": 30}).status_code == 404
    assert perform(C, "get_event", p, constraints={"time_window_days": 90}).status_code == 200


def test_times_need_an_offset(perform):
    r = perform(C, "list_events", {"calendar_id": OWNER, "time_min": "2026-09-22T10:00:00"})
    assert r.status_code == 400 and "offset" in r.json()["error"]


# ---- private_events (flag) --------------------------------------------------------------------

def test_private_events_hidden_when_false(perform):
    assert "e2" not in event_ids(perform(C, "list_events", {"calendar_id": OWNER},
                                         constraints={"private_events": False}))
    assert perform(C, "get_event", {"calendar_id": OWNER, "event_id": "e2"},
                   constraints={"private_events": False}).status_code == 404


def test_private_events_visible_when_allowed(perform):
    assert "e2" in event_ids(perform(C, "list_events", {"calendar_id": OWNER}))


def test_a_private_event_cannot_be_changed_when_hidden(perform, google):
    r = perform(C, "delete_event", {"calendar_id": OWNER, "event_id": "e2"},
                constraints={"private_events": False})
    assert r.status_code == 404 and google.calendar_writes == []


# ---- others_events (flag): own events only --------------------------------------------------

def test_update_of_someone_elses_event_is_refused(perform, google):
    r = perform(C, "update_event", {"calendar_id": OWNER, "event_id": "e4", "summary": "x"},
                constraints={"others_events": False})
    assert r.status_code == 403 and google.calendar_writes == []
    ok = perform(C, "update_event", {"calendar_id": OWNER, "event_id": "e1", "summary": "x"},
                 constraints={"others_events": False})
    assert ok.status_code == 200
    assert google.calendar_writes[-1][:4] == ("PATCH", OWNER, "e1", {"summary": "x"})


def test_delete_of_someone_elses_event(perform, google):
    p = {"calendar_id": OWNER, "event_id": "e4"}
    assert perform(C, "delete_event", p, constraints={"others_events": False}).status_code == 403
    assert perform(C, "delete_event", p).status_code == 200
    assert google.calendar_writes[-1][0] == "DELETE"


# ---- attendee (pattern, proxy) ------------------------------------------------------------------

def _create(perform, attendees, v=None):
    return perform(C, "create_event", {
        "calendar_id": OWNER, "summary": "Sync", "start": "2026-09-22T10:00:00Z",
        "end": "2026-09-22T10:30:00Z", "attendees": attendees}, v)


def test_attendee_allow_set_on_create(perform, google):
    v = {"attendee": vis(allow=["alice@example.com"])}
    assert _create(perform, ["Alice@Example.com"], v).status_code == 200
    body = google.calendar_writes[-1][3]
    assert body["attendees"] == [{"email": "alice@example.com"}]
    assert google.calendar_writes[-1][4]["sendUpdates"] == "none"
    r = _create(perform, ["alice@example.com", "bob@other.org"], v)
    assert r.status_code == 403 and len(google.calendar_writes) == 1


def test_denied_attendee_is_404(perform, google):
    r = _create(perform, ["mallory@evil.test"], {"attendee": vis(deny=["mallory@evil.test"])})
    assert r.status_code == 404 and google.calendar_writes == []


def test_attendee_allow_set_on_update(perform, google):
    r = perform(C, "update_event", {"calendar_id": OWNER, "event_id": "e1",
                                    "attendees": ["bob@other.org"]},
                {"attendee": vis(allow=["alice@example.com"])})
    assert r.status_code == 403 and google.calendar_writes == []


# ---- freebusy ------------------------------------------------------------------------------------

def test_freebusy_checks_every_calendar(perform):
    p = {"calendars": [TEAM, "primary"], "time_min": "2026-09-21T00:00:00Z",
         "time_max": "2026-09-30T00:00:00Z"}
    ok = perform(C, "freebusy", p)
    assert [c["id"] for c in ok.json()["data"]["calendars"]] == [TEAM, OWNER]
    assert perform(C, "freebusy", p, {"calendar": vis(allow=[TEAM])}).status_code == 403
    assert perform(C, "freebusy", {**p, "calendars": [SECRET]},
                   {"calendar": vis(deny=[SECRET])}).status_code == 404


# ---- respond -------------------------------------------------------------------------------------

def test_respond_sets_only_the_accounts_own_answer(perform, google):
    r = perform(C, "respond", {"calendar_id": OWNER, "event_id": "e4", "response": "accepted"})
    assert r.status_code == 200
    method, cal, eid, body, query = google.calendar_writes[-1]
    assert (method, eid, query["sendUpdates"]) == ("PATCH", "e4", "all")
    assert body["attendees"] == [{"email": OWNER, "self": True, "responseStatus": "accepted"}]


def test_respond_when_not_invited_is_409(perform):
    r = perform(C, "respond", {"calendar_id": OWNER, "event_id": "e3", "response": "declined"})
    assert r.status_code == 409


# ---- hidden events (by id; a recurring series hides its instances) ------------------------

def test_hidden_event_is_404_and_never_fetched(perform, google):
    v = {"event": vis(deny=["e1"])}
    before = len(google.requests)
    r = perform(C, "get_event", {"calendar_id": OWNER, "event_id": "e1"}, v)
    missing = perform(C, "get_event", {"calendar_id": OWNER, "event_id": "nosuch"})
    assert r.status_code == missing.status_code == 404 and r.json() == missing.json()
    assert not any(p.endswith("/events/e1") for _, p, _ in google.requests[before:])
    assert perform(C, "delete_event", {"calendar_id": OWNER, "event_id": "e1"},
                   v).status_code == 404
    assert google.calendar_writes == []
    assert "e1" not in event_ids(perform(C, "list_events", {"calendar_id": OWNER}, v))


def test_hiding_a_recurring_event_hides_its_instances(perform):
    assert "r1_20260925" in event_ids(perform(C, "list_events", {"calendar_id": OWNER}))
    v = {"event": vis(deny=["r1"])}
    assert "r1_20260925" not in event_ids(perform(C, "list_events", {"calendar_id": OWNER}, v))
    assert perform(C, "get_event", {"calendar_id": OWNER, "event_id": "r1_20260925"},
                   v).status_code == 404
