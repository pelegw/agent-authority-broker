"""A scripted GitHub behind `httpx.MockTransport`, for the plugin and broker tests.

It behaves like GitHub where the plugin's security depends on it:
  * `/app/*` requires an RS256 App JWT signed by `PRIVATE_KEY` with
    `iss == APP_ID`, not expired and at most 10 minutes ahead (the fake's
    own clock, shared with the plugin under test);
  * installation tokens are minted with exactly the requested permissions
    and repositories (plus the implicit metadata:read), 422 when asking
    for a permission the installation lacks or a repository it cannot
    reach, and every repo endpoint checks the token: a repository outside
    the token is a 404, a missing permission a 403. So a test can prove
    that GitHub itself would refuse what the broker refused;
  * a PAT reaches every repository with every permission (why PAT mode is
    proxy-only);
  * redirects (renamed repositories), rate limits and transport failures
    can be scripted.

Self-contained on purpose (no relative imports): broker/tests/targets
loads this file by path.
"""

import base64
import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

APP_ID = "12345"
APP_SLUG = "aab-test-app"
INSTALLATION_ID = "777"
ACCOUNT = "octo"
PAT = "ghp_" + "P" * 36
START = 1_760_000_000.0
RANK = {None: 0, "read": 1, "write": 2, "admin": 3}
INSTALLED = {"metadata": "read", "contents": "write", "issues": "write",
             "pull_requests": "write"}


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY_PEM = _pem(PRIVATE_KEY)
PUBLIC_KEY = PRIVATE_KEY.public_key()
OTHER_KEY_PEM = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))


class Clock:
    """A settable clock shared by the plugin under test and the fake."""

    def __init__(self, now: float = START):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def sha_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


@dataclass
class Recorded:
    method: str
    path: str
    query: dict
    auth: str
    body: object
    accept: str


@dataclass
class Repo:
    full_name: str
    private: bool = True
    default_branch: str = "main"
    branches: dict = field(default_factory=dict)       # name -> head commit sha
    files: dict = field(default_factory=dict)          # branch -> {path: bytes}
    issues: dict = field(default_factory=dict)         # number -> issue dict
    comments: dict = field(default_factory=dict)       # number -> [comment]
    pulls: dict = field(default_factory=dict)          # number -> pull dict
    id: int = 0


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


def _msg(status: int, message: str, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json={"message": message}, headers=headers or {})


class FakeGitHub:
    def __init__(self, clock: Clock | None = None):
        self.clock = clock or Clock()
        self.app_id = APP_ID
        self.installations = {INSTALLATION_ID: {
            "account": ACCOUNT, "permissions": dict(INSTALLED), "selection": "all"}}
        self.repos: dict[str, Repo] = {}
        self.renamed: dict[str, str] = {}              # old full_name -> new
        self.tokens: dict[str, dict] = {}
        self.pat = PAT
        self.requests: list[Recorded] = []
        self.jwt_claims: list[dict] = []
        self.failures: list[tuple[str, re.Pattern, object]] = []
        self.widen_tokens = False                      # misbehave: grant more than asked
        self._ids = itertools.count(1)
        self._seed()

    # ---- seed data ------------------------------------------------------------

    def add_repo(self, full_name: str, **files) -> Repo:
        r = Repo(full_name=full_name, id=next(self._ids))
        main = sha_of(f"{full_name}:main".encode())
        r.branches = {"main": main, "dev": sha_of(f"{full_name}:dev".encode())}
        r.files = {"main": {"README.md": b"hello from " + full_name.encode(), **files},
                   "dev": {"README.md": b"dev branch"}}
        r.issues = {1: self._issue(1, "First bug", "open"),
                    2: {**self._issue(2, "A pull request", "open"), "pull_request": {}}}
        r.comments = {1: [{"id": 11, "user": {"login": "alice"}, "body": "me too",
                           "created_at": "2026-01-01T00:00:00Z"}]}
        r.pulls = {2: {"number": 2, "title": "A pull request", "state": "open",
                       "draft": False, "merged": False, "user": {"login": "alice"},
                       "head": {"ref": "dev", "sha": r.branches["dev"],
                                "repo": {"id": r.id, "full_name": full_name}},
                       "base": {"ref": "main", "sha": main,
                                "repo": {"id": r.id, "full_name": full_name}},
                       "mergeable": True}}
        self.repos[full_name.lower()] = r
        return r

    @staticmethod
    def _issue(number: int, title: str, state: str) -> dict:
        return {"number": number, "title": title, "state": state, "body": f"body {number}",
                "user": {"login": "alice"}, "labels": [{"name": "bug"}], "comments": 1,
                "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
                "html_url": f"https://github.com/x/y/issues/{number}"}

    def _seed(self) -> None:
        self.add_repo("Octo/A", **{"docs/intro.md": b"# Intro\n", "bin/logo.png":
                                   b"\x89PNG\r\n\x1a\n\x00\xff"})
        self.add_repo("octo/b")
        self.add_repo("octo/secret")
        self.add_repo("evil/x")          # another owner: outside the installation

    # ---- scripting -------------------------------------------------------------

    def fail(self, method: str, path_regex: str, what) -> None:
        """One-shot failure for the next matching request: "connect",
        "read_timeout", an int status, or an httpx.Response."""
        self.failures.append((method, re.compile(path_regex), what))

    def rate_limit(self, method: str, path_regex: str, *, secondary: bool = False) -> None:
        if secondary:
            resp = _msg(403, "You have exceeded a secondary rate limit.", {"retry-after": "30"})
        else:
            resp = _msg(403, "API rate limit exceeded",
                        {"x-ratelimit-remaining": "0",
                         "x-ratelimit-reset": str(int(self.clock()) + 120)})
        self.fail(method, path_regex, resp)

    # ---- inspection --------------------------------------------------------------

    def token_requests(self) -> list[dict]:
        """JSON bodies of every installation-token request, in order."""
        return [r.body for r in self.requests if r.method == "POST"
                and r.path.endswith("/access_tokens")]

    def calls(self, method: str, prefix: str) -> list[Recorded]:
        return [r for r in self.requests if r.method == method and r.path.startswith(prefix)]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # ---- dispatch ---------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode("ascii")
        path = raw.split("?", 1)[0]
        query = {k: v[-1] for k, v in parse_qs(request.url.query.decode()).items()}
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:
            body = None
        auth = request.headers.get("authorization", "")
        self.requests.append(Recorded(request.method, unquote(path), query, auth, body,
                                      request.headers.get("accept", "")))
        for i, (method, rx, what) in enumerate(self.failures):
            if method == request.method and rx.search(unquote(path)):
                del self.failures[i]
                if what == "connect":
                    raise httpx.ConnectError("refused", request=request)
                if what == "read_timeout":
                    raise httpx.ReadTimeout("timed out", request=request)
                return what if isinstance(what, httpx.Response) else _msg(what, "scripted")
        for method, rx, fn in self._routes():
            m = rx.fullmatch(path)
            if m and method == request.method:
                return fn(request, auth, query, body, *[unquote(g) for g in m.groups()])
        return _msg(404, "Not Found")

    def _routes(self):
        R = re.compile
        repo = r"/repos/([^/]+)/([^/]+)"
        return [
            ("GET", R(r"/app/installations/(\d+)"), self._get_installation),
            ("POST", R(r"/app/installations/(\d+)/access_tokens"), self._create_token),
            ("GET", R(r"/installation/repositories"), self._installation_repos),
            ("GET", R(r"/user"), self._user),
            ("GET", R(r"/user/repos"), self._user_repos),
            ("GET", R(repo), self._get_repo),
            ("GET", R(repo + r"/issues"), self._list_issues),
            ("POST", R(repo + r"/issues"), self._create_issue),
            ("GET", R(repo + r"/issues/(\d+)"), self._get_issue),
            ("PATCH", R(repo + r"/issues/(\d+)"), self._patch_issue),
            ("GET", R(repo + r"/issues/(\d+)/comments"), self._list_comments),
            ("POST", R(repo + r"/issues/(\d+)/comments"), self._create_comment),
            ("GET", R(repo + r"/contents/(.+)"), self._get_contents),
            ("PUT", R(repo + r"/contents/(.+)"), self._put_contents),
            ("GET", R(repo + r"/pulls"), self._list_pulls),
            ("POST", R(repo + r"/pulls"), self._create_pull),
            ("GET", R(repo + r"/pulls/(\d+)"), self._get_pull),
            ("PUT", R(repo + r"/pulls/(\d+)/merge"), self._merge_pull),
            ("GET", R(repo + r"/commits/(.+)"), self._get_commit),
            ("POST", R(repo + r"/git/refs"), self._create_ref),
            ("DELETE", R(repo + r"/git/refs/heads/(.+)"), self._delete_ref),
        ]

    # ---- authentication ---------------------------------------------------------------

    def _app(self, auth: str) -> httpx.Response | None:
        """None when `auth` is a valid App JWT, else the 401 GitHub sends."""
        token = auth.removeprefix("Bearer ")
        try:
            claims = jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"],
                                options={"verify_exp": False, "verify_iat": False,
                                         "verify_nbf": False,
                                         "require": ["iat", "exp", "iss"]})
        except jwt.PyJWTError:
            return _msg(401, "A JSON web token could not be decoded")
        now = self.clock()
        if str(claims["iss"]) != self.app_id or claims["exp"] < now or \
                claims["iat"] > now + 1 or claims["exp"] - now > 600 + 1:
            return _msg(401, "'Expiration time' claim ('exp') is too far in the future")
        self.jwt_claims.append(claims)
        return None

    def _who(self, auth: str):
        value = auth.removeprefix("Bearer ")
        if value == self.pat:
            return "pat", None
        tok = self.tokens.get(value)
        if tok is not None and tok["expires_at"] > self.clock():
            return "inst", tok
        return None, None

    def _installation_repo_names(self, inst: dict) -> list[str]:
        return sorted(k for k in self.repos if k.split("/")[0] == inst["account"])

    def _access(self, auth: str, owner: str, name: str, unit: str, level: str):
        """(repo, None) or (None, the response GitHub would send)."""
        full = f"{owner}/{name}".lower()
        if full in self.renamed:
            new = self.renamed[full]
            return None, httpx.Response(301, json={"message": "Moved Permanently",
                                                   "url": f"/repositories/{new}"})
        kind, tok = self._who(auth)
        if kind is None:
            return None, _msg(401, "Bad credentials")
        repo = self.repos.get(full)
        if repo is None:
            return None, _msg(404, "Not Found")
        if kind == "pat":
            return repo, None
        inst = self.installations[tok["installation"]]
        if full not in self._installation_repo_names(inst) or \
                (tok["repos"] is not None and full not in tok["repos"]):
            return None, _msg(404, "Not Found")
        if RANK[tok["permissions"].get(unit)] < RANK[level]:
            return None, _msg(403, "Resource not accessible by integration")
        return repo, None

    # ---- /app and /installation ---------------------------------------------------------

    def _get_installation(self, req, auth, q, body, inst_id):
        if (bad := self._app(auth)) is not None:
            return bad
        inst = self.installations.get(inst_id)
        if inst is None:
            return _msg(404, "Not Found")
        return _json(200, {"id": int(inst_id), "app_id": int(self.app_id),
                           "app_slug": APP_SLUG, "account": {"login": inst["account"].title()},
                           "permissions": inst["permissions"],
                           "repository_selection": inst["selection"]})

    def _create_token(self, req, auth, q, body, inst_id):
        if (bad := self._app(auth)) is not None:
            return bad
        inst = self.installations.get(inst_id)
        if inst is None:
            return _msg(404, "Not Found")
        body = body or {}
        perms = body.get("permissions")
        if perms is None:                       # GitHub: omitted = everything installed
            perms = dict(inst["permissions"])
        for unit, level in perms.items():
            if RANK.get(level, 99) > RANK[inst["permissions"].get(unit)]:
                return _msg(422, "The permissions requested are not granted to this "
                                 "installation.")
        names = body.get("repositories")
        repos = None
        if names is not None:
            installed = self._installation_repo_names(inst)
            repos = {f"{inst['account']}/{n.lower()}" for n in names}
            if not repos <= set(installed):
                return _msg(422, "There is at least one repository that does not exist or "
                                 "is not accessible to the parent installation.")
        granted = {**perms, "metadata": "read"}
        if self.widen_tokens:
            granted["administration"] = "write"
        value = f"ghs_fake{next(self._ids):06d}" + "t" * 30
        expires = self.clock() + 3600
        self.tokens[value] = {"installation": inst_id, "repos": repos,
                              "permissions": granted, "expires_at": expires}
        out = {"token": value, "permissions": granted,
               "expires_at": datetime.fromtimestamp(expires, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "repository_selection": "selected" if repos is not None else inst["selection"]}
        if repos is not None:
            out["repositories"] = [{"name": r.split("/")[1], "full_name": r}
                                   for r in sorted(repos)]
        return _json(201, out)

    def _page(self, rows: list, q: dict) -> list:
        per, page = int(q.get("per_page", 30)), int(q.get("page", 1))
        return rows[(page - 1) * per: page * per]

    def _repo_json(self, r: Repo) -> dict:
        return {"id": r.id, "full_name": r.full_name, "name": r.full_name.split("/")[1],
                "private": r.private, "default_branch": r.default_branch,
                "description": f"about {r.full_name}", "archived": False,
                "updated_at": "2026-01-01T00:00:00Z",
                "html_url": f"https://github.com/{r.full_name}"}

    def _installation_repos(self, req, auth, q, body):
        kind, tok = self._who(auth)
        if kind != "inst":
            return _msg(401, "Bad credentials")
        inst = self.installations[tok["installation"]]
        names = [n for n in self._installation_repo_names(inst)
                 if tok["repos"] is None or n in tok["repos"]]
        rows = [self._repo_json(self.repos[n]) for n in names]
        return _json(200, {"total_count": len(rows), "repositories": self._page(rows, q)})

    def _user(self, req, auth, q, body):
        kind, _ = self._who(auth)
        if kind == "pat":
            return _json(200, {"login": "octo"})
        if kind == "inst":
            return _msg(403, "Resource not accessible by integration")
        return _msg(401, "Bad credentials")

    def _user_repos(self, req, auth, q, body):
        kind, _ = self._who(auth)
        if kind != "pat":
            return _msg(401, "Bad credentials")
        rows = [self._repo_json(r) for _, r in sorted(self.repos.items())]
        return _json(200, self._page(rows, q))

    # ---- repositories -------------------------------------------------------------------

    def _get_repo(self, req, auth, q, body, o, n):
        repo, bad = self._access(auth, o, n, "metadata", "read")
        return bad or _json(200, self._repo_json(repo))

    def _list_issues(self, req, auth, q, body, o, n):
        repo, bad = self._access(auth, o, n, "issues", "read")
        if bad:
            return bad
        state = q.get("state", "open")
        rows = [i for _, i in sorted(repo.issues.items())
                if state == "all" or i["state"] == state]
        return _json(200, self._page(rows, q))

    def _get_issue(self, req, auth, q, body, o, n, num):
        repo, bad = self._access(auth, o, n, "issues", "read")
        if bad:
            return bad
        issue = repo.issues.get(int(num))
        return _json(200, issue) if issue else _msg(404, "Not Found")

    def _create_issue(self, req, auth, q, body, o, n):
        repo, bad = self._access(auth, o, n, "issues", "write")
        if bad:
            return bad
        num = max(list(repo.issues) + list(repo.pulls) + [0]) + 1
        repo.issues[num] = {**self._issue(num, body["title"], "open"), "body": body.get("body")}
        return _json(201, repo.issues[num])

    def _patch_issue(self, req, auth, q, body, o, n, num):
        repo, bad = self._access(auth, o, n, "issues", "write")
        if bad:
            return bad
        issue = repo.issues.get(int(num))
        if issue is None:
            return _msg(404, "Not Found")
        issue.update({k: v for k, v in body.items() if k in ("state", "state_reason")})
        return _json(200, issue)

    def _list_comments(self, req, auth, q, body, o, n, num):
        repo, bad = self._access(auth, o, n, "issues", "read")
        return bad or _json(200, repo.comments.get(int(num), []))

    def _create_comment(self, req, auth, q, body, o, n, num):
        repo, bad = self._access(auth, o, n, "issues", "write")
        if bad:
            return bad
        if int(num) not in repo.issues:
            return _msg(404, "Not Found")
        c = {"id": next(self._ids), "user": {"login": "aab[bot]"}, "body": body["body"],
             "html_url": "https://github.com/c"}
        repo.comments.setdefault(int(num), []).append(c)
        return _json(201, c)

    # ---- contents ------------------------------------------------------------------------

    def _resolve_branch(self, repo: Repo, ref: str | None) -> str | None:
        ref = ref or repo.default_branch
        if ref in repo.branches:
            return ref
        for b, sha in repo.branches.items():
            if sha == ref:
                return b
        return None

    def _get_contents(self, req, auth, q, body, o, n, path):
        repo, bad = self._access(auth, o, n, "contents", "read")
        if bad:
            return bad
        branch = self._resolve_branch(repo, q.get("ref"))
        files = repo.files.get(branch or "", {})
        if path in files:
            data = files[path]
            return _json(200, {"type": "file", "encoding": "base64", "path": path,
                               "size": len(data), "sha": sha_of(data),
                               "content": base64.encodebytes(data).decode()})
        entries = sorted({p[len(path) + 1:].split("/")[0] for p in files
                          if p.startswith(path + "/")})
        if entries:
            return _json(200, [{"name": e, "path": f"{path}/{e}", "type": "file", "size": 1}
                               for e in entries])
        return _msg(404, "Not Found")

    def _put_contents(self, req, auth, q, body, o, n, path):
        repo, bad = self._access(auth, o, n, "contents", "write")
        if bad:
            return bad
        branch = body.get("branch") or repo.default_branch
        if branch not in repo.branches:
            return _msg(404, f"Branch {branch} not found")
        files = repo.files.setdefault(branch, {})
        exists = path in files
        if exists and body.get("sha") is None:
            return _msg(422, 'Invalid request.\n\n"sha" wasn\'t supplied.')
        if exists and body["sha"] != sha_of(files[path]):
            return _msg(409, f"{path} does not match {body['sha']}")
        data = base64.b64decode(body["content"])
        files[path] = data
        commit = sha_of(f"{branch}:{path}:{next(self._ids)}".encode())
        repo.branches[branch] = commit
        return _json(200 if exists else 201, {"content": {"path": path, "sha": sha_of(data)},
                                              "commit": {"sha": commit}})

    # ---- pulls and refs ------------------------------------------------------------------

    def _list_pulls(self, req, auth, q, body, o, n):
        repo, bad = self._access(auth, o, n, "pull_requests", "read")
        if bad:
            return bad
        state = q.get("state", "open")
        rows = [p for _, p in sorted(repo.pulls.items())
                if state == "all" or p["state"] == state]
        return _json(200, self._page(rows, q))

    def _get_pull(self, req, auth, q, body, o, n, num):
        repo, bad = self._access(auth, o, n, "pull_requests", "read")
        if bad:
            return bad
        pr = repo.pulls.get(int(num))
        return _json(200, pr) if pr else _msg(404, "Not Found")

    def _create_pull(self, req, auth, q, body, o, n):
        repo, bad = self._access(auth, o, n, "pull_requests", "write")
        if bad:
            return bad
        if body["head"] not in repo.branches or body["base"] not in repo.branches:
            return _msg(422, "Validation Failed")
        num = max(list(repo.issues) + list(repo.pulls) + [0]) + 1
        repo.pulls[num] = {"number": num, "title": body["title"], "state": "open",
                           "merged": False, "draft": body.get("draft", False),
                           "head": {"ref": body["head"], "sha": repo.branches[body["head"]],
                                    "repo": {"id": repo.id}},
                           "base": {"ref": body["base"], "sha": repo.branches[body["base"]],
                                    "repo": {"id": repo.id}},
                           "html_url": f"https://github.com/{repo.full_name}/pull/{num}",
                           "mergeable": True}
        return _json(201, repo.pulls[num])

    def _merge_pull(self, req, auth, q, body, o, n, num):
        repo, bad = self._access(auth, o, n, "contents", "write")
        if bad:
            return bad
        _, bad = self._access(auth, o, n, "pull_requests", "write")
        if bad:
            return bad
        pr = repo.pulls.get(int(num))
        if pr is None:
            return _msg(404, "Not Found")
        if not pr.get("mergeable", True):
            return _msg(405, "Pull Request is not mergeable")
        if body.get("sha") and body["sha"] != pr["head"]["sha"]:
            return _msg(409, "Head branch was modified. Review and try the merge again.")
        pr.update({"state": "closed", "merged": True})
        merged = sha_of(f"merge:{num}".encode())
        repo.branches[pr["base"]["ref"]] = merged
        return _json(200, {"sha": merged, "merged": True, "message": "merged"})

    def _get_commit(self, req, auth, q, body, o, n, ref):
        repo, bad = self._access(auth, o, n, "contents", "read")
        if bad:
            return bad
        sha = repo.branches.get(ref) or (ref if ref in repo.branches.values() else None)
        if sha is None:
            return _msg(422, f"No commit found for SHA: {ref}")
        if "vnd.github.sha" in req.headers.get("accept", ""):
            return httpx.Response(200, text=sha)
        return _json(200, {"sha": sha})

    def _create_ref(self, req, auth, q, body, o, n):
        repo, bad = self._access(auth, o, n, "contents", "write")
        if bad:
            return bad
        name = body["ref"].removeprefix("refs/heads/")
        if name in repo.branches:
            return _msg(422, "Reference already exists")
        repo.branches[name] = body["sha"]
        return _json(201, {"ref": body["ref"], "object": {"sha": body["sha"]}})

    def _delete_ref(self, req, auth, q, body, o, n, branch):
        repo, bad = self._access(auth, o, n, "contents", "write")
        if bad:
            return bad
        if branch not in repo.branches:
            return _msg(422, "Reference does not exist")
        del repo.branches[branch]
        return httpx.Response(204)
