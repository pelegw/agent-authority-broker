"""The GitHub plugin adapter: every manifest action, inside the broker's scope.

Served by `aab_plugin_runtime` in the plugin-github container; the broker
reaches it over the plugin API and never holds a GitHub credential.

`perform` runs the same fixed sequence for every action, and nothing reaches
GitHub before all of it has passed:

  1. read the CallScope (fail closed: malformed is 400, `allow_only: []`
     means nothing) and the credential requirements; requirements asking
     for more than this action's own manifest `target_permissions` are
     refused, so a confused caller can never widen a token;
  2. normalize the repository and check it against `visibility.repo`
     (deny wins) and the requirement's repo list: hidden, denied or out of
     scope is the same 404 as a repository that does not exist, and no
     token is minted for it;
  3. for actions that change a branch named in their params, check it
     against `visibility.branch` (exact strings, as the grant algebra
     defines `pattern`): denied 404, outside the selector 403;
  4. mint: an installation token for exactly this repository (list_repos:
     the capability's repo list minus denied ones) and exactly the action's
     permissions; in PAT mode the PAT, and steps 1-3 are all the
     enforcement there is;
  5. run the handler (reads.py / writes.py) with a `Call` carrying that token.

`resolve` and `label` apply no visibility: they serve the owner's console
and approval cards, and the broker filters `resolve` for agents itself.
"""

import re
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from aab_plugin_runtime import AdapterError, Result

from . import reads, writes
from .api import NOT_FOUND, GitHubAPI
from .app_jwt import InvalidKey, load_private_key
from .call import Call
from .connection import APP_ID_RE, APP_SLUG_RE, GitHubAppConnection, MemorySlot, Unreachable
from .ids import check_branch, normalize_repo
from .scope import branch_allowed, credential, is_visible, visibility, within

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")
RESOLVE_LIMIT = 50
# GitHub tokens: ghp_/gho_/ghu_/ghs_/github_pat_ prefixes, or 40-hex classic.
PAT_RE = re.compile(r"[A-Za-z0-9_]{20,255}")
# The param naming the branch an action changes. merge_pr's branch (the PR's
# base) is known only after reading the PR, so writes.merge_pr checks it.
BRANCH_PARAM = {"create_branch": "branch", "push_file": "branch", "create_pr": "head",
                "delete_branch": "branch"}


class GitHubAdapter:
    """The `aab_plugin_runtime` PluginAdapter for plugin id `github`."""

    def __init__(self, api: GitHubAPI | None = None, *, key_path: str | None = None,
                 manifest_path: Path = MANIFEST_PATH, clock: Callable[[], float] = time.time):
        self.manifest = yaml.safe_load(Path(manifest_path).read_text(encoding="utf-8"))
        self.api = api or GitHubAPI(clock=clock)
        self.connection = GitHubAppConnection(self.api, key_path=key_path, clock=clock)
        self._slot = MemorySlot()
        self.connection.use_config(self._slot)
        self._actions: dict[str, Callable[[Call], Result]] = {
            "list_repos": reads.list_repos, "list_issues": reads.list_issues,
            "get_issue": reads.get_issue, "get_file": reads.get_file,
            "list_prs": reads.list_prs,
            "create_issue": writes.create_issue, "comment_issue": writes.comment_issue,
            "close_issue": writes.close_issue, "create_branch": writes.create_branch,
            "push_file": writes.push_file, "create_pr": writes.create_pr,
            "merge_pr": writes.merge_pr, "delete_branch": writes.delete_branch,
        }
        actions = self.manifest.get("actions", [])
        # The manifest is the contract the broker enforces against; a declared
        # action this code does not implement (or the reverse) is a packaging
        # bug, so the container refuses to start.
        declared = {a["name"] for a in actions}
        if declared != set(self._actions):
            raise RuntimeError(f"manifest/adapter action mismatch: "
                               f"{sorted(declared ^ set(self._actions))}")
        self._needs = {a["name"]: dict(a.get("target_permissions") or {}) for a in actions}
        if not all(self._needs.values()):
            raise RuntimeError("every github action must declare target_permissions")

    def __repr__(self) -> str:
        return "GitHubAdapter()"

    # ---- lifecycle -------------------------------------------------------------

    def bind_secrets(self, slot) -> None:
        """The runtime's read-write handle on this plugin's config slot, where
        /configure has already written private_key_pem and pat."""
        self._slot = slot
        self.connection.use_config(slot)

    def configure(self, config: dict, secrets) -> None:
        """Validate and persist the console config. app_id/app_slug are kept in
        the encrypted slot too, so a container restart does not lose them
        (the broker sends config only on enable and on change). A secret that
        does not validate is wiped again before refusing, so a bad paste is
        never left behind to be used."""
        if not isinstance(config, dict):
            raise AdapterError(400, "config must be an object")
        app_id = _opt(config, "app_id", APP_ID_RE, "app_id must be the numeric App ID")
        slug = _opt(config, "app_slug", APP_SLUG_RE,
                    "app_slug must be the App's URL name (lowercase letters, digits, -)")
        pem = secrets.get("private_key_pem") if secrets is not None else None
        if pem:
            try:
                load_private_key(pem)
            except InvalidKey as exc:
                self._slot.set("private_key_pem", None)
                raise AdapterError(400, f"private_key_pem: {exc}") from None
        pat = secrets.get("pat") if secrets is not None else None
        if pat and not PAT_RE.fullmatch(pat):
            self._slot.set("pat", None)
            raise AdapterError(400, "pat does not look like a GitHub token")
        self._slot.set("app_id", app_id)
        self._slot.set("app_slug", slug)
        self.connection.reset()

    def status(self) -> dict:
        """The top-level fields the broker reads; details are under
        `connection` (added by the runtime). `enforcement` is what makes the
        broker report every dimension as proxy in PAT mode."""
        c = self.connection.status()
        return {k: c[k] for k in ("connected", "healthy", "health", "enforcement", "mode")}

    # ---- resources ---------------------------------------------------------------

    def normalize(self, kind: str, value: str) -> str:
        if kind == "repo":
            return normalize_repo(value)
        if kind == "branch":
            return check_branch(value)
        raise AdapterError(400, f"unknown resource kind {kind!r}")

    def resolve(self, kind: str, query: str, limit: int) -> list[dict]:
        if kind != "repo":
            raise AdapterError(400, "only repositories can be resolved")
        q = (query or "").strip().lower() if isinstance(query, str) else ""
        n = max(1, min(limit if isinstance(limit, int) and not isinstance(limit, bool)
                       else RESOLVE_LIMIT, RESOLVE_LIMIT))
        found = [r for r in self.connection.repositories() if q in r["id"]]
        return [{"id": r["id"], "label": r["full_name"] or r["id"], "kind": "repo"}
                for r in found[:n]]

    def label(self, kind: str, ids: list[str]) -> dict[str, str]:
        if kind not in ("repo", "branch"):
            raise AdapterError(400, f"unknown resource kind {kind!r}")
        # Ids are their own labels; no GitHub call for a cosmetic string.
        return {i: i for i in ids if isinstance(i, str)}

    # ---- actions -------------------------------------------------------------------

    def perform(self, action: str, params: dict, scope: dict) -> Result:
        handler = self._actions.get(action)
        if handler is None:
            raise AdapterError(404, f"unknown action {action!r}")
        if not isinstance(params, dict):
            raise AdapterError(400, "params must be an object")
        repo_deny, repo_allow = visibility(scope, "repo")
        branch_deny, branch_allow = visibility(scope, "branch")
        perms, cred_repos = credential(scope)
        if not within(perms, self._needs[action]):
            raise AdapterError(400, f"credential requirements exceed what {action} needs")
        deny = _deny_set(repo_deny)
        empty = Result(data={"items": [], "next_page": None})

        if action == "list_repos":
            repo, mint_repos = None, _list_scope(deny, repo_allow, cred_repos)
            if mint_repos == []:
                return empty                  # nothing visible: no token at all
        else:
            repo = normalize_repo(_required(params, "repo"))
            # Hidden, denied or out of scope: the same 404 as a missing repo,
            # decided before any token exists.
            if not is_visible(repo, list(deny), repo_allow) or \
                    (cred_repos is not None and repo not in cred_repos):
                raise AdapterError(404, NOT_FOUND)
            mint_repos = [repo]
        bparam = BRANCH_PARAM.get(action)
        if bparam:
            branch_allowed(check_branch(_required(params, bparam), bparam),
                           branch_deny, branch_allow)

        token = self._mint(action, perms, mint_repos)
        if token is None:
            return empty
        return handler(Call(
            self.api, self.connection, action, params, repo, token,
            repo_deny=frozenset(deny),
            repo_allow=None if repo_allow is None else frozenset(repo_allow),
            branch_deny=frozenset(branch_deny),
            branch_allow=None if branch_allow is None else frozenset(branch_allow)))

    def _mint(self, action: str, perms: dict, repos: list[str] | None):
        """The call's token; None only for a list_repos with nothing reachable."""
        requirements: dict = {"permissions": perms}
        if repos is not None:
            requirements["resources"] = {"repo": repos}
        try:
            return self.connection.mint(requirements)
        except Unreachable:
            if action == "list_repos":
                return None                   # all under other owners: empty list
            raise
        except AdapterError as exc:
            if exc.status != 404 or action != "list_repos" or repos is None:
                raise
        # list_repos over an explicit list naming repositories the
        # installation cannot reach (GitHub refuses the whole token then):
        # keep only the ones it can, never widening to "all". Learning which
        # ones exist takes the installation's list (a metadata-only lookup
        # inside this plugin; the rows come from the narrowed token below).
        installed = {r["id"] for r in self.connection.repositories()}
        keep = [r for r in repos if r in installed]
        if not keep:
            return None
        return self.connection.mint({"permissions": perms, "resources": {"repo": keep}})


# ---- helpers ---------------------------------------------------------------------

def _required(params: dict, name: str):
    if params.get(name) is None:
        raise AdapterError(400, f"{name} is required")
    return params[name]


def _opt(config: dict, name: str, pattern: re.Pattern, message: str) -> str | None:
    value = config.get(name)
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value.strip()):
        raise AdapterError(400, message)
    return value.strip()


def _deny_set(deny: list[str]) -> set[str]:
    """Deny ids as sent, plus their canonical form: normalizing can only add
    matches, so a deny stored in another spelling still denies."""
    out = set(deny)
    for d in deny:
        try:
            out.add(normalize_repo(d))
        except AdapterError:
            pass
    return out


def _list_scope(deny: set[str], allow: list[str] | None,
                cred: list[str] | None) -> list[str] | None:
    """Repositories to mint a list_repos token for: None (all the
    installation has; rows are filtered afterwards), or the intersection of
    every explicit list minus denied ones (possibly empty: nothing)."""
    allowed: set[str] | None = None
    for ids in (allow, cred):
        if ids is not None:
            allowed = set(ids) if allowed is None else allowed & set(ids)
    if allowed is None:
        return None
    # Only canonical ids can ever match a row; anything else is dropped here
    # rather than failing the whole list.
    return sorted(r for r in allowed if r not in deny and _canonical(r))


def _canonical(repo_id: str) -> bool:
    try:
        return normalize_repo(repo_id) == repo_id
    except AdapterError:
        return False
