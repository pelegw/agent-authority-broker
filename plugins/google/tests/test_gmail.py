"""Gmail: every narrowing and constraint, both the allow and the refuse side.

The fake's threads.list ignores `q` and returns every thread, so each
filter here is proven on the post-filter, not on Google's search."""

import base64
import email

import pytest

from . import fake_google as fg
from .conftest import items, vis

G = "gmail"


def ids(response) -> list[str]:
    return [t["id"] for t in items(response)]


def last_q(google) -> str:
    return [q for m, p, q in google.requests if p.endswith("/users/me/threads")][-1].get("q", "")


def sent_message(google) -> email.message.Message:
    raw = google.gmail_writes[-1][2]
    raw = raw.get("message", raw)["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


# ---- scopes (target-enforced) ------------------------------------------------------

def test_a_read_refreshes_with_gmail_readonly_only(perform, google):
    assert perform(G, "search_threads").status_code == 200
    assert google.refreshes == [(fg.GM_RO,)]


@pytest.mark.parametrize("action,params,scopes", [
    ("send", {"to": ["alice@example.com"]}, {fg.GM_SEND, fg.GM_META}),
    ("create_draft", {"to": ["alice@example.com"]}, {fg.GM_COMPOSE, fg.GM_META}),
    ("archive_thread", {"thread_id": "aaa1"}, {fg.GM_MODIFY}),
    ("delete_thread", {"thread_id": "aaa1"}, {fg.GM_FULL}),
])
def test_each_write_mints_exactly_its_scopes(perform, google, action, params, scopes):
    assert perform(G, action, params).status_code == 200
    assert google.refreshes == [tuple(sorted(scopes))]


# ---- search: the default view --------------------------------------------------------

def test_search_without_restrictions_sees_everything(perform):
    assert ids(perform(G, "search_threads")) == ["aaa1", "bbb2", "ccc3", "ddd4", "eee5"]


def test_agent_query_is_grouped(perform, google):
    perform(G, "search_threads", {"query": "from:alice"})
    assert last_q(google) == "(from:alice)"


# ---- label (list, proxy): query terms AND post-filter ----------------------------------

def test_label_allow_is_injected_and_post_filtered(perform, google):
    r = perform(G, "search_threads", visibility={"label": vis(allow=["Label_work"])})
    assert "{label:work}" in last_q(google)
    # The fake returned all five; only threads carrying Work survive.
    assert ids(r) == ["aaa1", "ddd4", "eee5"]


def test_label_deny_is_injected_and_post_filtered(perform, google):
    r = perform(G, "search_threads", visibility={"label": vis(deny=["Label_secret"])})
    assert "-label:secret-stuff" in last_q(google)
    assert "ccc3" not in ids(r)


def test_empty_label_allow_means_nothing(perform, google):
    before = len(google.requests)
    assert ids(perform(G, "search_threads", visibility={"label": vis(allow=[])})) == []
    assert all("/threads" not in p for _, p, _ in google.requests[before:])


def test_get_thread_outside_the_labels_is_404(perform):
    v = {"label": vis(allow=["Label_work"])}
    assert perform(G, "get_thread", {"thread_id": "aaa1"}, v).status_code == 200
    r = perform(G, "get_thread", {"thread_id": "bbb2"}, v)
    assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_thread_with_a_hidden_label_is_404(perform):
    r = perform(G, "get_thread", {"thread_id": "ccc3"}, {"label": vis(deny=["Label_secret"])})
    assert r.status_code == 404


def test_list_labels_applies_deny_and_allow(perform):
    all_ids = [x["id"] for x in items(perform(G, "list_labels"))]
    assert "Label_secret" in all_ids
    denied = [x["id"] for x in items(perform(G, "list_labels",
                                             visibility={"label": vis(deny=["Label_secret"])}))]
    assert "Label_secret" not in denied
    only = [x["id"] for x in items(perform(G, "list_labels",
                                           visibility={"label": vis(allow=["Label_work"])}))]
    assert only == ["Label_work"]


# ---- contact (pattern, proxy): reads ------------------------------------------------------

def test_contact_allow_restricts_reads_to_threads_involving_them(perform, google):
    r = perform(G, "search_threads", visibility={"contact": vis(allow=["alice@example.com"])})
    assert ids(r) == ["aaa1", "ddd4", "eee5"]
    assert "from:alice@example.com" in last_q(google)


def test_contact_deny_hides_threads_with_that_participant(perform):
    v = {"contact": vis(deny=["MALLORY@evil.test"])}          # compared lowercased
    assert "eee5" not in ids(perform(G, "search_threads", visibility=v))
    assert perform(G, "get_thread", {"thread_id": "eee5"}, v).status_code == 404
    assert perform(G, "get_thread", {"thread_id": "aaa1"}, v).status_code == 200


# ---- contact + domain: sends ---------------------------------------------------------------

def test_send_to_an_allowed_contact(perform, google):
    r = perform(G, "send", {"to": ["Alice <ALICE@example.com>"], "subject": "Hi", "body": "b"},
                {"contact": vis(allow=["alice@example.com"])})
    assert r.status_code == 200 and r.json()["data"]["status"] == "sent"
    msg = sent_message(google)
    assert msg["To"] == "alice@example.com" and msg["Subject"] == "Hi"


def test_send_outside_the_contact_allow_set_is_403(perform, google):
    r = perform(G, "send", {"to": ["alice@example.com"], "cc": ["bob@other.org"]},
                visibility={"contact": vis(allow=["alice@example.com"])})
    assert r.status_code == 403 and "outside your grant" in r.json()["error"]
    assert google.gmail_writes == []


def test_send_to_a_denied_contact_is_404(perform, google):
    r = perform(G, "send", {"to": ["mallory@evil.test"]},
                visibility={"contact": vis(deny=["mallory@evil.test"])})
    assert r.status_code == 404 and google.gmail_writes == []


def test_denied_contact_outside_the_allow_set_looks_like_any_outsider(perform):
    v = {"contact": vis(deny=["mallory@evil.test"], allow=["alice@example.com"])}
    hidden = perform(G, "send", {"to": ["mallory@evil.test"]}, v)
    other = perform(G, "send", {"to": ["bob@other.org"]}, v)
    assert hidden.status_code == other.status_code == 403


def test_domain_allow_on_sends(perform, google):
    v = {"domain": vis(allow=["example.com"])}
    assert perform(G, "send", {"to": ["alice@example.com"]}, v).status_code == 200
    r = perform(G, "send", {"to": ["bob@other.org"]}, v)
    assert r.status_code == 403 and len(google.gmail_writes) == 1


def test_domain_deny_on_drafts(perform, google):
    r = perform(G, "create_draft", {"to": ["x@evil.test"]}, {"domain": vis(deny=["evil.test"])})
    assert r.status_code == 404 and google.gmail_writes == []


# ---- constraints ---------------------------------------------------------------------------

def test_date_window_is_injected_and_post_filtered(perform, google):
    r = perform(G, "search_threads", constraints={"date_window_days": 30})
    assert "newer_than:30d" in last_q(google)
    assert "ddd4" not in ids(r)                      # 100 days old
    assert perform(G, "get_thread", {"thread_id": "ddd4"},
                   constraints={"date_window_days": 30}).status_code == 404
    assert perform(G, "get_thread", {"thread_id": "ddd4"},
                   constraints={"date_window_days": 365}).status_code == 200


def test_attachments_false_strips_metadata_and_refuses_download(perform, google):
    body = perform(G, "get_thread", {"thread_id": "aaa1"},
                   constraints={"attachments": False}).json()["data"]
    assert "attachments" not in body["messages"][0]
    before = len(google.requests)
    r = perform(G, "get_attachment", {"thread_id": "aaa1", "message_id": "f011", "part_id": "1"},
                constraints={"attachments": False})
    assert r.status_code == 403
    assert all("gmail" not in p for _, p, _ in google.requests[before:])   # never fetched


def test_attachments_allowed(perform):
    body = perform(G, "get_thread", {"thread_id": "aaa1"}).json()["data"]
    [att] = body["messages"][0]["attachments"]
    assert att == {"part_id": "1", "filename": "report.pdf", "mime_type": "application/pdf",
                   "size": 11}
    r = perform(G, "get_attachment", {"thread_id": "aaa1", "message_id": "f011", "part_id": "1"})
    assert r.status_code == 200 and base64.b64decode(r.json()["binary_b64"]) == b"%PDF-report"
    assert r.json()["mime"] == "application/pdf"


def test_attachment_of_another_threads_message_is_404(perform):
    r = perform(G, "get_attachment", {"thread_id": "bbb2", "message_id": "f011", "part_id": "1"})
    assert r.status_code == 404


def test_bcc_false_refuses_bcc(perform, google):
    r = perform(G, "send", {"to": ["alice@example.com"], "bcc": ["boss@example.com"]},
                constraints={"bcc": False})
    assert r.status_code == 403 and google.gmail_writes == []
    ok = perform(G, "send", {"to": ["alice@example.com"], "bcc": ["boss@example.com"]})
    assert ok.status_code == 200 and sent_message(google)["Bcc"] == "boss@example.com"


def test_mark_read_false_refuses_read_state_changes(perform, google):
    r = perform(G, "label_thread", {"thread_id": "aaa1", "remove": ["UNREAD"]},
                constraints={"mark_read": False})
    assert r.status_code == 403 and google.gmail_writes == []
    ok = perform(G, "label_thread", {"thread_id": "aaa1", "remove": ["UNREAD"]})
    assert ok.status_code == 200
    assert google.gmail_writes[-1] == ("modify", "aaa1",
                                       {"addLabelIds": [], "removeLabelIds": ["UNREAD"]})


def test_reads_never_modify_labels(perform, google):
    perform(G, "search_threads")
    perform(G, "get_thread", {"thread_id": "aaa1"})
    perform(G, "get_attachment", {"thread_id": "aaa1", "message_id": "f011", "part_id": "1"})
    assert google.gmail_writes == []
    assert all(m == "GET" for m, p, _ in google.requests if "gmail" in p)


# ---- hidden == 404 ------------------------------------------------------------------------------

@pytest.mark.parametrize("action,params", [
    ("get_thread", {}), ("archive_thread", {}), ("trash_thread", {}), ("delete_thread", {}),
    ("label_thread", {"add": ["Label_work"]}),
    ("get_attachment", {"message_id": "f011", "part_id": "1"}),
    ("send", {"to": ["alice@example.com"]}),
])
def test_hidden_thread_is_404_and_never_fetched(perform, google, action, params):
    before = len(google.requests)
    r = perform(G, action, {**params, "thread_id": "aaa1"}, {"thread": vis(deny=["aaa1"])})
    missing = perform(G, action, {**params, "thread_id": "fff9"})
    assert r.status_code == missing.status_code == 404
    assert r.json() == missing.json() == {"error": "not found"}
    assert not any("aaa1" in p for _, p, _ in google.requests[before:])
    assert google.gmail_writes == []


def test_hidden_thread_absent_from_search(perform):
    assert "aaa1" not in ids(perform(G, "search_threads",
                                     visibility={"thread": vis(deny=["aaa1"])}))


# ---- replies ------------------------------------------------------------------------------------

def test_reply_threads_into_a_visible_thread(perform, google):
    r = perform(G, "send", {"to": ["alice@example.com"], "body": "ok", "thread_id": "aaa1"})
    assert r.status_code == 200
    msg = sent_message(google)
    assert msg["In-Reply-To"] == "<f011@mail.example.com>"
    assert msg["Subject"] == "Re: Quarterly report"
    assert google.gmail_writes[-1][2]["threadId"] == "aaa1"


def test_reply_into_a_thread_outside_the_labels_is_404(perform, google):
    r = perform(G, "send", {"to": ["bob@other.org"], "thread_id": "bbb2"},
                {"label": vis(allow=["Label_work"])})
    assert r.status_code == 404 and google.gmail_writes == []


def test_header_injection_is_refused(perform, google):
    r = perform(G, "send", {"to": ["alice@example.com"], "subject": "hi\r\nBcc: x@evil.test"})
    assert r.status_code == 400 and google.gmail_writes == []
    r = perform(G, "send", {"to": ["alice@example.com\r\nBcc: x@evil.test"]})
    assert r.status_code == 400


# ---- relabel ----------------------------------------------------------------------------------

def test_label_thread_inside_the_allow_set(perform, google):
    v = {"label": vis(allow=["Label_work", "Label_personal"])}
    ok = perform(G, "label_thread", {"thread_id": "aaa1", "add": ["Personal"]}, v)
    assert ok.status_code == 200
    assert google.gmail_writes[-1][2]["addLabelIds"] == ["Label_personal"]


def test_label_thread_outside_the_allow_set_is_403(perform, google):
    r = perform(G, "label_thread", {"thread_id": "aaa1", "add": ["Label_personal"]},
                {"label": vis(allow=["Label_work"])})
    assert r.status_code == 403 and google.gmail_writes == []


def test_label_thread_with_a_hidden_label_is_404(perform, google):
    r = perform(G, "label_thread", {"thread_id": "aaa1", "add": ["Label_secret"]},
                {"label": vis(deny=["Label_secret"])})
    assert r.status_code == 404 and google.gmail_writes == []


def test_label_thread_cannot_trash(perform, google):
    r = perform(G, "label_thread", {"thread_id": "aaa1", "add": ["TRASH"]})
    assert r.status_code == 400 and google.gmail_writes == []


# ---- normalize / resolve / label ------------------------------------------------------------

@pytest.mark.parametrize("kind,value,expect", [
    ("contact", "Alice <ALICE@Example.com>", "alice@example.com"),
    ("thread", " AAA1 ", "aaa1"),
    ("label", "work", "Label_work"),
    ("label", "inbox", "INBOX"),
    ("label", "Label_secret", "Label_secret"),
])
def test_normalize(connected, kind, value, expect):
    r = connected.post("/normalize", headers={"X-Plugin-Id": G},
                       json={"kind": kind, "value": value})
    assert r.status_code == 200 and r.json() == {"id": expect}


@pytest.mark.parametrize("kind,value,status", [
    ("contact", "not an address", 400), ("contact", "a@b.com, c@d.com", 400),
    ("thread", "zz-not-hex", 400), ("label", "No such label", 404)])
def test_normalize_refuses(connected, kind, value, status):
    r = connected.post("/normalize", headers={"X-Plugin-Id": G},
                       json={"kind": kind, "value": value})
    assert r.status_code == status


def test_resolve_and_label(connected):
    r = connected.post("/resolve", headers={"X-Plugin-Id": G},
                       json={"kind": "label", "query": "per"})
    assert r.json()["items"] == [{"id": "Label_personal", "label": "Personal", "kind": "label"}]
    r = connected.post("/label", headers={"X-Plugin-Id": G},
                       json={"kind": "thread", "ids": ["aaa1"]})
    assert r.json()["labels"] == {"aaa1": "Quarterly report"}


# ---- malformed scopes fail closed ---------------------------------------------------------------

@pytest.mark.parametrize("scope_patch", [
    {"visibility": {"label": {"deny": "Label_secret", "allow_only": None}}},
    {"visibility": {"label": {"deny": [], "allow_only": "Label_work"}}},
    {"constraints": {"date_window_days": "30"}},
    {"constraints": {"attachments": 0}},
    {"constraints": {"no_such_constraint": True}},
    {"visibility": "everything"},
])
def test_malformed_scope_is_400(connected, google, scope_patch):
    from .conftest import scope_for
    scope = {**scope_for(G, "search_threads"), **scope_patch}
    r = connected.post("/perform", headers={"X-Plugin-Id": G},
                       json={"action": "search_threads", "params": {}, "scope": scope})
    assert r.status_code == 400
    assert not any("/threads" in p for _, p, _ in google.requests)


def test_a_malformed_id_in_googles_listing_is_skipped(perform, google, monkeypatch):
    import httpx
    real = google._handle

    def handle(request):
        if request.url.path.endswith("/users/me/threads"):
            return httpx.Response(200, json={"threads": [{"id": "../labels"}, {"id": "aaa1"}]})
        return real(request)
    monkeypatch.setattr(google, "_handle", handle)
    assert ids(perform(G, "search_threads")) == ["aaa1"]
