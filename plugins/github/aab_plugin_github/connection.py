"""The `github_app` connection: the App credential, the installation, token minting.

Everything that can act on GitHub lives here, inside the plugin container;
the broker holds none of it (docs/plugins/github.md).

  config slot "github"      app_id, app_slug, private_key_path (written by the
                            adapter's configure), private_key_pem and pat
                            (written by the runtime's /configure). Owned by
                            the owner's config form.
  state slot "github_app"   installation_id, installation_account and the
                            pending connect state nonce. A separate file on
                            purpose: /configure can only ever write the
                            config slot, so nothing relayed as "config" can
                            plant an installation id.

Modes, derived on every call from what is configured (so a /configure takes
effect immediately):
  app   app_id and a private key (private_key_pem, or the file at
        private_key_path inside /run/secrets/github).
        Every call gets an installation token narrowed to exactly its
        repositories and permissions: enforcement "target".
  pat   no App, but a PAT. GitHub cannot narrow it: enforcement "proxy",
        requirements are ignored, the adapter's visibility checks are all
        that restrict it.
  None  nothing configured: not connected.

`mint()` rules, each failing closed:
  * permissions come only from the requirement and must be non-empty
    (GitHub reads a missing `permissions` as "everything installed");
  * a permission the installation does not have is a 403 before any token
    request ("installation lacks permission contents:write");
  * repositories are sent by NAME, which GitHub resolves inside the
    installation's own account, so a repository under another owner is
    dropped rather than sent (sending `a` for `other/a` would mint a token
    for `<account>/a`); if nothing is left the call is a 404, never an
    unrestricted token;
  * the token GitHub returns is checked: permissions or repositories wider
    than requested are refused (503) and never cached or used;
  * tokens are cached in memory by the exact (installation, repositories,
    permissions) tuple for at most 50 minutes; never logged, persisted or
    shown in a repr.
"""

import hmac
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from datetime import datetime

from aab_plugin_runtime import AdapterError
from aab_plugin_runtime.logging_setup import kv

from .api import NOT_FOUND, GitHubAPI, GitHubError
from .app_jwt import KEY_DIR, BadKeyPath, InvalidKey, app_jwt, load_private_key, read_key_file
from .ids import normalize_owner, normalize_repo, split_repo
from .scope import credential, missing, within
from .tokens import GitHubToken, TokenCache

log = logging.getLogger("aab_plugin_github")

STATE_TTL_SECONDS = 600               # connect state nonce: 10 minutes, single use
INSTALLATION_TTL_SECONDS = 600        # re-read the installed permission set this often
REPOS_TTL_SECONDS = 300               # the resolve() picker list
MAX_REPO_PAGES = 10                   # 1000 repositories are plenty for a picker
APP_ID_RE = re.compile(r"[0-9]{1,12}")
APP_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
INSTALLATION_ID_RE = re.compile(r"[0-9]{1,20}")
INSTALL_URL = "https://github.com/apps/{slug}/installations/new?state={state}"


class Unreachable(GitHubError):
    """Every requested repository belongs to another account than the
    installation's: known without asking GitHub, and never "all" instead."""

    def __init__(self):
        super().__init__(404, NOT_FOUND)


class MemorySlot:
    """Stand-in for the runtime's encrypted SecretSlot until `serve()` binds
    the real one (the adapter is also usable standalone in tests)."""

    def __init__(self):
        self._values: dict[str, str] = {}

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._values.get(name, default)

    def set(self, name: str, value: str | None) -> None:
        if value in (None, ""):
            self._values.pop(name, None)
        else:
            self._values[name] = value

    def wipe(self) -> None:
        self._values.clear()

    def __repr__(self) -> str:
        return "MemorySlot(<redacted>)"


class GitHubAppConnection:
    kind = "github_app"
    slot = "github_app"          # the runtime binds this connection its own slot

    def __init__(self, api: GitHubAPI, *, key_dir: str = KEY_DIR,
                 clock: Callable[[], float] = time.time):
        self.api = api
        self._key_dir = key_dir
        self._clock = clock
        self._config = MemorySlot()          # adapter hands in the "github" slot
        self._state = MemorySlot()           # runtime binds the "github_app" slot
        self._tokens = TokenCache()
        self._installation: tuple[float, str, dict] | None = None
        self._repos: tuple[float, str, list[dict]] | None = None
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return f"GitHubAppConnection(mode={self.mode()!r})"

    # ---- wiring ----------------------------------------------------------------

    def bind_secrets(self, slot) -> None:
        self._state = slot

    def use_config(self, slot) -> None:
        self._config = slot

    def reset(self) -> None:
        """Forget every minted token and cached lookup (config or install changed)."""
        with self._lock:
            self._tokens.clear()
            self._installation = None
            self._repos = None

    def invalidate(self, token: GitHubToken) -> None:
        self._tokens.drop(token)

    # ---- configuration (re-read on every use) ------------------------------------

    def app_id(self) -> str | None:
        v = self._config.get("app_id")
        return v if isinstance(v, str) and APP_ID_RE.fullmatch(v) else None

    def app_slug(self) -> str | None:
        v = self._config.get("app_slug")
        return v if isinstance(v, str) and APP_SLUG_RE.fullmatch(v) else None

    def pat(self) -> str | None:
        return self._config.get("pat") or None

    def _pem_text(self) -> str | None:
        """The console's key, else the key file at private_key_path (confined
        to the key directory on every read, not just when configured)."""
        pem = self._config.get("private_key_pem")
        if pem:
            return pem
        path = self._config.get("private_key_path")
        if path:
            try:
                return read_key_file(path, self._key_dir)
            except BadKeyPath:
                return None
        return None

    def mode(self) -> str | None:
        if self.app_id() and self._pem_text():
            return "app"
        if self.pat():
            return "pat"
        return None

    def installation_id(self) -> str | None:
        v = self._state.get("installation_id")
        return v if isinstance(v, str) and INSTALLATION_ID_RE.fullmatch(v) else None

    # ---- the App's own authentication ----------------------------------------------

    def _jwt(self) -> str:
        app_id, pem = self.app_id(), self._pem_text()
        if not app_id or not pem:
            raise GitHubError(503, "the GitHub App is not configured (app_id, private key)")
        try:
            key = load_private_key(pem)
        except InvalidKey:
            raise GitHubError(503, "the GitHub App private key is invalid") from None
        return app_jwt(app_id, key, self._clock())

    def _fetch_installation(self, inst: str) -> dict:
        resp = self.api.request("GET", f"/app/installations/{inst}", bearer=self._jwt(),
                                before_effect=True)
        info = self.api.json(resp, before_effect=True)
        account = info.get("account") if isinstance(info, dict) else None
        try:
            login = normalize_owner(account.get("login") if isinstance(account, dict) else None)
        except AdapterError:
            raise GitHubError(503, "GitHub returned an unexpected installation") from None
        perms = info.get("permissions")
        perms = {k: v for k, v in perms.items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(perms, dict) else {}
        return {"id": inst, "app_id": info.get("app_id"), "app_slug": info.get("app_slug"),
                "account": login, "permissions": perms,
                "repository_selection": info.get("repository_selection")}

    def installation(self, *, force: bool = False) -> dict:
        inst = self.installation_id()
        if not inst:
            raise GitHubError(503, "the GitHub App is not installed; connect it in the console")
        now = self._clock()
        with self._lock:
            c = self._installation
            if not force and c and c[1] == inst and now - c[0] < INSTALLATION_TTL_SECONDS:
                return c[2]
        info = self._fetch_installation(inst)
        with self._lock:
            self._installation = (now, inst, info)
        return info

    # ---- connect flow ------------------------------------------------------------------

    def start(self, enabled_plugins: list[str]) -> dict:
        mode = self.mode()
        if mode == "pat":
            return {"kind": "none"}
        if mode is None:
            raise AdapterError(400, "configure app_id, app_slug and private_key_pem "
                                    "(or a pat) first")
        slug = self.app_slug()
        if not slug:
            raise AdapterError(400, "app_slug is not configured")
        state = secrets.token_urlsafe(24)
        self._state.set("connect_state", state)
        self._state.set("connect_state_expires_at", str(int(self._clock()) + STATE_TTL_SECONDS))
        return {"kind": "install", "url": INSTALL_URL.format(slug=slug, state=state),
                "state": state}

    def finish(self, code: str | None, state: str | None,
               installation_id: str | None) -> dict:
        """Record the installation GitHub redirected back with. `code` (only
        sent when the App asks for user authorization) is ignored: the plugin
        never acts as a user."""
        mode = self.mode()
        if mode == "pat":
            return {"ok": True, "mode": "pat"}
        if mode is None:
            raise AdapterError(400, "configure the GitHub App first")
        self._consume_state(state)
        if not isinstance(installation_id, str) or \
                not INSTALLATION_ID_RE.fullmatch(installation_id):
            raise AdapterError(400, "installation_id must be the numeric id GitHub sent")
        try:
            info = self._fetch_installation(installation_id)
        except GitHubError as exc:
            if exc.github_status == 404:
                raise AdapterError(400, "this App has no such installation") from None
            raise
        app_id = self.app_id()
        if info["app_id"] is not None and str(info["app_id"]) != app_id:
            raise AdapterError(400, "the installation belongs to another App")
        with self._lock:
            self._state.set("installation_id", installation_id)
            self._state.set("installation_account", info["account"])
            self.reset()
            self._installation = (self._clock(), installation_id, info)
        log.info("github app installation recorded %s", kv(
            installation=installation_id, account=info["account"],
            repository_selection=info["repository_selection"]))
        return {"ok": True, "mode": "app", "account": info["account"],
                "repository_selection": info["repository_selection"],
                "installed_permissions": info["permissions"]}

    def _consume_state(self, state: str | None) -> None:
        """Single use, 10 minutes. The stored nonce is forgotten on ANY finish
        attempt, so a guess cannot be retried against the same nonce."""
        with self._lock:     # read-and-clear is atomic: two finishes cannot share a nonce
            expected = self._state.get("connect_state")
            expires = self._state.get("connect_state_expires_at") or ""
            self._state.set("connect_state", None)
            self._state.set("connect_state_expires_at", None)
        if not isinstance(state, str) or not state:
            raise AdapterError(400, "missing state: start the connection from the console")
        if not expected or not hmac.compare_digest(state.encode(), expected.encode()):
            raise AdapterError(400, "unknown or already used state: start the connection again")
        if not expires.isdigit() or int(expires) < self._clock():
            raise AdapterError(400, "the connect state expired: start the connection again")

    def qr_png(self) -> bytes:
        raise AdapterError(404, "GitHub has no QR flow")

    def disconnect(self) -> dict:
        """Forget the installation, the pending state and every minted token.
        In PAT mode the PAT IS the connection, so it is wiped too; the App's
        own id and key stay (they are App config, and without an installation
        they reach no repository). Uninstalling the App on GitHub is separate."""
        with self._lock:
            self._state.wipe()
            if self._config.get("pat"):
                self._config.set("pat", None)
            self.reset()
        return {"ok": True}

    # ---- status ------------------------------------------------------------------------

    def status(self) -> dict:
        """Never raises for GitHub being slow or down: mode and enforcement
        come from local config, so the broker always learns them."""
        mode = self.mode()
        out = {"kind": self.kind, "mode": mode,
               "enforcement": "target" if mode == "app" else "proxy",
               "account": None, "installed_permissions": None, "repositories_count": None}
        if mode is None:
            if self.app_id() and self._config.get("private_key_path"):
                health = "the key file at private_key_path cannot be read"
            else:
                health = ("not configured: set app_id, app_slug and private_key_pem "
                          "(or private_key_path), or a pat")
            return {**out, "connected": False, "healthy": False, "health": health}
        if mode == "pat":
            return {**out, **self._pat_status()}
        return {**out, **self._app_status()}

    def _pat_status(self) -> dict:
        try:
            resp = self.api.request("GET", "/user", bearer=self.pat(), before_effect=True)
            body = self.api.json(resp, before_effect=True)
        except GitHubError as exc:
            if exc.github_status == 401:
                return {"connected": False, "healthy": False,
                        "health": "the PAT was rejected by GitHub"}
            return {"connected": True, "healthy": False, "health": f"GitHub: {exc.message}"}
        login = body.get("login") if isinstance(body, dict) else None
        return {"connected": True, "healthy": True,
                "health": "ok (PAT: every restriction is proxy-enforced)",
                "account": login if isinstance(login, str) else None}

    def _app_status(self) -> dict:
        try:
            load_private_key(self._pem_text() or "")
        except InvalidKey:
            return {"connected": False, "healthy": False,
                    "health": "the GitHub App private key is invalid"}
        if not self.installation_id():
            return {"connected": False, "healthy": False,
                    "health": "App configured but not installed: use connect"}
        try:
            info = self.installation(force=True)
        except GitHubError as exc:
            if exc.github_status == 404:
                return {"connected": False, "healthy": False,
                        "health": "installation not found on GitHub (uninstalled?): reconnect"}
            if exc.github_status == 401:
                return {"connected": False, "healthy": False,
                        "health": "GitHub rejected the App JWT: check app_id and the key"}
            return {"connected": True, "healthy": False, "health": f"GitHub: {exc.message}"}
        out = {"connected": True, "account": info["account"],
               "installed_permissions": info["permissions"],
               "repository_selection": info["repository_selection"]}
        try:
            token = self.mint({"permissions": {"metadata": "read"}})
            resp = self.api.request("GET", "/installation/repositories",
                                    bearer=token.bearer(), params={"per_page": 1},
                                    before_effect=True)
            body = self.api.json(resp, before_effect=True)
            count = body.get("total_count") if isinstance(body, dict) else None
        except AdapterError as exc:
            return {**out, "healthy": False, "health": f"cannot list repositories: {exc.message}"}
        return {**out, "healthy": True, "health": "ok",
                "repositories_count": count if isinstance(count, int) else None}

    # ---- minting -----------------------------------------------------------------------

    def mint(self, requirements: dict) -> GitHubToken:
        """A bearer token for exactly `requirements` ({"permissions": {...},
        "resources": {"repo": [ids]}}); see the module docstring."""
        mode = self.mode()
        if mode == "pat":
            log.info("github credential %s", kv(mode="pat", enforcement="proxy"))
            return GitHubToken("pat", None, (), self.pat())
        if mode is None:
            raise GitHubError(503, "GitHub is not configured")
        inst = self.installation_id()
        if not inst:
            raise GitHubError(503, "the GitHub App is not installed; connect it in the console")
        perms, repos = credential({"credential": requirements})
        info = self.installation()
        lacking = missing(perms, info["permissions"])
        if lacking:
            raise GitHubError(403, f"installation lacks permission {', '.join(lacking)}")
        names = self._names(repos, info["account"])
        key = (inst, names, tuple(sorted(perms.items())))
        # What the token is FOR (permission and repository names), never
        # the token.
        scope = {"permissions": [f"{k}:{v}" for k, v in sorted(perms.items())],
                 "repos": "all" if names is None else list(names)}
        now = self._clock()
        hit = self._tokens.get(key, now)
        if hit is not None:
            log.info("github installation token %s", kv(source="cache", **scope))
            return hit
        body: dict = {"permissions": dict(sorted(perms.items()))}
        if names is not None:
            body["repositories"] = list(names)
        try:
            resp = self.api.request("POST", f"/app/installations/{inst}/access_tokens",
                                    bearer=self._jwt(), json=body, before_effect=True)
        except GitHubError as exc:
            raise self._mint_error(exc, perms) from None
        data = self.api.json(resp, before_effect=True)
        token = _accept(data, perms, names)
        self._tokens.put(key, token, now, _expiry(data.get("expires_at")))
        log.info("github installation token %s", kv(source="minted", **scope))
        return token

    def _names(self, repos: list[str] | None, account: str) -> tuple[str, ...] | None:
        if repos is None:
            return None
        names = sorted({name for owner, name in map(split_repo, repos) if owner == account})
        if not names:
            # Nothing this installation can reach. Never fall back to "all".
            raise Unreachable()
        return tuple(names)

    def _mint_error(self, exc: GitHubError, perms: dict[str, str]) -> GitHubError:
        if exc.github_status == 422:
            # Either a permission the installation no longer has, or a
            # repository it cannot reach. Re-read the installation to tell.
            try:
                info = self.installation(force=True)
            except GitHubError as again:
                return again
            lacking = missing(perms, info["permissions"])
            if lacking:
                return GitHubError(403, f"installation lacks permission {', '.join(lacking)}")
            return GitHubError(404, NOT_FOUND)
        if exc.github_status == 404:
            return GitHubError(503, "the GitHub App installation is gone: reconnect")
        if exc.status not in (429, 503):
            # Minting has no side effect on the target: whatever went wrong,
            # the action itself was not attempted.
            return GitHubError(503, f"could not mint a GitHub token ({exc.message})",
                               github_status=exc.github_status)
        return exc

    # ---- the picker's repository list ----------------------------------------------------

    def repositories(self) -> list[dict]:
        """Every repository the credential reaches: [{"id", "full_name"}]."""
        mode = self.mode()
        cache_key = f"{mode}:{self.installation_id()}"
        now = self._clock()
        with self._lock:
            c = self._repos
            if c and c[1] == cache_key and now - c[0] < REPOS_TTL_SECONDS:
                return c[2]
        if mode == "app":
            bearer, path, field = self.mint({"permissions": {"metadata": "read"}}).bearer(), \
                "/installation/repositories", "repositories"
        elif mode == "pat":
            bearer, path, field = self.pat(), "/user/repos", None
        else:
            raise GitHubError(503, "GitHub is not configured")
        out: list[dict] = []
        for page in range(1, MAX_REPO_PAGES + 1):
            resp = self.api.request("GET", path, bearer=bearer,
                                    params={"per_page": 100, "page": page}, before_effect=True)
            body = self.api.json(resp, before_effect=True)
            rows = body.get(field) if field and isinstance(body, dict) else body
            if not isinstance(rows, list):
                raise GitHubError(503, "GitHub returned an unexpected repository list")
            for r in rows:
                full = r.get("full_name") if isinstance(r, dict) else None
                try:
                    out.append({"id": normalize_repo(full), "full_name": full})
                except AdapterError:
                    continue
            if len(rows) < 100:
                break
        with self._lock:
            self._repos = (now, cache_key, out)
        return out


def _accept(data, perms: dict[str, str], names: tuple[str, ...] | None) -> GitHubToken:
    """Refuse a token wider than what was asked (belt and braces over GitHub)."""
    value = data.get("token") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        raise GitHubError(503, "GitHub returned no token")
    got = data.get("permissions")
    # metadata:read is part of every installation token, asked for or not.
    allowed = {**perms, "metadata": perms.get("metadata", "read")}
    if not isinstance(got, dict) or not within(got, allowed):
        log.warning("refusing an installation token wider than requested %s",
                    kv(units=sorted(got) if isinstance(got, dict) else "?"))
        raise GitHubError(503, "GitHub issued a token wider than requested; refused")
    if names is not None:
        repos = data.get("repositories")
        got_names = {r.get("name", "").lower() for r in repos if isinstance(r, dict)} \
            if isinstance(repos, list) else None
        if data.get("repository_selection") == "all" or got_names is None or \
                not got_names <= set(names):
            log.warning("refusing an installation token for more repositories than requested")
            raise GitHubError(503, "GitHub issued a token wider than requested; refused")
    return GitHubToken("app", names, tuple(sorted(perms.items())), value)


def _expiry(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
