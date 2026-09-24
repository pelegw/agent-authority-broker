"""The `google_oauth` connection: one Google account behind gmail, gcal and gdrive.

It owns the only long-lived Google credential in the system, the refresh
token, encrypted in this service's own secret slot `google` (under
PLUGIN_SECRETS_KEY) together with the OAuth client id/secret and the
pending connect `state`. The broker never sees any of them.

Connect flow (docs/plugin-api.md):
  start(enabled, redirect_uri)  consent URL for the union of the ENABLED
                                plugins' scopes, offline access, forced
                                consent (so Google returns a refresh token)
                                and incremental grants; a 32-byte state
                                nonce is stored (as a hash) with the
                                redirect URI, single use, 10 minutes.
  finish(code, state)           consumes the pending state on ANY attempt,
                                exchanges the code at the token endpoint
                                with the client secret and the same
                                redirect URI, stores the refresh token and
                                the granted scopes.
  disconnect()                  revokes the refresh token at Google (best
                                effort) and wipes it; the client id/secret
                                stay, they are configuration.

Minting (the target-enforced half of every call): `mint(requirements)`
takes the call's scope set from the broker's CallScope and returns an
access token for EXACTLY that set, cached in memory by the sorted scope
tuple and refreshed with `scope=<subset>` when missing or within 5 minutes
of expiry. The subset must already be granted (else 403, reconnect), and a
token Google returns with any scope beyond the request is refused: if
downscoping ever stopped working, claiming `enforced_where: target` would
be a lie, so the plugin stops instead (docs/plugins/google.md).

Tokens are never logged, never persisted and never in a repr.
"""

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from urllib.parse import urlencode, urlsplit

import httpx
from aab_plugin_runtime import AdapterError

from .scopes import manifest_scopes, names, requirement_urls, scope_url
from .transport import DEFAULT_TIMEOUT_SECONDS, open_client

log = logging.getLogger("aab_plugin_google.connection")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SLOT = "google"
STATE_TTL_SECONDS = 600
REFRESH_MARGIN_SECONDS = 300
_MAX_TOKEN_SECONDS = 3600
_CALLBACK_PATH = re.compile(r"/oauth/callback/[a-z][a-z0-9]*")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_ERROR_CODE = re.compile(r"[a-z_]{1,40}")


class AccessToken:
    """A minted access token. The value is only ever read by the API client
    to build its Authorization header; nothing prints it."""
    __slots__ = ("value", "expires_at", "scopes")

    def __init__(self, value: str, expires_at: float, scopes: tuple[str, ...]):
        self.value, self.expires_at, self.scopes = value, expires_at, scopes

    def __repr__(self) -> str:
        return f"AccessToken(<redacted>, expires_at={int(self.expires_at)})"


class GoogleOAuthConnection:
    kind = "google_oauth"
    # The runtime binds this slot (not the plugin id's) for the connection,
    # and writes `shared: true` config secrets here.
    slot = SLOT

    def __init__(self, *, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.time,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self._transport = transport
        self._clock = clock
        self._timeout = timeout
        self._plugins: dict[str, frozenset[str]] = {}
        self._slot = None
        self._cache: dict[tuple[str, ...], AccessToken] = {}
        self._lock = threading.RLock()
        self._widened = False          # Google handed back more than we asked for
        self._refused = False          # Google refused the refresh token

    def __repr__(self) -> str:        # no client secret, refresh or access token here
        return f"GoogleOAuthConnection(slot={self.slot!r}, plugins={sorted(self._plugins)})"

    # ---- wiring --------------------------------------------------------------------

    def register(self, manifest: dict) -> None:
        """Called by each hosted adapter: remember which scopes it may need."""
        self._plugins[manifest["id"]] = manifest_scopes(manifest)

    def bind_secrets(self, slot) -> None:
        self._slot = slot

    def _secrets(self):
        if self._slot is None:
            raise AdapterError(503, "secret store not bound")
        return self._slot

    def configure_client(self, client_id) -> None:
        """Store the (non-secret) client id so it survives a restart. A new
        client id invalidates the old refresh token (it belongs to the old
        client), so the credential is dropped and a reconnect is needed."""
        if not isinstance(client_id, str) or not client_id.strip():
            return
        slot = self._secrets()
        client_id = client_id.strip()
        if slot.get("client_id") == client_id:
            return
        if slot.get("client_id") and slot.get("refresh_token"):
            self._wipe_credential(slot)
        slot.set("client_id", client_id)

    # ---- connect flow ------------------------------------------------------------------

    def start(self, enabled_plugins: list[str], redirect_uri: str | None = None) -> dict:
        slot = self._secrets()
        client_id = slot.get("client_id")
        if not client_id or not slot.get("client_secret"):
            raise AdapterError(400, "set the Google OAuth client id and secret first")
        enabled = sorted({p for p in enabled_plugins if isinstance(p, str)})
        unknown = [p for p in enabled if p not in self._plugins]
        if unknown:
            raise AdapterError(400, f"not a plugin of this service: {unknown}")
        if not enabled:
            raise AdapterError(400, "enable at least one Google plugin before connecting")
        redirect = _check_redirect(redirect_uri)
        scopes = sorted({scope_url(n) for p in enabled for n in self._plugins[p]})
        nonce = secrets.token_urlsafe(32)           # 32 random bytes
        slot.set("oauth_state", json.dumps({
            "hash": _digest(nonce), "redirect_uri": redirect, "scopes": scopes,
            "expires_at": int(self._clock()) + STATE_TTL_SECONDS}))
        query = urlencode({
            "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
            "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true", "state": nonce})
        log.info("google consent started for %s", enabled)
        return {"kind": "oauth", "url": f"{AUTH_URL}?{query}"}

    def finish(self, code: str | None, state: str | None,
               installation_id: str | None = None) -> dict:
        slot = self._secrets()
        pending = _load_state(slot.get("oauth_state"))
        # Single use: any attempt, right or wrong, consumes the pending
        # state, so a guessed or replayed state gets exactly one try.
        slot.set("oauth_state", None)
        if pending is None or pending["expires_at"] <= self._clock():
            raise AdapterError(400, "no pending Google authorization; start the connection again")
        if not isinstance(state, str) or not hmac.compare_digest(_digest(state), pending["hash"]):
            raise AdapterError(400, "state does not match; start the connection again")
        if not isinstance(code, str) or not code.strip():
            raise AdapterError(400, "authorization code missing")
        body = self._token_request({
            "grant_type": "authorization_code", "code": code.strip(),
            "client_id": slot.get("client_id") or "",
            "client_secret": slot.get("client_secret") or "",
            "redirect_uri": pending["redirect_uri"]}, exchange=True)
        refresh = body.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise AdapterError(400, "Google returned no refresh token; remove this app's access "
                                    "in your Google account settings and connect again")
        granted = sorted(set(str(body.get("scope") or "").split()))
        slot.set("refresh_token", refresh)
        slot.set("granted_scopes", json.dumps(granted))
        with self._lock:
            self._cache.clear()
            self._widened = self._refused = False
        log.info("google connected; %d scopes granted", len(granted))
        return {"connected": True, "granted_scopes": names(granted),
                "missing_scopes": names(set(pending["scopes"]) - set(granted))}

    def qr_png(self) -> bytes:
        raise AdapterError(404, "Google connects with OAuth, not a QR code")

    def disconnect(self) -> dict:
        slot = self._secrets()
        refresh = slot.get("refresh_token")
        revoked = False
        if refresh:
            try:
                with open_client(self._transport, self._timeout) as c:
                    revoked = c.post(REVOKE_URL, data={"token": refresh}).status_code == 200
            except httpx.HTTPError as exc:
                log.warning("google token revocation failed: %s", type(exc).__name__)
        self._wipe_credential(slot)
        log.info("google disconnected (revoked at Google: %s)", revoked)
        return {"ok": True, "revoked": revoked}

    def _wipe_credential(self, slot) -> None:
        for name in ("refresh_token", "granted_scopes", "oauth_state"):
            slot.set(name, None)
        with self._lock:
            self._cache.clear()
            self._widened = self._refused = False

    # ---- status --------------------------------------------------------------------------

    def _granted(self, slot) -> set[str]:
        try:
            value = json.loads(slot.get("granted_scopes") or "[]")
        except ValueError:
            return set()
        return {s for s in value if isinstance(s, str)} if isinstance(value, list) else set()

    def status(self) -> dict:
        slot = self._secrets()
        return {"kind": self.kind,
                "client_configured": bool(slot.get("client_id") and slot.get("client_secret")),
                "connected": bool(slot.get("refresh_token")),
                "granted_scopes": names(self._granted(slot))}

    def plugin_status(self, plugin_id: str) -> dict:
        """Health as one hosted plugin sees it: connected, plus the scopes
        that plugin needs but the consent did not grant (a plugin enabled
        after connecting shows "reconnect needed: scopes missing")."""
        slot = self._secrets()
        connected = bool(slot.get("refresh_token"))
        granted = set(names(self._granted(slot)))
        missing = sorted(self._plugins.get(plugin_id, frozenset()) - granted)
        if not (slot.get("client_id") and slot.get("client_secret")):
            health = "not configured: set the OAuth client id and secret"
        elif not connected:
            health = "not connected"
        elif self._refused:
            health = "reconnect required: Google refused the stored authorization"
        elif self._widened:
            health = "refusing tokens: Google returned more scopes than requested"
        elif missing:
            health = "reconnect needed: scopes missing"
        else:
            health = "ok"
        return {"connected": connected, "healthy": health == "ok", "health": health,
                "granted_scopes": sorted(granted), "missing_scopes": missing,
                "enforcement": "mixed"}

    # ---- minting ---------------------------------------------------------------------------

    def mint(self, requirements: dict) -> AccessToken:
        urls = requirement_urls(requirements)
        with self._lock:
            now = self._clock()
            hit = self._cache.get(urls)
            if hit is not None and hit.expires_at - now > REFRESH_MARGIN_SECONDS:
                return hit
            slot = self._secrets()
            refresh = slot.get("refresh_token")
            if not refresh:
                raise AdapterError(503, "Google is not connected")
            if not set(urls) <= self._granted(slot):
                raise AdapterError(403, "scopes not granted; reconnect")
            body = self._token_request({
                "grant_type": "refresh_token", "refresh_token": refresh,
                "client_id": slot.get("client_id") or "",
                "client_secret": slot.get("client_secret") or "",
                "scope": " ".join(urls)}, exchange=False)
            access = body.get("access_token")
            if not isinstance(access, str) or not access:
                raise AdapterError(503, "Google returned no access token")
            got = set(str(body.get("scope") or "").split())
            if not got or not got <= set(urls):
                self._widened = True
                log.error("google returned a token wider than the requested scope set; refused")
                raise AdapterError(503, "Google returned a token wider than the requested "
                                        "scopes; refusing it")
            ttl = body.get("expires_in")
            ttl = ttl if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl > 0 \
                else _MAX_TOKEN_SECONDS
            token = AccessToken(access, now + min(ttl, _MAX_TOKEN_SECONDS), urls)
            self._cache[urls] = token
            return token

    def invalidate(self, requirements: dict) -> None:
        """Drop a cached token Google rejected (401), so the next call refreshes."""
        try:
            urls = requirement_urls(requirements)
        except AdapterError:
            return
        with self._lock:
            self._cache.pop(urls, None)

    def _token_request(self, form: dict, *, exchange: bool) -> dict:
        """POST to the token endpoint. Nothing at the target changes here,
        so every transport failure is a 503 (not performed)."""
        try:
            with open_client(self._transport, self._timeout) as c:
                resp = c.post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise AdapterError(503, f"Google token endpoint unreachable "
                                    f"({type(exc).__name__})") from exc
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code == 200 and isinstance(body, dict):
            return body
        error = body.get("error") if isinstance(body, dict) else None
        # Only Google's fixed error vocabulary is ever echoed, never a
        # description (which we do not control).
        error = error if isinstance(error, str) and _ERROR_CODE.fullmatch(error) else "error"
        if resp.status_code in (400, 401):
            if exchange:
                raise AdapterError(400, f"Google refused the authorization code ({error})")
            if error == "invalid_scope":
                raise AdapterError(403, "scopes not granted; reconnect")
            if error == "invalid_grant":
                self._refused = True
                raise AdapterError(503, "Google refused the stored authorization; "
                                        "reconnect required")
            raise AdapterError(503, f"Google refused the OAuth client ({error}); "
                                    "check the client id and secret")
        raise AdapterError(503, f"Google token endpoint answered {resp.status_code}")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_state(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    ok = (isinstance(data, dict) and isinstance(data.get("hash"), str)
          and isinstance(data.get("redirect_uri"), str)
          and isinstance(data.get("expires_at"), int)
          and isinstance(data.get("scopes"), list))
    return data if ok else None


def _check_redirect(uri: str | None) -> str:
    """The broker computes the redirect URI; this checks its shape, so a
    confused or compromised caller cannot send the code anywhere else."""
    if not isinstance(uri, str) or not uri:
        raise AdapterError(400, "redirect_uri is required")
    try:
        parts = urlsplit(uri)
        parts.port                          # raises on a malformed port
    except ValueError as exc:
        raise AdapterError(400, "redirect_uri is not a valid URL") from exc
    if (parts.scheme not in ("https", "http") or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment
            or not _CALLBACK_PATH.fullmatch(parts.path)):
        raise AdapterError(400, "redirect_uri must be <origin>/oauth/callback/<service>")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        # Google accepts plain-http redirect URIs only for loopback hosts;
        # anywhere else the code would travel in clear text.
        raise AdapterError(400, "an http redirect_uri is only allowed for localhost; "
                                "use the public https domain")
    return uri
