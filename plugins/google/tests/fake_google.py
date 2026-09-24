"""A scripted Google (OAuth, Gmail, Calendar, Drive) behind `httpx.MockTransport`.

Shared by this package's tests and the broker's end-to-end test
(broker/tests/targets/test_google.py loads this file by path), so both
suites exercise the same Google. The real connection and client run
unmodified against it and no socket is ever opened.

What makes it useful for an authority broker:
  * every API endpoint checks the bearer token's scopes against the scopes
    Google documents for that endpoint (SCOPES below) and answers 403
    insufficientPermissions otherwise, so a manifest whose
    target_permissions are too narrow fails a test, and a token that is
    too wide is visible in `refreshes`;
  * list endpoints IGNORE the search query (`q`) and time bounds and return
    everything: the worst case for the plugin, which must drop what the
    call may not see on its own (the query is recorded for assertions);
  * the token endpoint records every refresh's requested scope set.
"""

import base64
import json
import re
import time
from urllib.parse import parse_qs, unquote

import httpx

API = "https://www.googleapis.com/auth/"
GM_RO, GM_META, GM_COMPOSE = API + "gmail.readonly", API + "gmail.metadata", API + "gmail.compose"
GM_SEND, GM_MODIFY, GM_FULL = API + "gmail.send", API + "gmail.modify", "https://mail.google.com/"
CAL_RO, CAL_EVENTS = API + "calendar.readonly", API + "calendar.events"
DRIVE_RO, DRIVE = API + "drive.readonly", API + "drive"
ALL_SCOPES = [GM_RO, GM_META, GM_COMPOSE, GM_SEND, GM_MODIFY, GM_FULL, CAL_RO, CAL_EVENTS,
              DRIVE_RO, DRIVE]

CLIENT_ID = "1234-test.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-client-secret-DO-NOT-LEAK"
REFRESH_TOKEN = "1//refresh-token-DO-NOT-LEAK"
AUTH_CODE = "4/auth-code-DO-NOT-LEAK"
OWNER = "owner@example.com"
FOLDER = "application/vnd.google-apps.folder"
DAY_MS = 86_400_000

# Which scopes Google accepts per endpoint (subset of the real rules, as
# documented for each method).
SCOPES = {
    "gmail.labels": {GM_RO, GM_MODIFY, GM_META, GM_FULL},
    "gmail.threads.list": {GM_RO, GM_MODIFY, GM_FULL},
    "gmail.threads.get.full": {GM_RO, GM_MODIFY, GM_FULL},
    "gmail.threads.get.metadata": {GM_RO, GM_MODIFY, GM_META, GM_FULL},
    "gmail.messages.get": {GM_RO, GM_MODIFY, GM_FULL},
    "gmail.attachments.get": {GM_RO, GM_MODIFY, GM_FULL},
    "gmail.drafts.create": {GM_COMPOSE, GM_MODIFY, GM_FULL},
    "gmail.messages.send": {GM_SEND, GM_COMPOSE, GM_MODIFY, GM_FULL},
    "gmail.threads.modify": {GM_MODIFY, GM_FULL},
    "gmail.threads.trash": {GM_MODIFY, GM_FULL},
    "gmail.threads.delete": {GM_FULL},
    "calendar.list": {CAL_RO},
    "calendar.events.read": {CAL_RO, CAL_EVENTS},
    "calendar.freebusy": {CAL_RO},
    "calendar.events.write": {CAL_EVENTS},
    "drive.read": {DRIVE_RO, DRIVE},
    "drive.write": {DRIVE},
}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _msg(mid, tid, labels, frm, to, subject, days_ago, now_ms, body="hello",
         cc="", attachment=None):
    parts = [{"partId": "0", "mimeType": "text/plain", "filename": "",
              "body": {"size": len(body), "data": _b64(body.encode())}}]
    if attachment:
        parts.append({"partId": "1", "mimeType": attachment[1], "filename": attachment[0],
                      "body": {"size": len(attachment[2]), "attachmentId": f"att-{mid}"}})
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to},
               {"name": "Subject", "value": subject},
               {"name": "Date", "value": "Mon, 21 Sep 2026 10:00:00 +0000"},
               {"name": "Message-ID", "value": f"<{mid}@mail.example.com>"}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    return {"id": mid, "threadId": tid, "labelIds": labels, "snippet": body[:40],
            "internalDate": str(now_ms - days_ago * DAY_MS),
            "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}}


class FakeGoogle:
    def __init__(self, now: float | None = None):
        self.now_ms = int((now if now is not None else time.time()) * 1000)
        self.requests: list[tuple[str, str, dict]] = []     # method, path, query
        self.token_forms: list[dict] = []
        self.refreshes: list[tuple[str, ...]] = []          # requested scope sets
        self.granted = list(ALL_SCOPES)
        self.expected_redirect: str | None = None
        self.widen = False             # refreshes return every granted scope
        self.omit_refresh_token = False
        self.revoked: list[str] = []
        self.tokens: dict[str, set[str]] = {}
        self.fail_next: dict[str, object] = {}              # key -> status | exception
        self.gmail_writes: list[tuple[str, str, dict]] = []
        self.calendar_writes: list[tuple[str, str, dict, dict]] = []
        self.drive_writes: list[tuple[str, str, dict, object]] = []
        self._seed()

    # ---- data ------------------------------------------------------------------------

    def _seed(self):
        n = self.now_ms
        self.labels = [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "UNREAD", "name": "UNREAD", "type": "system"},
            {"id": "TRASH", "name": "TRASH", "type": "system"},
            {"id": "Label_work", "name": "Work", "type": "user"},
            {"id": "Label_secret", "name": "Secret Stuff", "type": "user"},
            {"id": "Label_personal", "name": "Personal", "type": "user"},
        ]
        self.threads = {
            "aaa1": [_msg("f011", "aaa1", ["INBOX", "Label_work", "UNREAD"],
                          "Alice <alice@example.com>", OWNER, "Quarterly report", 1, n,
                          body="Numbers attached.",
                          attachment=("report.pdf", "application/pdf", b"%PDF-report"))],
            "bbb2": [_msg("f021", "bbb2", ["INBOX", "Label_personal"], "bob@other.org", OWNER,
                          "Dinner", 2, n)],
            "ccc3": [_msg("f031", "ccc3", ["Label_secret"], "carol@example.com", OWNER,
                          "Secret plans", 1, n)],
            "ddd4": [_msg("f041", "ddd4", ["Label_work"], "alice@example.com", OWNER,
                          "Old thread", 100, n)],
            "eee5": [_msg("f051", "eee5", ["INBOX", "Label_work"], "alice@example.com", OWNER,
                          "Group note", 1, n, cc="Mallory <mallory@evil.test>")],
        }
        self.attachments = {"att-f011": b"%PDF-report"}
        self.calendars = [
            {"id": OWNER, "summary": "Owner", "primary": True, "accessRole": "owner"},
            {"id": "team@group.calendar.google.com", "summary": "Team", "accessRole": "writer"},
            {"id": "secret@group.calendar.google.com", "summary": "Secret",
             "accessRole": "owner"},
        ]
        day = 86400
        now = n / 1000

        def ev(eid, start_days, hours=1, **kw):
            s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + start_days * day))
            e = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(now + start_days * day + hours * 3600))
            return {"id": eid, "summary": kw.pop("summary", eid), "start": {"dateTime": s},
                    "end": {"dateTime": e}, "status": "confirmed",
                    "creator": kw.pop("creator", {"email": OWNER, "self": True}),
                    "organizer": {"email": OWNER, "self": True}, **kw}

        self.events = {
            OWNER: {
                "e1": ev("e1", 1, summary="Standup",
                         attendees=[{"email": OWNER, "self": True, "responseStatus": "accepted"},
                                    {"email": "alice@example.com",
                                     "responseStatus": "accepted"}]),
                "e2": ev("e2", 2, summary="Doctor", visibility="private"),
                "e3": ev("e3", 60, summary="Far away"),
                "r1_20260925": ev("r1_20260925", 4, summary="Weekly 1:1",
                                  recurringEventId="r1"),
                "e4": ev("e4", 3, summary="Their meeting",
                         creator={"email": "alice@example.com"},
                         organizer={"email": "alice@example.com"},
                         attendees=[{"email": OWNER, "self": True,
                                     "responseStatus": "needsAction"}]),
            },
            "team@group.calendar.google.com": {"t1": ev("t1", 1, summary="Team sync")},
            "secret@group.calendar.google.com": {"s1": ev("s1", 1, summary="Hidden")},
        }
        self.files = {
            "rootid": {"id": "rootid", "name": "My Drive", "mimeType": FOLDER},
            "fA": {"id": "fA", "name": "A", "mimeType": FOLDER, "parents": ["rootid"]},
            "fA1": {"id": "fA1", "name": "A1", "mimeType": FOLDER, "parents": ["fA"]},
            "fB": {"id": "fB", "name": "B", "mimeType": FOLDER, "parents": ["rootid"]},
            "doc1": {"id": "doc1", "name": "plan.pdf", "mimeType": "application/pdf",
                     "parents": ["fA1"], "size": "1000"},
            "img1": {"id": "img1", "name": "photo.png", "mimeType": "image/png",
                     "parents": ["fB"], "size": str(2 * 1024 * 1024)},
            "gdoc1": {"id": "gdoc1", "name": "Notes", "parents": ["fA"],
                      "mimeType": "application/vnd.google-apps.document"},
            "html1": {"id": "html1", "name": "page.html", "mimeType": "text/html",
                      "parents": ["fA"], "size": "20"},
            "sc1": {"id": "sc1", "name": "link", "parents": ["fA"],
                    "mimeType": "application/vnd.google-apps.shortcut"},
            "sdroot": {"id": "sdroot", "name": "Team drive", "mimeType": FOLDER,
                       "driveId": "drv1"},
            "shared1": {"id": "shared1", "name": "team.pdf", "mimeType": "application/pdf",
                        "parents": ["sdroot"], "driveId": "drv1", "size": "10"},
            "orphan1": {"id": "orphan1", "name": "from-a-friend.pdf",
                        "mimeType": "application/pdf", "parents": ["noaccess"], "size": "10"},
        }
        self.content = {"doc1": b"%PDF-plan", "img1": b"\x89PNG" + b"0" * 32,
                        "html1": b"<script>x</script>", "shared1": b"%PDF-team",
                        "orphan1": b"%PDF-friend"}

    # ---- transport ---------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        # Looked up per request, so a test can wrap `_handle` after the
        # adapters were built.
        return httpx.MockTransport(lambda request: self._handle(request))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        query = {k: v if len(v) > 1 else v[0]
                 for k, v in parse_qs(request.url.query.decode(), keep_blank_values=True).items()}
        path = unquote(request.url.path)
        self.requests.append((request.method, f"{request.url.host}{path}", query))
        for key in list(self.fail_next):
            if key in f"{request.method} {request.url.host}{path}":
                outcome = self.fail_next.pop(key)
                if isinstance(outcome, int):
                    return _json(outcome, {"error": {"code": outcome, "message": "injected"}})
                raise outcome("injected", request=request)
        host = request.url.host
        if host == "oauth2.googleapis.com":
            return self._oauth(request, path)
        if host == "gmail.googleapis.com":
            return self._gmail(request, path, query)
        if path.startswith("/calendar/v3"):
            return self._calendar(request, path[len("/calendar/v3"):], query)
        if path.startswith("/drive/v3") or path.startswith("/upload/drive/v3"):
            return self._drive(request, path.split("/v3", 1)[1], query)
        return _json(404, {"error": {"code": 404, "message": "no such endpoint"}})

    # ---- OAuth ---------------------------------------------------------------------------

    def _oauth(self, request, path):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if path == "/revoke":
            self.revoked.append(form.get("token", ""))
            return _json(200, {})
        self.token_forms.append(form)
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return _json(401, {"error": "invalid_client"})
        grant = form.get("grant_type")
        if grant == "authorization_code":
            if form.get("code") != AUTH_CODE or (
                    self.expected_redirect and form.get("redirect_uri") != self.expected_redirect):
                return _json(400, {"error": "invalid_grant"})
            body = {"access_token": self._issue(set(self.granted)), "expires_in": 3599,
                    "scope": " ".join(self.granted), "token_type": "Bearer"}
            if not self.omit_refresh_token:
                body["refresh_token"] = REFRESH_TOKEN
            return _json(200, body)
        if grant == "refresh_token":
            if form.get("refresh_token") != REFRESH_TOKEN or REFRESH_TOKEN in self.revoked:
                return _json(400, {"error": "invalid_grant"})
            asked = set(form.get("scope", "").split())
            if not asked <= set(self.granted):
                return _json(400, {"error": "invalid_scope"})
            self.refreshes.append(tuple(sorted(asked)))
            scopes = set(self.granted) if self.widen else asked
            return _json(200, {"access_token": self._issue(scopes), "expires_in": 3599,
                               "scope": " ".join(sorted(scopes)), "token_type": "Bearer"})
        return _json(400, {"error": "unsupported_grant_type"})

    def _issue(self, scopes: set[str]) -> str:
        token = f"ya29.fake-access-{len(self.tokens) + 1}-DO-NOT-LEAK"
        self.tokens[token] = set(scopes)
        return token

    def _allowed(self, request, rule) -> bool:
        auth = request.headers.get("authorization", "")
        scopes = self.tokens.get(auth.removeprefix("Bearer "), set())
        return bool(scopes & SCOPES[rule])

    # ---- Gmail ------------------------------------------------------------------------------

    def _gmail(self, request, path, q):
        p = path.removeprefix("/gmail/v1/users/me")
        m = request.method

        def need(rule):
            return None if self._allowed(request, rule) else _insufficient()

        if m == "GET" and p == "/labels":
            return need("gmail.labels") or _json(200, {"labels": self.labels})
        if m == "GET" and p == "/threads":
            return need("gmail.threads.list") or _json(200, {"threads": [
                {"id": t, "snippet": ""} for t in self.threads]})
        mt = re.fullmatch(r"/threads/([^/]+)", p)
        if m == "GET" and mt:
            fmt = q.get("format", "full")
            denied = need(f"gmail.threads.get.{'metadata' if fmt == 'metadata' else 'full'}")
            if denied:
                return denied
            msgs = self.threads.get(mt.group(1))
            if msgs is None:
                return _json(404, {"error": {"code": 404, "message": "Not Found"}})
            return _json(200, {"id": mt.group(1), "messages": [
                _format(x, fmt, q.get("metadataHeaders")) for x in msgs]})
        mm = re.fullmatch(r"/messages/([^/]+)", p)
        if m == "GET" and mm:
            denied = need("gmail.messages.get")
            if denied:
                return denied
            for msgs in self.threads.values():
                for x in msgs:
                    if x["id"] == mm.group(1):
                        return _json(200, x)
            return _json(404, {"error": {"code": 404, "message": "Not Found"}})
        ma = re.fullmatch(r"/messages/([^/]+)/attachments/([^/]+)", p)
        if m == "GET" and ma:
            denied = need("gmail.attachments.get")
            data = self.attachments.get(ma.group(2))
            return denied or (_json(200, {"size": len(data), "data": _b64(data)}) if data
                              else _json(404, {"error": {"code": 404}}))
        body = json.loads(request.content or b"{}")
        if m == "POST" and p == "/drafts":
            denied = need("gmail.drafts.create")
            if denied:
                return denied
            self.gmail_writes.append(("draft", "", body))
            return _json(200, {"id": "draft-1", "message": {
                "id": "m-draft", "threadId": body["message"].get("threadId", "new-thread")}})
        if m == "POST" and p == "/messages/send":
            denied = need("gmail.messages.send")
            if denied:
                return denied
            self.gmail_writes.append(("send", "", body))
            return _json(200, {"id": "m-sent", "threadId": body.get("threadId", "new-thread")})
        mw = re.fullmatch(r"/threads/([^/]+)/(modify|trash)", p)
        if m == "POST" and mw:
            denied = need(f"gmail.threads.{mw.group(2)}")
            if denied:
                return denied
            self.gmail_writes.append((mw.group(2), mw.group(1), body))
            return _json(200, {"id": mw.group(1)})
        if m == "DELETE" and mt:
            denied = need("gmail.threads.delete")
            if denied:
                return denied
            self.gmail_writes.append(("delete", mt.group(1), {}))
            self.threads.pop(mt.group(1), None)
            return httpx.Response(204)
        return _json(404, {"error": {"code": 404, "message": "no such Gmail endpoint"}})

    # ---- Calendar ------------------------------------------------------------------------------

    def _calendar(self, request, p, q):
        m = request.method

        def need(rule):
            return None if self._allowed(request, rule) else _insufficient()

        if m == "GET" and p == "/users/me/calendarList":
            return need("calendar.list") or _json(200, {"items": self.calendars})
        if m == "GET" and p == "/users/me/calendarList/primary":
            return need("calendar.list") or _json(200, self.calendars[0])
        if m == "POST" and p == "/freeBusy":
            denied = need("calendar.freebusy")
            if denied:
                return denied
            body = json.loads(request.content)
            return _json(200, {"calendars": {i["id"]: {"busy": [
                {"start": e["start"]["dateTime"], "end": e["end"]["dateTime"]}
                for e in self.events.get(i["id"], {}).values()]} for i in body["items"]}})
        mc = re.fullmatch(r"/calendars/([^/]+)/events(?:/([^/]+))?", p)
        if not mc:
            return _json(404, {"error": {"code": 404}})
        cal, eid = mc.group(1), mc.group(2)
        events = self.events.get(cal)
        if m == "GET":
            denied = need("calendar.events.read")
            if denied:
                return denied
            if events is None:
                return _json(404, {"error": {"code": 404}})
            if eid is None:
                return _json(200, {"items": list(events.values())})
            return _json(200, events[eid]) if eid in events else _json(404, {"error": {}})
        denied = need("calendar.events.write")
        if denied:
            return denied
        body = json.loads(request.content or b"{}")
        self.calendar_writes.append((m, cal, eid or "", body, dict(q)))
        if m == "POST":
            return _json(200, {"id": "new-event", "htmlLink": "https://calendar.test/e"})
        if m == "DELETE":
            return httpx.Response(204)
        return _json(200, {"id": eid})

    # ---- Drive ----------------------------------------------------------------------------------

    def _drive(self, request, p, q):
        m = request.method
        read = m == "GET"
        if not self._allowed(request, "drive.read" if read else "drive.write"):
            return _insufficient()
        all_drives = q.get("supportsAllDrives") == "true"
        if m == "GET" and p == "/about":
            return _json(200, {"user": {"emailAddress": OWNER}})
        if m == "GET" and p == "/files":
            qs = q.get("q", "")
            parent = re.match(r"'([^']+)' in parents", qs)
            files = [f for f in self.files.values()
                     if (not parent or parent.group(1) in f.get("parents", []))
                     and f["id"] != "rootid" and (all_drives or not f.get("driveId"))]
            return _json(200, {"files": files})
        mf = re.fullmatch(r"/files/([^/]+)(/export|/permissions)?", p)
        if not mf:
            if m == "POST" and p == "/files":
                self.drive_writes.append(("create", "", dict(q), request.content))
                return _json(200, {"id": "new-file", "name": "new"})
            return _json(404, {"error": {"code": 404}})
        fid = "rootid" if mf.group(1) == "root" else mf.group(1)
        f = self.files.get(fid)
        if f is None or (f.get("driveId") and not all_drives):
            return _json(404, {"error": {"code": 404, "message": "File not found"}})
        if m == "GET" and mf.group(2) == "/export":
            return httpx.Response(200, content=b"%PDF-exported",
                                  headers={"content-type": "application/pdf"})
        if m == "GET" and q.get("alt") == "media":
            return httpx.Response(200, content=self.content.get(fid, b""))
        if m == "GET":
            return _json(200, f)
        body = request.content
        self.drive_writes.append((m, fid + (mf.group(2) or ""), dict(q),
                                  json.loads(body) if body else {}))
        if m == "DELETE":
            return httpx.Response(204)
        return _json(200, {"id": "perm-1" if mf.group(2) else fid})


def _format(message: dict, fmt: str, wanted) -> dict:
    if fmt != "metadata":
        return message
    wanted = {w.lower() for w in ([wanted] if isinstance(wanted, str) else wanted or [])}
    payload = message["payload"]
    return {**{k: v for k, v in message.items() if k != "payload"},
            "payload": {"mimeType": payload["mimeType"],
                        "headers": [h for h in payload["headers"]
                                    if h["name"].lower() in wanted]}}


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


def _insufficient() -> httpx.Response:
    return _json(403, {"error": {"code": 403, "message": "Request had insufficient "
                                 "authentication scopes.",
                                 "errors": [{"reason": "insufficientPermissions"}]}})
