"""`Call`: one action's context after every scope check has passed.

The adapter builds a Call only once the repository is known to be visible,
the branch (if any) is inside the grant and a token has been minted for
exactly this call. Handlers then talk to GitHub only through it, so every
request carries the minted token and nothing else, and a 401 (a token GitHub
no longer accepts) evicts that token from the cache.
"""

from dataclasses import dataclass

import httpx

from .api import GitHubAPI, GitHubError
from .ids import repo_path
from .scope import branch_allowed, is_visible
from .tokens import GitHubToken


@dataclass
class Call:
    api: GitHubAPI
    connection: object                 # GitHubAppConnection (invalidate on 401)
    action: str
    params: dict
    repo: str | None                   # normalized owner/name; None for list_repos
    token: GitHubToken
    repo_deny: frozenset[str] = frozenset()
    repo_allow: frozenset[str] | None = None
    branch_deny: frozenset[str] = frozenset()
    branch_allow: frozenset[str] | None = None

    def __repr__(self) -> str:
        return f"Call(action={self.action!r}, repo={self.repo!r}, token={self.token!r})"

    @property
    def repo_path(self) -> str:
        return repo_path(self.repo)

    # ---- visibility ----------------------------------------------------------

    def visible_repo(self, repo_id: str) -> bool:
        return is_visible(repo_id, list(self.repo_deny),
                          None if self.repo_allow is None else list(self.repo_allow))

    def require_branch(self, branch: str) -> None:
        """For branches learned during the call (merge_pr's base)."""
        branch_allowed(branch, self.branch_deny, self.branch_allow)

    # ---- GitHub ----------------------------------------------------------------

    def request(self, method: str, path: str, *, before_effect: bool = False,
                **kwargs) -> httpx.Response:
        try:
            return self.api.request(method, path, bearer=self.token.bearer(),
                                    before_effect=before_effect, **kwargs)
        except GitHubError as exc:
            if exc.github_status == 401:
                self.connection.invalidate(self.token)
            raise

    def get_json(self, path: str, *, params=None, before_effect: bool = False):
        resp = self.request("GET", path, params=params, before_effect=before_effect)
        return self.api.json(resp, before_effect=before_effect)

    def send_json(self, method: str, path: str, body: dict):
        """The action's own side effect: after sending, failures are 502."""
        resp = self.request(method, path, json=body)
        if resp.status_code == 204 or not resp.content:
            return {}
        return self.api.json(resp)
