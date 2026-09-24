"""What each action does against (the fake) GitHub, in App mode."""

import base64

from .conftest import data, items
from .fakes import sha_of


def test_list_issues_and_get_issue(app_mode, perform):
    rows = items(perform("list_issues", {"repo": "octo/a", "state": "all"}))
    assert [(r["number"], r["pull_request"]) for r in rows] == [(1, False), (2, True)]
    assert rows[0]["labels"] == ["bug"] and rows[0]["user"] == "alice"
    issue = data(perform("get_issue", {"repo": "octo/a", "number": 1}))
    assert issue["body"] == "body 1" and issue["repo"] == "octo/a"
    assert issue["comments"] == [{"id": 11, "user": "alice", "body": "me too",
                                  "created_at": "2026-01-01T00:00:00Z"}]
    assert perform("get_issue", {"repo": "octo/a", "number": 99}).status_code == 404


def test_list_pagination_hint(app_mode, perform):
    first = data(perform("list_issues", {"repo": "octo/a", "state": "all", "limit": 1}))
    assert len(first["items"]) == 1 and first["next_page"] == 2
    last = data(perform("list_issues", {"repo": "octo/a", "state": "all", "limit": 1,
                                        "page": 2}))
    assert last["items"][0]["number"] == 2


def test_get_file_text_binary_and_directory(app_mode, perform, gh):
    text = data(perform("get_file", {"repo": "octo/a", "path": "docs/intro.md"}))
    assert (text["encoding"], text["content"]) == ("utf-8", "# Intro\n")
    assert text["sha"] == sha_of(b"# Intro\n")
    binary = data(perform("get_file", {"repo": "octo/a", "path": "bin/logo.png"}))
    assert binary["encoding"] == "base64"
    assert base64.b64decode(binary["content"]) == b"\x89PNG\r\n\x1a\n\x00\xff"
    folder = data(perform("get_file", {"repo": "octo/a", "path": "docs"}))
    assert folder["type"] == "dir" and [e["name"] for e in folder["entries"]] == ["intro.md"]
    at_ref = data(perform("get_file", {"repo": "octo/a", "path": "README.md", "ref": "dev"}))
    assert at_ref["content"] == "dev branch"
    assert gh.calls("GET", "/repos/octo/a/contents/README.md")[-1].query == {"ref": "dev"}


def test_a_file_github_will_not_inline_is_400(app_mode, perform, gh):
    import httpx
    gh.fail("GET", r"/contents/big\.bin$", httpx.Response(200, json={
        "type": "file", "encoding": "none", "content": "", "size": 5_000_000, "sha": "0" * 40}))
    r = perform("get_file", {"repo": "octo/a", "path": "big.bin"})
    assert r.status_code == 400 and "1 MB" in r.json()["error"]


def test_list_prs_flags_forks_without_naming_them(app_mode, perform, gh):
    gh.repos["octo/a"].pulls[3] = {
        "number": 3, "title": "from a fork", "state": "open", "user": {"login": "mallory"},
        "head": {"ref": "patch", "repo": {"id": 999, "full_name": "mallory/secret-fork"}},
        "base": {"ref": "main", "repo": {"id": gh.repos["octo/a"].id}}}
    rows = items(perform("list_prs", {"repo": "octo/a"}))
    assert [(r["number"], r["head"], r["base"], r["cross_repository"]) for r in rows] == [
        (2, "dev", "main", False), (3, "patch", "main", True)]
    assert "mallory/secret-fork" not in str(rows)


def test_create_comment_and_close_issue(app_mode, perform, gh):
    created = data(perform("create_issue", {"repo": "octo/a", "title": "New", "body": "b"}))
    assert created["status"] == "created" and created["number"] == 3
    assert gh.repos["octo/a"].issues[3]["title"] == "New"
    commented = data(perform("comment_issue", {"repo": "octo/a", "number": 3, "body": "hi"}))
    assert commented["status"] == "commented"
    assert gh.repos["octo/a"].comments[3][0]["body"] == "hi"
    closed = data(perform("close_issue", {"repo": "octo/a", "number": 3,
                                          "reason": "not_planned"}))
    assert closed == {"status": "closed", "number": 3}
    assert gh.repos["octo/a"].issues[3]["state_reason"] == "not_planned"


def test_create_branch_from_the_default_branch_and_from_a_ref(app_mode, perform, gh):
    repo = gh.repos["octo/a"]
    out = data(perform("create_branch", {"repo": "octo/a", "branch": "agent/one"}))
    assert out == {"status": "created", "branch": "agent/one", "sha": repo.branches["main"]}
    out = data(perform("create_branch", {"repo": "octo/a", "branch": "agent/two",
                                         "from_ref": "dev"}))
    assert out["sha"] == repo.branches["dev"]
    again = perform("create_branch", {"repo": "octo/a", "branch": "agent/one"})
    assert again.status_code == 409


def test_push_file_creates_then_updates_with_the_current_sha(app_mode, perform, gh):
    made = data(perform("push_file", {"repo": "octo/a", "branch": "dev", "path": "notes/a.md",
                                      "content": "v1", "message": "add"}))
    assert made["created"] is True and made["blob_sha"] == sha_of(b"v1")
    updated = data(perform("push_file", {"repo": "octo/a", "branch": "dev",
                                         "path": "notes/a.md", "content": "v2 é",
                                         "message": "edit"}))
    assert updated["created"] is False
    assert gh.repos["octo/a"].files["dev"]["notes/a.md"] == "v2 é".encode()
    put = gh.calls("PUT", "/repos/octo/a/contents/notes/a.md")[-1].body
    assert put["sha"] == sha_of(b"v1") and put["branch"] == "dev"


def test_push_file_with_a_stale_sha_is_409(app_mode, perform):
    r = perform("push_file", {"repo": "octo/a", "branch": "main", "path": "README.md",
                              "content": "x", "message": "m", "sha": "0" * 40})
    assert r.status_code == 409


def test_push_file_refuses_a_directory_and_oversized_content(app_mode, perform, gh):
    r = perform("push_file", {"repo": "octo/a", "branch": "main", "path": "docs",
                              "content": "x", "message": "m"})
    assert r.status_code == 400
    r = perform("push_file", {"repo": "octo/a", "branch": "main", "path": "big.txt",
                              "content": "é" * 600_000, "message": "m"})
    assert r.status_code == 400
    assert gh.calls("PUT", "/repos/") == []


def test_create_pr_and_merge_it(app_mode, perform, gh):
    perform("create_branch", {"repo": "octo/a", "branch": "agent/pr"})
    opened = data(perform("create_pr", {"repo": "octo/a", "head": "agent/pr", "base": "main",
                                        "title": "T", "draft": True}))
    assert opened["status"] == "opened"
    number = opened["number"]
    assert gh.repos["octo/a"].pulls[number]["draft"] is True
    merged = data(perform("merge_pr", {"repo": "octo/a", "number": number}))
    assert merged["status"] == "merged"
    body = gh.calls("PUT", f"/repos/octo/a/pulls/{number}/merge")[-1].body
    assert body == {"merge_method": "squash",
                    "sha": gh.repos["octo/a"].pulls[number]["head"]["sha"]}
    assert perform("merge_pr", {"repo": "octo/a", "number": number}).status_code == 409


def test_create_pr_refuses_cross_repository_heads(app_mode, perform, gh):
    r = perform("create_pr", {"repo": "octo/a", "head": "mallory:patch", "base": "main",
                              "title": "T"})
    assert r.status_code == 400
    assert gh.calls("POST", "/repos/") == []


def test_merge_refuses_a_moved_head_and_an_unmergeable_pr(app_mode, perform, gh):
    pr = gh.repos["octo/a"].pulls[2]
    pr["mergeable"] = False
    assert perform("merge_pr", {"repo": "octo/a", "number": 2}).status_code == 409
    pr["mergeable"] = True
    original = pr["head"]["sha"]
    # The head moves between the plugin's read and GitHub's merge.
    import httpx
    gh.fail("PUT", r"/pulls/2/merge$", httpx.Response(409, json={
        "message": "Head branch was modified. Review and try the merge again."}))
    r = perform("merge_pr", {"repo": "octo/a", "number": 2})
    assert r.status_code == 409 and "moved" in r.json()["error"]
    assert pr["head"]["sha"] == original and pr["merged"] is False


def test_delete_branch(app_mode, perform, gh):
    assert data(perform("delete_branch", {"repo": "octo/a", "branch": "dev"})) == {
        "status": "deleted", "branch": "dev"}
    assert "dev" not in gh.repos["octo/a"].branches
    missing = perform("delete_branch", {"repo": "octo/a", "branch": "dev"})
    assert missing.status_code == 404 and missing.json() == {"error": "not found"}


def test_resolve_and_label_serve_the_owner(app_mode, adapter):
    found = adapter.resolve("repo", "OCTO/", 10)
    assert [f["id"] for f in found] == ["octo/a", "octo/b", "octo/secret"]
    assert found[0]["label"] == "Octo/A"
    assert adapter.resolve("repo", "", 1) == [found[0]]
    assert adapter.label("repo", ["octo/a"]) == {"octo/a": "octo/a"}


def test_normalize(adapter):
    assert adapter.normalize("repo", " Octo/A ") == "octo/a"
    assert adapter.normalize("branch", "Feature/X") == "Feature/X"
