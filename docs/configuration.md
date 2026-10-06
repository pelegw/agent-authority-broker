# Configuration: files vs the console

**Principle: files hold only what cannot live in the database.** The owner
configures everything else from the console, and the broker stores it in
`broker.db`. Nobody has to search `.env` for a knob. A hijacked console
session must not be able to weaken what the operator decided.

## What lives in files, and why

`python scripts/init_secrets.py` writes `.env`. Do not write it by hand.
`.env` holds only two categories.

**1. Generated bootstrap secrets.** They must exist before the database is
readable, or before containers can authenticate each other.

| Key | Why it cannot be in the database |
|---|---|
| `SETUP_TOKEN` | Authorizes creating the owner account; it must exist before anyone can log in. Inert once setup completes. |
| `BROKER_SECRETS_KEY` | Decrypts the secrets entered in the console (the Telegram bot token, the installer's GitHub token); a key stored beside its ciphertext protects nothing. |
| `DECISION_SIGNING_KEY` | Signs the hash-chained decision record; a key kept in the database could re-sign a tampered chain. |
| `ORIGIN_SECRET` | Origin lockdown behind Cloudflare; checked before any login exists. |
| `PLUGIN_TOKEN_<SERVICE>` | Broker ↔ plugin service authentication; both containers need it at start. |
| `PLUGIN_SECRETS_KEY_<SERVICE>`, `SIDECAR_TOKEN` | Held by the plugin containers only; the broker never receives them. |

**2. Exposure settings that fail closed at boot.** These decide how clients
reach the broker. They are env-only, so a hijacked console session cannot
switch them off.

| Key | Why |
|---|---|
| `CF_ACCESS_ENABLED`, `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, `CF_ACCESS_ALLOWED_EMAILS` | Cloudflare Access on the admin plane. In public mode the broker refuses to boot without it. |
| `ALLOW_INSECURE_ADMIN` | The escape hatch that weakens that boot interlock; only the host operator may set it. |
| `ORIGIN_SECRET_HEADER`, `TRUST_CF_CONNECTING_IP` | Paired with the origin secret; decide which client IP rate limits see. |
| `MCP_ALLOWED_HOSTS` | The **base** Host allowlist of `/mcp` (DNS-rebinding guard). The console can add hosts but never remove these. |
| `SITE_DOMAIN` | The public hostname (public overlay only). The edge serves it, and the broker builds the OAuth redirect URI `https://<SITE_DOMAIN>/oauth/callback/<service>` from it, never from a request's Host header; a connect without it is refused. Env-only so that a hijacked session cannot point a consent flow elsewhere. |

The files also hold deployment plumbing that the console never edits:

- `BROKER_DB` and `PLUGIN_URL_<SERVICE>`: where the broker's database and each
  plugin service live. `docker-compose.yml` sets them.
- `TZ`.
- Values that only Compose or other containers read: `BROKER_PORT`,
  `DEVICE_NAME` and `GITHUB_APP_KEY_DIR`. `GITHUB_APP_KEY_DIR` is an optional
  file-based alternative to pasting the GitHub App key.
- `LOG_LEVEL` / `LOG_FORMAT` (`docs/logging.md`). They are process-level.
  Every service reads them once at start, including the plugin containers,
  which have no console.

**3. The plugin installer (opt-in).** `aab-installer` installs external
plugins from their own repositories (`docs/plugin-packaging.md`). It holds
the Docker socket, so it has root access on the server. Everything that
bounds it is file-only. The console can ask it to inspect, install, upgrade
or remove. The console can never widen what the installer can do. Otherwise,
a hijacked console session is one click from root on the server.

| Key | Read by | Why it lives in a file |
|---|---|---|
| `INSTALLER_ENABLED` | `scripts/compose-files.sh` | Loads `docker-compose.installer.yml` at all. Off (`false`) by default: without it there is no installer container, no `net_installer`, and the console's + Add plugin only explains how to turn it on. |
| `INSTALLER_ALLOWED_SOURCES` | the installer | Comma-separated repositories it may clone, e.g. `github.com/you/*` (`*` is exactly one path segment). Empty refuses every inspect and install (fail closed). The one thing that decides whose code can be built on this host. |
| `INSTALLER_TOKEN` | the broker and the installer | Generated (`--rotate INSTALLER_TOKEN`); the `X-Installer-Token` the broker presents on `net_installer`, compared in constant time. An empty token refuses to boot. |
| `AAB_HOME` | compose and the installer | The checkout's absolute path on the host (`deploy/push.sh`'s `REMOTE_DIR`, default `/opt/aab`). The installer mounts it at the same path, so every relative path compose resolves inside it is the host's. |
| `INSTALLER_URL` | the broker | Set by the installer overlay (`http://aab-installer:8070`), never by hand. Empty means the installer is off: the install routes answer 503 saying so. |

The broker reads only `INSTALLER_URL` and `INSTALLER_TOKEN` (`Settings`). The
boot line names the token as set or unset. The installed state lives in three
places:

- `plugins.d/` holds the installed plugins. The installer owns it, and
  `deploy/push.sh` never syncs it.
- The `plugin_pins` table holds the owner's approval of each installed
  manifest.
- `.env` holds each installed service's `PLUGIN_TOKEN_<SERVICE>` /
  `PLUGIN_SECRETS_KEY_<SERVICE>`. The installer adds them through
  `scripts/init_secrets.py`, like every other generated secret.

The read-only GitHub token for private plugin repositories is **not** in this
table. It is a credential, not a bound, so it lives in the console (below). It
bounds nothing. The allowlist decides which repositories git can clone. The
token only lets git read the private ones among them. As a third-party
credential, it follows the rule of the Telegram bot token. The owner enters it
in the console, and the broker encrypts it under `BROKER_SECRETS_KEY`. It is
never in a file.

The console's Settings view lists every env-only key with its reason
(`GET /v1/admin/settings` → `env_only`). It shows secrets as set or unset,
never as values. A test fails if a new `Settings` field is neither
console-editable nor in that list.

## What lives in the console

| What | Where | Stored |
|---|---|---|
| Telegram bot token | Channels > Telegram | `plugin_secrets` (slot `broker`), Fernet under `BROKER_SECRETS_KEY`, through `crypto.py` only. Write-only: no route returns it. |
| Telegram link, enable/disable | Channels > Telegram | `app_config` (`telegram_*`) |
| GitHub App id and private key | Plugins > GitHub | Sent once to `plugin-github`'s `/configure`, encrypted in its own volume. The broker never stores them. |
| Google OAuth client id and secret | Plugins > Google | Sent once to `plugin-google`'s `/configure`, encrypted in its own volume. The broker never stores them. |
| Plugin enable/disable, non-secret plugin config | Plugins | `plugins` table |
| Install, upgrade, remove an external plugin; approve (pin) an offered manifest | Plugins > + Add plugin, a card's Upgrade / Remove, Offered, awaiting review | The pin in `plugin_pins` (audited `plugin.pin`); the install itself in `plugins.d/` and `.env`, done by the installer within the bounds above |
| GitHub token for private plugin repositories (optional, read-only) | Plugins > + Add plugin | `plugin_secrets` (slot `broker`, name `installer_git_token`), Fernet under `BROKER_SECRETS_KEY`, through `crypto.py` only. Write-only: no route returns it. Sent to the installer in the body of inspect, install and upgrade requests only; the installer keeps no copy. |
| Operator settings (below) | Settings | `app_config` as `setting:<name>` (JSON) |

### The Telegram token at runtime

- `POST /v1/admin/telegram/token {token}` stores it. The broker enforces the
  shape `<digits>:<token>`, so nothing else can reach the API URL.
  `DELETE /v1/admin/telegram/token` clears it. Either way, the poll loop
  starts or stops within a couple of seconds. A supervisor task in the app
  lifespan watches for the token, so **no restart is ever needed**.
- A token for a *different* bot drops the chat link and the update offset. A
  new bot is a new channel, and the owner must link it again. When the owner
  enters the token of the same bot again, the broker keeps both.
- If `BROKER_SECRETS_KEY` changes, the stored token no longer decrypts.
  Telegram switches off, and the console shows "re-enter required". If the key
  is *missing* while encrypted values exist, the broker does not start.
- Other routes:
  - `GET /v1/admin/telegram`: status and poll health, never the token.
  - `POST .../link/start`: a one-time code, 5 minutes, private chat only.
  - `.../enable`, `.../disable`, `.../test`, `.../unlink`.

### The installer's GitHub token at runtime

Private plugin repositories need a read-only GitHub token to clone. It is a
console setting, not a `.env` entry, because it is a third-party credential,
like the Telegram bot token. The rule for those credentials is simple. The
owner enters them in the console, and the broker keeps them encrypted. Also,
with the token out of the installer's environment, the container that is root
on the server holds no credential at all between requests.

- `POST /v1/admin/plugins/install/git-token {token}` stores it.
  `DELETE /v1/admin/plugins/install/git-token` clears it. The delete needs no
  key, so the owner can always remove an unreadable token. Both answer
  `{"git_token": "unset" | "set" | "unreadable", "secrets_key_configured": bool}`
  and never the token. The broker checks the shape loosely: 20 to 255
  printable ASCII characters with no whitespace. It drops the whitespace
  around a paste. Without `BROKER_SECRETS_KEY`, the broker rejects the token
  (409). The broker audits each change (`installer.git_token.set`,
  `installer.git_token.clear`) under the owner's name and logs it by name
  only.
- `GET /v1/admin/plugins/install/status` carries the same `git_token` state
  word and `secrets_key_configured`.
- When the broker has a token, it sends it as `git_token` in the body of every
  inspect, install and upgrade request to the installer, and nowhere else. The
  installer offers it to git through `GIT_ASKPASS`, for `github.com` sources
  on its allowlist only. It uses the token for the clone of that request, or
  for the single clone at the start of that job. It writes the token nowhere:
  not the job record, `install.json`, a job log line or a log line.
- If `BROKER_SECRETS_KEY` changes, the stored token no longer decrypts. The
  console shows "re-enter required". The broker rejects inspect, install and
  upgrade with 409 `git_token_unreadable` until the owner enters the token
  again or clears it. The rejection comes before the broker pins anything or
  asks the installer anything. Remove needs no clone and keeps working.
- Use a fine-grained token with these values:
  - Resource owner = the plugins' owner.
  - Repository access = only the plugin repositories.
  - Permissions = Contents: read-only.
- A classic token with `repo` reads every repository the account can read.

## Operator settings

`runtime_settings()` returns the effective values: the console's value, else
the `Settings` default (the env value when present). To edit, send
`PATCH /v1/admin/settings {"settings": {name: value}}`. `null` resets a
setting to its env default. The broker validates every field before it
writes any. It rejects out-of-range values with 400. It audits each change
(`settings.update`) with the changed names. When a stored value no longer
validates, the broker ignores it and uses the env default. Changes apply on
the next request, with no restart. The scheduler reads its tick again every
cycle. The one exception is `mcp_allowed_hosts_extra`, which takes effect at
the next broker start (see below).

| Setting | Default | Min | Max | Unit | Meaning |
|---|---|---|---|---|---|
| `max_delegation_depth` | 3 | 0 | 10 | hops | Delegation hops below a root key (0 = no delegation). |
| `max_live_delegations` | 25 | 1 | 200 | keys | Live child keys one key may have delegated at once; revoked or expired children do not count. |
| `session_idle_seconds` | 43200 | 300 | 604800 | seconds | Console sessions end after this much inactivity. |
| `session_absolute_seconds` | 604800 | 3600 | 2592000 | seconds | Console sessions end this long after login (new sessions). |
| `key_rotation_grace_seconds` | 86400 | 0 | 2592000 | seconds | A rotated key's previous secret keeps working this long. |
| `grant_max_hours` | 720 | 1 | 8760 | hours | Longest expiry a permission request may ask for. |
| `draft_ttl_hours` | 24 | 1 | 720 | hours | Undecided drafts expire after this long. |
| `scheduler_tick_seconds` | 15 | 1 | 300 | seconds | How often due queued actions are looked for. |
| `schedule_min_lead_seconds` | 30 | 0 | 3600 | seconds | A scheduled action must be at least this far ahead. |
| `schedule_max_horizon_days` | 30 | 1 | 365 | days | A scheduled action may be at most this far ahead. |
| `long_poll_max_wait_seconds` | 60 | 0 | 120 | seconds | Longest `?wait=` a long-poll read may hold. |
| `long_poll_interval_seconds` | 1.0 | 0.05 | 10 | seconds | How often a held long poll checks for news. |
| `plugin_timeout_seconds` | 30.0 | 1 | 300 | seconds | Plugin API calls give up after this long (a timeout after sending is an unknown outcome, never retried). |
| `ancestors_cache_seconds` | 60 | 0 | 3600 | seconds | How long folder-ancestry answers (subtree narrowing) are cached. |
| `mcp_allowed_hosts_extra` | `[]` | | 32 entries | host patterns | Extra `/mcp` Host headers (`host`, `host:port`, `host:*`). Takes effect at the next broker start. |

`mcp_allowed_hosts_extra` is additive by construction. The effective
allowlist is `MCP_ALLOWED_HOSTS` from the file, followed by the console's
extras. Thus the console can add a hostname, for example the public
`SITE_DOMAIN`. But it can never remove `localhost` or anything else the
operator put in the file.

It is also the one setting that does not apply on the next request. The
broker builds the MCP transport's DNS-rebinding guard once per app lifespan,
together with the MCP session manager (`mcp_server.run_session_manager`). The
guard reads the allowlist at that moment. The console stores and shows a
saved change at once. But until the broker restarts
(`docker compose restart broker`), `/mcp` keeps the old behaviour:

- It answers 421 for a newly added hostname.
- It keeps accepting a removed hostname.

The REST surface does not use this allowlist at all.
