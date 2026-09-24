"""Write and destructive actions.

Each handler receives a `Call` whose repository is visible, whose branch (if
the action names one) is inside the grant, and whose token carries exactly
this action's permissions on exactly this repository.

The 503/502 split is precise here, because the broker's queue depends on it:
  * lookups that precede the side effect (the default branch, the file's
    current sha, the pull request before a merge) use `before_effect=True`:
    any failure there is 503, the write provably did not happen;
  * the write itself (`send_json`) is 502 once sent: it may have happened.

Write results carry no `resource_ref` on purpose (as in the WhatsApp
plugin): the action has been performed, and a post-filter 404 on its result
would tell the agent that it had not.
"""

import base64

from aab_plugin_runtime import AdapterError, Result

from .api import NOT_FOUND, GitHubError
from .call import Call
from .ids import check_branch, check_path, check_ref, check_sha, encode_path, encode_ref
from .params import as_dict, boolean, choice, integer, text

MAX_PUSH_BYTES = 1024 * 1024
SHA_ACCEPT = "application/vnd.github.sha"     # "get a commit" as a bare sha


def create_issue(call: Call) -> Result:
    title = text(call.params, "title", required=True, max_len=256)
    body = text(call.params, "body", default="", allow_empty=True, max_len=65536)
    out = call.send_json("POST", f"{call.repo_path}/issues", {"title": title, "body": body})
    return Result(data={"status": "created", "number": out.get("number"),
                        "url": out.get("html_url")})


def comment_issue(call: Call) -> Result:
    number = integer(call.params, "number", required=True)
    body = text(call.params, "body", required=True, max_len=65536)
    out = call.send_json("POST", f"{call.repo_path}/issues/{number}/comments", {"body": body})
    return Result(data={"status": "commented", "id": out.get("id"), "url": out.get("html_url")})


def close_issue(call: Call) -> Result:
    number = integer(call.params, "number", required=True)
    reason = choice(call.params, "reason", ("completed", "not_planned"), "completed")
    out = call.send_json("PATCH", f"{call.repo_path}/issues/{number}",
                         {"state": "closed", "state_reason": reason})
    return Result(data={"status": "closed", "number": out.get("number", number)})


def _commit_sha(call: Call, ref_name: str | None) -> str:
    """The commit a ref points at (default branch when None). Lookups only."""
    if ref_name is None:
        repo = as_dict(call.get_json(call.repo_path, before_effect=True), "repository",
                       before_effect=True)
        try:
            ref_name = check_branch(repo.get("default_branch"), "default branch")
        except AdapterError:
            raise GitHubError(503, "GitHub returned no usable default branch") from None
    resp = call.request("GET", f"{call.repo_path}/commits/{encode_ref(ref_name)}",
                        accept=SHA_ACCEPT, before_effect=True)
    try:
        return check_sha(resp.text.strip())
    except AdapterError:
        raise GitHubError(503, "GitHub returned an unexpected commit id") from None


def create_branch(call: Call) -> Result:
    branch = check_branch(call.params.get("branch"))
    from_ref = call.params.get("from_ref")
    if from_ref is not None:
        check_ref(from_ref, "from_ref")
    sha = _commit_sha(call, from_ref)
    try:
        call.send_json("POST", f"{call.repo_path}/git/refs",
                       {"ref": f"refs/heads/{branch}", "sha": sha})
    except GitHubError as exc:
        if exc.github_status == 422 and "already exists" in exc.github_message.lower():
            raise AdapterError(409, "the branch already exists") from None
        raise
    return Result(data={"status": "created", "branch": branch, "sha": sha})


def push_file(call: Call) -> Result:
    branch = check_branch(call.params.get("branch"))
    path = check_path(call.params.get("path"))
    content = text(call.params, "content", required=True, allow_empty=True)
    message = text(call.params, "message", required=True, max_len=4096)
    data = content.encode("utf-8")
    if len(data) > MAX_PUSH_BYTES:
        raise AdapterError(400, "content is larger than 1 MiB")
    sha = call.params.get("sha")
    if sha is not None:
        check_sha(sha)
    url = f"{call.repo_path}/contents/{encode_path(path)}"
    created = False
    if sha is None:
        # Updating needs the current blob sha; creating must not send one.
        try:
            existing = call.get_json(url, params={"ref": branch}, before_effect=True)
        except GitHubError as exc:
            if exc.status != 404:
                raise
            existing, created = None, True
        if existing is not None:
            if not isinstance(existing, dict) or existing.get("type") != "file":
                raise AdapterError(400, "path exists and is not a file")
            try:
                sha = check_sha(existing.get("sha"))
            except AdapterError:
                raise GitHubError(503, "GitHub returned no usable file sha") from None
    body = {"message": message, "content": base64.b64encode(data).decode("ascii"),
            "branch": branch}
    if sha is not None:
        body["sha"] = sha
    try:
        out = call.send_json("PUT", url, body)
    except GitHubError as exc:
        if exc.github_status == 409:
            raise AdapterError(409, "the file changed since it was read; read it again") from None
        raise
    commit = out.get("commit") if isinstance(out.get("commit"), dict) else {}
    blob = out.get("content") if isinstance(out.get("content"), dict) else {}
    return Result(data={"status": "committed", "path": path, "branch": branch,
                        "created": created, "commit_sha": commit.get("sha"),
                        "blob_sha": blob.get("sha")})


def create_pr(call: Call) -> Result:
    head = check_branch(call.params.get("head"), "head")
    base = check_branch(call.params.get("base"), "base")
    title = text(call.params, "title", required=True, max_len=256)
    body = text(call.params, "body", default="", allow_empty=True, max_len=65536)
    draft = boolean(call.params, "draft", default=False)
    out = call.send_json("POST", f"{call.repo_path}/pulls",
                         {"title": title, "head": head, "base": base, "body": body,
                          "draft": draft})
    return Result(data={"status": "opened", "number": out.get("number"),
                        "url": out.get("html_url")})


def merge_pr(call: Call) -> Result:
    number = integer(call.params, "number", required=True)
    method = choice(call.params, "method", ("merge", "squash", "rebase"), "squash")
    pr = as_dict(call.get_json(f"{call.repo_path}/pulls/{number}", before_effect=True),
                 "pull request", before_effect=True)
    if pr.get("merged"):
        raise AdapterError(409, "the pull request is already merged")
    if pr.get("state") != "open":
        raise AdapterError(409, "the pull request is not open")
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    try:
        base_ref = check_branch(base.get("ref"), "base")
        head_sha = check_sha(head.get("sha"), "head sha")
    except AdapterError:
        raise GitHubError(503, "GitHub returned an unreadable pull request") from None
    # The branch a merge writes to is the base: that is what the grant's
    # branch selector must reach.
    call.require_branch(base_ref)
    try:
        # `sha` makes GitHub refuse (409) if the head moved after this read,
        # so what merges is what was checked.
        out = call.send_json("PUT", f"{call.repo_path}/pulls/{number}/merge",
                             {"merge_method": method, "sha": head_sha})
    except GitHubError as exc:
        if exc.github_status == 405:
            raise AdapterError(409, f"not mergeable: {exc.github_message}") from None
        if exc.github_status == 409:
            raise AdapterError(409, "the pull request head moved; review it again") from None
        raise
    return Result(data={"status": "merged" if out.get("merged", True) else "not_merged",
                        "number": number, "sha": out.get("sha")})


def delete_branch(call: Call) -> Result:
    branch = check_branch(call.params.get("branch"))
    try:
        call.request("DELETE", f"{call.repo_path}/git/refs/heads/{encode_ref(branch)}")
    except GitHubError as exc:
        if exc.github_status == 422:
            if "does not exist" in exc.github_message.lower():
                raise AdapterError(404, NOT_FOUND) from None
            raise AdapterError(409, f"GitHub refused: {exc.github_message}") from None
        raise
    return Result(data={"status": "deleted", "branch": branch})
