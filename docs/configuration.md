# Configuration: files vs the console

**Principle: files hold only what cannot live in the database.** The owner
configures everything else from the console, and the broker stores it in
`broker.db`. Nobody should have to hunt through `.env` for a knob, and a
hijacked console session must not be able to weaken what the host operator
decided.

## What lives in files, and why

`.env` is written by `python scripts/init_secrets.py` (never by hand) and is
reduced to two categories.

**1. Generated bootstrap secrets.** They must exist before the database is
readable, or before containers can authenticate each other.

| Key | Why it cannot be in the database |
|---|---|
| `SETUP_TOKEN` | Authorizes creating the owner account; it must exist before anyone can log in. Inert once setup completes. |
| `BROKER_SECRETS_KEY` | Decrypts the secrets entered in the console (the Telegram bot token); a key stored beside its ciphertext protects nothing. |
| `DECISION_SIGNING_KEY` | Signs the hash-chained decision record; a key kept in the database could re-sign a tampered chain. |
| `ORIGIN_SECRET` | Origin lockdown behind Cloudflare; checked before any login exists. |
| `PLUGIN_TOKEN_<SERVICE>` | Broker ↔ plugin service authentication; both containers need it at start. |
| `PLUGIN_SECRETS_KEY_<SERVICE>`, `SIDECAR_TOKEN` | Held by the plugin containers only; the broker never receives them. |

**2. Exposure settings that fail closed at boot.** These decide how the broker
is reachable. They are env-only so that a hijacked console session cannot
switch them off.

| Key | Why |
|---|---|
| `CF_ACCESS_ENABLED`, `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, `CF_ACCESS_ALLOWED_EMAILS` | Cloudflare Access on the admin plane. In public mode the broker refuses to boot without it. |
| `ALLOW_INSECURE_ADMIN` | The escape hatch that weakens that boot interlock; only the host operator may set it. |
| `ORIGIN_SECRET_HEADER`, `TRUST_CF_CONNECTING_IP` | Paired with the origin secret; decide which client IP rate limits see. |
| `MCP_ALLOWED_HOSTS` | The **base** Host allowlist of `/mcp` (DNS-rebinding guard). The console can add hosts but never remove these. |
| `SITE_DOMAIN` | The public hostname (public overlay only). The edge serves it, and the broker builds the OAuth redirect URI `https://<SITE_DOMAIN>/oauth/callback/<service>` from it, never from a request's Host header; a connect without it is refused. Env-only so that a hijacked session cannot point a consent flow elsewhere. |

Plus deployment plumbing that is never edited from the console:
`BROKER_DB` and `PLUGIN_URL_<SERVICE>` (where the broker's database and each
plugin service live; set in `docker-compose.yml`), `TZ`, and values that only
Compose or other containers read: `BROKER_PORT`, `DEVICE_NAME`,
`GITHUB_APP_KEY_DIR` (an optional file-based alternative to pasting the GitHub
App key). And `LOG_LEVEL` / `LOG_FORMAT` (`docs/logging.md`): process-level,
read once at start by every service, including the plugin containers, which
have no console.

The console's Settings view lists every env-only key with its reason
(`GET /v1/admin/settings` → `env_only`); secrets are shown as set/unset,
never as values. A test fails if a new `Settings` field is neither
console-editable nor listed there.

## What lives in the console

| What | Where | Stored |
|---|---|---|
| Telegram bot token | Channels > Telegram | `plugin_secrets` (slot `broker`), Fernet under `BROKER_SECRETS_KEY`, via `crypto.py` only. Write-only: no route returns it. |
| Telegram link, enable/disable | Channels > Telegram | `app_config` (`telegram_*`) |
| GitHub App id and private key | Plugins > GitHub | Relayed once to `plugin-github`'s `/configure`, encrypted in its own volume. Never stored by the broker. |
| Google OAuth client id and secret | Plugins > Google | Relayed once to `plugin-google`'s `/configure`, encrypted in its own volume. Never stored by the broker. |
| Plugin enable/disable, non-secret plugin config | Plugins | `plugins` table |
| Operator settings (below) | Settings | `app_config` as `setting:<name>` (JSON) |

### The Telegram token at runtime

- `POST /v1/admin/telegram/token {token}` stores it (the shape
  `<digits>:<token>` is enforced, so nothing else can reach the API URL);
  `DELETE /v1/admin/telegram/token` clears it. The poll loop starts or stops
  within a couple of seconds either way: a supervisor task in the app
  lifespan watches for the token, so **no restart is ever needed**.
- A token for a *different* bot drops the chat link and the update offset: a
  new bot is a new channel and the owner must link it again. Re-entering the
  same bot's token keeps both.
- If `BROKER_SECRETS_KEY` changes, the stored token no longer decrypts:
  Telegram switches off and the console shows "re-enter required". If the key
  is *missing* while encrypted values exist, the broker refuses to boot.
- Other routes: `GET /v1/admin/telegram` (status and poll health, never the
  token), `POST .../link/start` (one-time code, 5 minutes, private chat only),
  `.../enable`, `.../disable`, `.../test`, `.../unlink`.

## Operator settings

`runtime_settings()` returns the effective values: the `Settings` default
(env value when present) overlaid by the console's value. Editing is
`PATCH /v1/admin/settings {"settings": {name: value}}`; `null` resets a
setting to its env default. Every field is validated before any is written,
out-of-range values are refused with 400, and each change is audited
(`settings.update`) with the changed names. A stored value that no longer
validates is ignored in favour of the env default. Changes apply on the next
request (the scheduler re-reads its tick every cycle); no restart. The one
exception is `mcp_allowed_hosts_extra`, which takes effect at the next broker
start (see below).

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

`mcp_allowed_hosts_extra` is additive by construction: the effective allowlist
is `MCP_ALLOWED_HOSTS` from the file followed by the console's extras, so the
console can add a host (for example the public `SITE_DOMAIN`) but can never
remove `localhost` or anything else the operator put in the file.

It is also the one setting that does not apply on the next request. The MCP
transport's DNS-rebinding guard is built once per app lifespan, together with
the MCP session manager (`mcp_server.run_session_manager`), and reads the
allowlist at that moment. A saved change is stored and shown at once, but
`/mcp` keeps answering 421 for a newly added host, and keeps accepting a
removed one, until the broker restarts (`docker compose restart broker`).
The REST surface does not use this allowlist at all.
