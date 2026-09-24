"""Compact result rows: what an agent sees of GitHub's objects.

GitHub objects are large (every URL template, every nested user); agents
pay tokens for every byte, so rows keep only what an agent acts on. Every
row that names a repository carries `resource_ref: {"kind": "repo", "id":
"owner/name"}` so the broker's post-filter drops anything the call may not
see, whatever this plugin did.

Pull requests from another repository (a fork) are flagged
`cross_repository` without naming that repository: it may be one the owner
has hidden, and a hidden repository is never acknowledged.
"""

from aab_plugin_runtime import AdapterError

from .ids import normalize_repo


def ref(repo_id: str) -> dict:
    return {"kind": "repo", "id": repo_id}


def _login(obj) -> str | None:
    user = obj.get("user") if isinstance(obj, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) else None


def _labels(obj: dict) -> list[str]:
    labels = obj.get("labels")
    if not isinstance(labels, list):
        return []
    return [lb["name"] for lb in labels if isinstance(lb, dict) and isinstance(lb.get("name"), str)]


def repo_row(r) -> dict | None:
    """None for anything whose full_name is not a valid owner/name: a row we
    cannot identify cannot be checked against the scope, so it is dropped."""
    if not isinstance(r, dict):
        return None
    try:
        rid = normalize_repo(r.get("full_name"))
    except AdapterError:
        return None
    return {"repo": rid, "full_name": r.get("full_name"), "private": r.get("private"),
            "description": r.get("description"), "default_branch": r.get("default_branch"),
            "archived": r.get("archived"), "updated_at": r.get("updated_at"),
            "url": r.get("html_url"), "resource_ref": ref(rid)}


def issue_row(i: dict, repo_id: str) -> dict:
    return {"number": i.get("number"), "title": i.get("title"), "state": i.get("state"),
            "user": _login(i), "labels": _labels(i), "comments": i.get("comments"),
            "pull_request": "pull_request" in i, "created_at": i.get("created_at"),
            "updated_at": i.get("updated_at"), "resource_ref": ref(repo_id)}


def issue_detail(i: dict, repo_id: str) -> dict:
    return {**issue_row(i, repo_id), "repo": repo_id, "body": i.get("body"),
            "state_reason": i.get("state_reason"), "closed_at": i.get("closed_at"),
            "url": i.get("html_url")}


def comment_row(c) -> dict | None:
    if not isinstance(c, dict):
        return None
    return {"id": c.get("id"), "user": _login(c), "body": c.get("body"),
            "created_at": c.get("created_at")}


def pr_row(p: dict, repo_id: str) -> dict:
    head = p.get("head") if isinstance(p.get("head"), dict) else {}
    base = p.get("base") if isinstance(p.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    cross = bool(head_repo.get("id") is None or head_repo.get("id") != base_repo.get("id"))
    return {"number": p.get("number"), "title": p.get("title"), "state": p.get("state"),
            "draft": p.get("draft"), "user": _login(p), "head": head.get("ref"),
            "base": base.get("ref"), "cross_repository": cross,
            "merged_at": p.get("merged_at"), "created_at": p.get("created_at"),
            "updated_at": p.get("updated_at"), "resource_ref": ref(repo_id)}
