"""Drive: the folder subtree walk, every narrowing and constraint (both
sides), hidden == 404, and the root/shortcut escape hatches.

Tree in the fake:  rootid ─ fA ─ fA1 ─ doc1 (pdf)
                         │    ├ gdoc1 (Google Doc), html1, sc1 (shortcut)
                         └ fB ─ img1 (png, 2 MiB)
                   sdroot (shared drive) ─ shared1 ;  noaccess (unreadable) ─ orphan1
search_files in the fake ignores `q` and returns every file."""

import base64

import pytest

from . import fake_google as fg
from .conftest import items, vis

D = "gdrive"


def ids(response) -> list[str]:
    return sorted(f["id"] for f in items(response))


def files_get_count(google, fid) -> int:
    return sum(1 for m, p, q in google.requests
               if m == "GET" and p.endswith(f"/drive/v3/files/{fid}"))


def search(perform, v=None, c=None, query="plan"):
    return perform(D, "search_files", {"query": query}, v, c)


# ---- scopes -----------------------------------------------------------------------------

def test_reads_use_drive_readonly_and_writes_drive(perform, google):
    perform(D, "get_file_metadata", {"file_id": "doc1"})
    perform(D, "trash_file", {"file_id": "doc1"})
    assert google.refreshes == [(fg.DRIVE_RO,), (fg.DRIVE,)]


# ---- folder (subtree, proxy) --------------------------------------------------------------

def test_ancestors_walk_the_parent_chain(connected):
    r = connected.post("/resolve", headers={"X-Plugin-Id": D},
                       json={"kind": "folder", "query": "doc1", "relation": "ancestors"})
    assert r.json() == {"ancestors": ["fA1", "fA", "rootid"]}


def test_ancestor_walk_is_cached_for_60_seconds(connected, google, clock):
    body = {"kind": "folder", "query": "doc1", "relation": "ancestors"}
    connected.post("/resolve", headers={"X-Plugin-Id": D}, json=body)
    first = files_get_count(google, "fA")
    connected.post("/resolve", headers={"X-Plugin-Id": D}, json=body)
    assert files_get_count(google, "fA") == first == 1          # cache hit
    clock.advance(61)
    connected.post("/resolve", headers={"X-Plugin-Id": D}, json=body)
    assert files_get_count(google, "fA") == 2                   # expired, walked again


def test_folder_allow_keeps_search_inside_the_subtree(perform):
    v = {"folder": vis(allow=["fA"])}
    # A folder is inside its own subtree, so fA itself is listed too.
    assert ids(search(perform, v)) == ["doc1", "fA", "fA1", "gdoc1", "html1", "sc1"]


def test_folder_allow_refuses_a_file_outside(perform, google):
    v = {"folder": vis(allow=["fA1"])}
    assert perform(D, "get_file_metadata", {"file_id": "doc1"}, v).status_code == 200
    r = perform(D, "get_file_metadata", {"file_id": "img1"}, v)
    assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_list_files_of_a_folder_outside_the_subtree_is_404(perform):
    v = {"folder": vis(allow=["fA1"])}
    assert perform(D, "list_files", {"folder_id": "fA"}, v).status_code == 404
    assert ids(perform(D, "list_files", {"folder_id": "fA1"}, v)) == ["doc1"]


def test_hidden_folder_hides_its_subtree(perform):
    v = {"folder": vis(deny=["fA1"])}
    assert "doc1" not in ids(search(perform, v))
    assert perform(D, "get_file_metadata", {"file_id": "doc1"}, v).status_code == 404
    assert perform(D, "list_files", {"folder_id": "fA1"}, v).status_code == 404
    assert perform(D, "get_file_metadata", {"file_id": "gdoc1"}, v).status_code == 200


def test_unreadable_ancestry_counts_as_hidden_while_folders_are_hidden(perform):
    assert "orphan1" in ids(search(perform))                         # nothing hidden
    assert "orphan1" not in ids(search(perform, {"folder": vis(deny=["fB"])}))


def test_list_rows_carry_file_and_folder_refs(perform):
    rows = {r["id"]: r["resource_ref"] for r in items(perform(D, "list_files",
                                                               {"folder_id": "fA"}))}
    assert rows["fA1"] == {"kind": "folder", "id": "fA1"}
    assert rows["gdoc1"] == {"kind": "file", "id": "gdoc1"}


def test_root_alias_resolves_to_the_real_id(connected):
    r = connected.post("/normalize", headers={"X-Plugin-Id": D},
                       json={"kind": "folder", "value": "root"})
    assert r.json() == {"id": "rootid"}


def test_root_alias_cannot_bypass_a_hidden_root(perform):
    r = perform(D, "list_files", {"folder_id": "root"}, {"folder": vis(deny=["rootid"])})
    assert r.status_code == 404


def test_client_supplied_path_is_not_an_id(perform):
    assert perform(D, "get_file_metadata", {"file_id": "fA/fA1/doc1"}).status_code == 400


# ---- hidden files == 404 ---------------------------------------------------------------------

@pytest.mark.parametrize("action,params", [
    ("get_file_metadata", {}), ("download_file", {}), ("trash_file", {}), ("delete_file", {}),
    ("move_file", {"new_parent_id": "fB"}), ("share_file", {"role": "reader", "type": "anyone"}),
])
def test_hidden_file_is_404_and_never_fetched(perform, google, action, params):
    before = files_get_count(google, "doc1")
    r = perform(D, action, {**params, "file_id": "doc1"}, {"file": vis(deny=["doc1"])})
    missing = perform(D, action, {**params, "file_id": "nosuchfile"})
    assert r.status_code == missing.status_code == 404
    assert r.json() == missing.json() == {"error": "not found"}
    assert files_get_count(google, "doc1") == before
    assert google.drive_writes == []


def test_hidden_file_absent_from_listings(perform):
    assert "doc1" not in ids(perform(D, "list_files", {"folder_id": "fA1"},
                                     {"file": vis(deny=["doc1"])}))


# ---- mime (list, proxy) ------------------------------------------------------------------------

def test_mime_allow_filters_files_but_not_folders(perform):
    v = {"mime": vis(allow=["application/pdf"])}
    assert ids(search(perform, v, {"shared_drives": False})) == [
        "doc1", "fA", "fA1", "fB", "orphan1"]
    assert perform(D, "get_file_metadata", {"file_id": "img1"}, v).status_code == 404


def test_mime_deny(perform):
    assert "img1" not in ids(search(perform, {"mime": vis(deny=["image/png"])}))


# ---- shared_drives (flag) ------------------------------------------------------------------------

def test_shared_drives_false_never_asks_for_them(perform, google):
    r = search(perform, c={"shared_drives": False})
    assert "shared1" not in ids(r)
    assert all("supportsAllDrives" not in q for _, p, q in google.requests if "/drive/" in p)
    assert perform(D, "get_file_metadata", {"file_id": "shared1"},
                   constraints={"shared_drives": False}).status_code == 404


def test_shared_drives_allowed(perform):
    assert "shared1" in ids(search(perform))
    assert perform(D, "get_file_metadata", {"file_id": "shared1"}).status_code == 200


# ---- file_content (flag): metadata only ------------------------------------------------------

def test_metadata_only_refuses_download_before_any_call(perform, google):
    before = len(google.requests)
    r = perform(D, "download_file", {"file_id": "doc1"}, constraints={"file_content": False})
    assert r.status_code == 403
    assert all("/drive/" not in p for _, p, _ in google.requests[before:])
    assert perform(D, "get_file_metadata", {"file_id": "doc1"},
                   constraints={"file_content": False}).status_code == 200


def test_metadata_only_search_matches_names_only(perform, google):
    search(perform, c={"file_content": False})
    q = [q for m, p, q in google.requests if p.endswith("/drive/v3/files")][-1]["q"]
    assert "fullText" not in q and "name contains 'plan'" in q
    search(perform)
    q = [q for m, p, q in google.requests if p.endswith("/drive/v3/files")][-1]["q"]
    assert "fullText contains 'plan'" in q


def test_download_with_content_allowed(perform):
    r = perform(D, "download_file", {"file_id": "doc1"})
    assert r.status_code == 200 and base64.b64decode(r.json()["binary_b64"]) == b"%PDF-plan"


def test_google_native_files_export_as_pdf(perform):
    r = perform(D, "download_file", {"file_id": "gdoc1"})
    assert r.json()["mime"] == "application/pdf"


def test_active_content_is_served_opaque(perform):
    assert perform(D, "download_file", {"file_id": "html1"}).json()["mime"] == \
        "application/octet-stream"


def test_shortcuts_are_not_followed(perform):
    assert perform(D, "download_file", {"file_id": "sc1"}).status_code == 400


# ---- max_download_mb (range) -----------------------------------------------------------------

def test_download_over_the_limit_is_refused(perform, google):
    r = perform(D, "download_file", {"file_id": "img1"}, constraints={"max_download_mb": 1})
    assert r.status_code == 403
    assert not any(q.get("alt") == "media" for _, _, q in google.requests)
    ok = perform(D, "download_file", {"file_id": "img1"}, constraints={"max_download_mb": 5})
    assert ok.status_code == 200


def test_a_lying_size_is_still_capped(perform, google):
    google.files["doc1"]["size"] = "1"
    google.content["doc1"] = b"x" * (1024 * 1024 + 1)
    r = perform(D, "download_file", {"file_id": "doc1"}, constraints={"max_download_mb": 1})
    assert r.status_code == 403


# ---- external_sharing (flag) -----------------------------------------------------------------

def share(perform, c=None, **kw):
    return perform(D, "share_file", {"file_id": "doc1", "role": "reader", **kw}, None, c)


def test_external_sharing_false_refuses_outsiders(perform, google):
    no = {"external_sharing": False}
    assert share(perform, no, type="user", email_address="bob@other.org").status_code == 403
    assert share(perform, no, type="anyone").status_code == 403
    assert share(perform, no, type="domain", domain="other.org").status_code == 403
    assert google.drive_writes == []
    ok = share(perform, no, type="user", email_address="colleague@example.com")
    assert ok.status_code == 200
    method, target, query, body = google.drive_writes[-1]
    assert (method, target) == ("POST", "doc1/permissions")
    assert body == {"role": "reader", "type": "user", "emailAddress": "colleague@example.com"}
    assert query["sendNotificationEmail"] == "false"


def test_external_sharing_allowed(perform):
    assert share(perform, type="anyone").status_code == 200
    assert share(perform, type="user", email_address="bob@other.org").status_code == 200


def test_consumer_accounts_have_no_internal_principals(perform, google, monkeypatch):
    real = google._handle

    def handle(request):
        if request.url.path.endswith("/about"):
            import httpx
            return httpx.Response(200, json={"user": {"emailAddress": "me@gmail.com"}})
        return real(request)
    monkeypatch.setattr(google, "_handle", handle)
    r = share(perform, {"external_sharing": False}, type="user", email_address="you@gmail.com")
    assert r.status_code == 403


# ---- writes inside the subtree ---------------------------------------------------------------

def test_upload_respects_mime_and_folder(perform, google):
    content = base64.b64encode(b"hello").decode()
    p = {"parent_id": "fA1", "name": "n.txt", "mime_type": "text/plain", "content_b64": content}
    assert perform(D, "upload_file", p, {"mime": vis(allow=["application/pdf"])}).status_code == 403
    assert perform(D, "upload_file", p, {"folder": vis(allow=["fB"])}).status_code == 404
    assert google.drive_writes == []
    assert perform(D, "upload_file", p, {"folder": vis(allow=["fA"])}).status_code == 200
    method, _, query, _ = google.drive_writes[-1]
    assert method == "create" and query["uploadType"] == "multipart"


def test_move_into_a_hidden_folder_is_404(perform, google):
    r = perform(D, "move_file", {"file_id": "doc1", "new_parent_id": "fB"},
                {"folder": vis(deny=["fB"])})
    assert r.status_code == 404 and google.drive_writes == []
    ok = perform(D, "move_file", {"file_id": "doc1", "new_parent_id": "fB"})
    assert ok.status_code == 200
    assert google.drive_writes[-1][2]["addParents"] == "fB"
    assert google.drive_writes[-1][2]["removeParents"] == "fA1"


def test_move_out_of_the_allowed_subtree_is_404(perform, google):
    r = perform(D, "move_file", {"file_id": "doc1", "new_parent_id": "fB"},
                {"folder": vis(allow=["fA"])})
    assert r.status_code == 404 and google.drive_writes == []


def test_create_folder_and_delete(perform, google):
    assert perform(D, "create_folder", {"parent_id": "fA", "name": "new"}).status_code == 200
    assert perform(D, "delete_file", {"file_id": "doc1"}).status_code == 200
    assert google.drive_writes[-1][0] == "DELETE"
