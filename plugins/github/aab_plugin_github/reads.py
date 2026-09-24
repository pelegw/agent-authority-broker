"""Read actions: list_repos, list_issues, get_issue, get_file, list_prs.

Each handler receives a `Call` whose repository has already passed the
scope (hidden or out of scope: 404 before any token exists) and whose token
was minted for exactly this call's repository and permissions. Every row
carries a `resource_ref`, so the broker's post-filter would drop a row this
code let through by mistake.
"""

import base64

from aab_plugin_runtime import AdapterError, Result

from .call import Call
from .ids import check_path, check_ref, encode_path
from .params import as_dict, as_list, choice, integer, page_params
from .rows import comment_row, issue_detail, issue_row, pr_row, ref, repo_row

MAX_FILE_BYTES = 1024 * 1024      # the contents API's own inline limit
STATES = ("open", "closed", "all")


def _next_page(raw: list, per_page: int, page: int) -> int | None:
    # Counted before visibility filtering: a filtered page may be short.
    return page + 1 if len(raw) >= per_page else None


def list_repos(call: Call) -> Result:
    per_page, page = page_params(call.params)
    query = {"per_page": per_page, "page": page}
    if call.token.mode == "app":
        # The installation token already limits this list to the minted
        # repositories; visibility below removes anything hidden.
        body = as_dict(call.get_json("/installation/repositories", params=query),
                       "repository list")
        raw = as_list(body.get("repositories"), "repository list")
    else:
        raw = as_list(call.get_json("/user/repos", params={**query, "sort": "updated"}),
                      "repository list")
    items = [row for row in map(repo_row, raw) if row and call.visible_repo(row["repo"])]
    return Result(data={"items": items, "next_page": _next_page(raw, per_page, page)})


def list_issues(call: Call) -> Result:
    per_page, page = page_params(call.params)
    state = choice(call.params, "state", STATES, "open")
    raw = as_list(call.get_json(f"{call.repo_path}/issues", params={
        "state": state, "per_page": per_page, "page": page}), "issue list")
    items = [issue_row(i, call.repo) for i in raw if isinstance(i, dict)]
    return Result(data={"items": items, "next_page": _next_page(raw, per_page, page)})


def get_issue(call: Call) -> Result:
    number = integer(call.params, "number", required=True)
    issue = as_dict(call.get_json(f"{call.repo_path}/issues/{number}"), "issue")
    comments = as_list(call.get_json(f"{call.repo_path}/issues/{number}/comments",
                                     params={"per_page": 100}), "comment list")
    return Result(data={**issue_detail(issue, call.repo),
                        "comments": [c for c in map(comment_row, comments) if c]})


def get_file(call: Call) -> Result:
    path = check_path(call.params.get("path"))
    ref_name = call.params.get("ref")
    if ref_name is not None:
        check_ref(ref_name)
    body = call.get_json(f"{call.repo_path}/contents/{encode_path(path)}",
                         params={"ref": ref_name} if ref_name else None)
    base = {"repo": call.repo, "path": path, "ref": ref_name, "resource_ref": ref(call.repo)}
    if isinstance(body, list):
        entries = [{"name": e.get("name"), "path": e.get("path"), "type": e.get("type"),
                    "size": e.get("size")} for e in body if isinstance(e, dict)]
        return Result(data={**base, "type": "dir", "entries": entries})
    body = as_dict(body, "file")
    kind = body.get("type")
    meta = {**base, "type": kind, "sha": body.get("sha"), "size": body.get("size")}
    if kind == "symlink":
        return Result(data={**meta, "target": body.get("target")})
    if kind == "submodule":
        return Result(data={**meta, "submodule_git_url": body.get("submodule_git_url")})
    if kind != "file":
        raise AdapterError(502, "GitHub returned an unexpected content type")
    if body.get("encoding") != "base64" or not isinstance(body.get("content"), str):
        # GitHub sends no inline content above 1 MB (encoding "none").
        raise AdapterError(400, "the file is larger than 1 MB; get_file cannot return it")
    try:
        raw = base64.b64decode(body["content"])
    except ValueError:
        raise AdapterError(502, "GitHub returned undecodable file content") from None
    if len(raw) > MAX_FILE_BYTES:
        raise AdapterError(400, "the file is larger than 1 MB; get_file cannot return it")
    try:
        return Result(data={**meta, "encoding": "utf-8", "content": raw.decode("utf-8")})
    except UnicodeDecodeError:
        return Result(data={**meta, "encoding": "base64",
                            "content": base64.b64encode(raw).decode("ascii")})


def list_prs(call: Call) -> Result:
    per_page, page = page_params(call.params)
    state = choice(call.params, "state", STATES, "open")
    raw = as_list(call.get_json(f"{call.repo_path}/pulls", params={
        "state": state, "per_page": per_page, "page": page}), "pull request list")
    items = [pr_row(p, call.repo) for p in raw if isinstance(p, dict)]
    return Result(data={"items": items, "next_page": _next_page(raw, per_page, page)})
