# Deployment

How the Agent Authority Broker runs: one Docker Compose project, one container
per plugin service, and a strict split of which container receives which
secret. The step-by-step public runbook (EC2, Cloudflare) is
`deploy/DEPLOY.md`; the design rationale and diagrams are in
`docs/architecture.md` section 2. What lives in files versus the console, and
every console setting with its bounds, is `docs/configuration.md`.

> Status (0.2.0): all three plugin services are real: `plugin-whatsapp`
> (plugin `whatsapp`), `plugin-github` (plugin `github`) and `plugin-google`
> (plugins `gmail`, `gcal`, `gdrive`). The Telegram bot token and every
> plugin credential are entered in the console. The topology, networks,
> volumes and env split below are final. The images and the read-only
> `wa_data` mount have not yet been verified by a real build and run: do the
> checks in [Verify after `docker compose up`](#verify-after-docker-compose-up).

## Containers, networks, volumes

| Service | Image / build | Networks | Published port | Volumes |
|---|---|---|---|---|
| `edge` (public overlay only) | `caddy:2-alpine` | `edge_net` | `443` | `caddy_data`, `edge/Caddyfile` (ro), `edge/certs` (ro) |
| `broker` | `./broker` | `edge_net`, `broker_net` | `127.0.0.1:${BROKER_PORT:-8080}` in the base file only; none in public mode | `broker_data` |
| `plugin-whatsapp` | `./plugins/whatsapp` | `broker_net`, `wa_internal` | none | `wa_data` (ro), `whatsapp_secrets` |
| `whatsapp-sidecar` | `./sidecars/whatsapp` | `wa_internal` | none | `wa_data` (rw) |
| `plugin-github` | `./plugins/github` | `broker_net` | none | `github_secrets`, `${GITHUB_APP_KEY_DIR}` bind at `/run/secrets/github` (ro) |
| `plugin-google` | `./plugins/google` | `broker_net` | none | `google_secrets` |

- `edge_net` carries edge ↔ broker only, so a compromised edge cannot reach any
  plugin's `/perform`.
- `broker_net` carries broker ↔ plugin services. Plugins answer on `:8090`
  (`PLUGIN_URL_<SERVICE>`), authenticated by `X-Plugin-Token`.
- `wa_internal` carries plugin-whatsapp ↔ sidecar only. The broker is not on
  it and cannot reach the sidecar or its archive.
- No network is `internal: true`: the broker (Telegram, Cloudflare JWKS), the
  plugins (GitHub, Google) and the sidecar (WhatsApp) all need egress.
- Every image runs as the non-root user `aab` (uid 10001).
- Plugin build contexts are `./plugins/<service>` with the named context
  `runtime` (`./plugin-runtime`, the `aab-plugin-runtime` package). Each
  plugin package ships its own manifests; the broker keeps byte-identical
  vendored copies and pins every manifest a service offers against them.
- Each plugin image runs one uvicorn worker on `:8090` with a TCP
  healthcheck (every plugin API route needs the token, so the check only
  opens the port); the broker's healthcheck calls `/health`. The sidecar
  has no Docker healthcheck.

## Local run

```bash
python scripts/init_secrets.py          # writes .env (0600), prints a checklist, never a secret
docker compose up -d --build
# then browse to http://127.0.0.1:8080/admin: the setup page asks for SETUP_TOKEN (in .env)
```

Only the broker is published, on loopback. Origin lockdown is off
(`ORIGIN_SECRET` is forced empty in the base file) and Cloudflare Access is not
required. Third-party credentials (Telegram bot token, GitHub App, Google OAuth
client) are not in `.env` at all: you enter them in the console when you enable
the feature that needs them (`docs/configuration.md`).

The WhatsApp pairing QR is shown in the console (Plugins > WhatsApp >
Connect), printed in `docker compose logs whatsapp-sidecar`, and served as a
PNG at `/v1/admin/plugins/whatsapp/connect/qr.png` (admin only).

## Public run

```bash
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d --build
```

The overlay removes the broker's host port (`ports: !reset []`), sets
`ORIGIN_SECRET` on the broker (origin lockdown on, which also makes the boot
interlock demand Cloudflare Access), adds `SITE_DOMAIN` to `MCP_ALLOWED_HOSTS`,
and adds the Caddy `edge` on `edge_net` publishing `443`. Cloudflare must sit
in front with a Transform Rule injecting `X-AAB-Origin`, Authenticated Origin
Pulls, and an Access application on `/admin*`, `/auth*`, `/v1/admin*` and
`/oauth*`. Full procedure: `deploy/DEPLOY.md`.

OAuth redirect URIs to register at the providers:
`https://<SITE_DOMAIN>/oauth/callback/google` (Google OAuth client) and
`https://<SITE_DOMAIN>/oauth/callback/github` (GitHub App Setup URL). The
broker serves the callback page without an owner credential (the provider's
cross-site redirect carries no SameSite=Strict cookie; Access still applies),
and the page relays the one-time code through the admin-guarded
`connect/finish` to the plugin; the broker never sees client secrets or
refresh tokens. The code does appear once in the broker's access log line for
the callback (uvicorn logs the query string); it is single-use and expires
within minutes.

## Verify after `docker compose up`

Run these after the first `docker compose up -d --build` on a new host and
after any change to the images or compose files. In public mode add
`-f docker-compose.yml -f docker-compose.public.yml` to every compose command
and use `https://<SITE_DOMAIN>` instead of `http://127.0.0.1:8080`.

1. **Containers.** `docker compose ps`: every service is running, and
   `broker`, `plugin-whatsapp`, `plugin-github` and `plugin-google` turn
   `healthy` within a minute. The sidecar has no healthcheck.
   `docker compose logs broker` shows no boot refusal.
2. **Published ports.** `docker compose ps` shows a host port only for the
   broker (`127.0.0.1:8080->8080/tcp`), or in public mode only for `edge`
   (`443`). No plugin and no sidecar port.
3. **Health.** `curl -s http://127.0.0.1:8080/health` and `/v1/health`
   answer `{"status":"ok","version":"0.2.0"}`. They report liveness only,
   never plugin or connection state.
4. **Setup page.** `/admin` shows the setup page until the owner exists
   (`GET /auth/status` answers `"setup_completed": false`), then the login
   page.
5. **Plugin health cards.** After logging in, the Overview has one card per
   discovered plugin: `whatsapp`, `github`, `gmail`, `gcal` and `gdrive`,
   all disabled on first boot. A service that was
   down at boot is not listed yet: the broker retries discovery at most every
   30 seconds and logs that the service is not reachable yet. If one stays
   missing, the container is down or its token does not match the broker's
   (after rotating a `PLUGIN_TOKEN_<SERVICE>`, recreate both ends). A
   manifest that fails the pin against the vendored copy is refused and
   audited (`plugin.refused`). Enabling WhatsApp shows "waiting for QR
   pairing" until the phone scans the QR.
6. **The read-only `wa_data` mount.** After pairing, with the sidecar
   running:
   - `docker compose exec plugin-whatsapp ls -l /data` lists `messages.db`,
     `messages.db-wal` and `messages.db-shm`, and
     `docker compose exec plugin-whatsapp touch /data/probe` fails with
     "Read-only file system".
   - Archive reads keep working while the sidecar writes: call
     `list_chats` or `read_messages` with an agent key while messages
     arrive; each call answers 200 with current rows.
   - `docker compose stop whatsapp-sidecar`: once `messages.db-shm` is
     absent (the sidecar removes it when it closes the archive cleanly),
     archive reads answer 503 (not performed, safe to retry), never stale
     or partial rows. `docker compose start whatsapp-sidecar` and reads
     work again without restarting the plugin.
7. **Decision record.** After the first agent call, Overview's chain
   verification (or `GET /v1/admin/decisions/verify`, or
   `aab decisions verify`) reports the chain as intact.
8. **Public mode only:** the checks in `deploy/DEPLOY.md` step 9 (health
   through Cloudflare, the origin IP unreachable directly, the admin plane
   behind Access).

## The env split

`scripts/init_secrets.py` writes every value into one `.env`, but compose uses
no `env_file`: each service names exactly the variables it receives under
`environment:`. **No service receives the whole `.env`.** Env is keyed by
*service*, not plugin id: `plugin-google` hosts gmail, gcal and gdrive behind
one token and one key.

| Container | Receives | Must never receive |
|---|---|---|
| `broker` | `SETUP_TOKEN`, `BROKER_SECRETS_KEY`, `DECISION_SIGNING_KEY`, `ORIGIN_SECRET` (public overlay only; forced empty in the base file), `CF_ACCESS_ENABLED/TEAM_DOMAIN/AUD/ALLOWED_EMAILS`, `ALLOW_INSECURE_ADMIN`, `MCP_ALLOWED_HOSTS`, `SITE_DOMAIN` (public overlay only: builds the OAuth redirect URI `https://<SITE_DOMAIN>/oauth/callback/<service>`), `PLUGIN_URL_<SERVICE>` and `PLUGIN_TOKEN_<SERVICE>` for `WHATSAPP`, `GITHUB`, `GOOGLE`, `BROKER_DB`, `TZ` | `SIDECAR_TOKEN`, any `PLUGIN_SECRETS_KEY_<SERVICE>`, the `wa_data` volume |
| `plugin-whatsapp` | `PLUGIN_TOKEN_WHATSAPP`, `PLUGIN_SECRETS_KEY_WHATSAPP`, `SIDECAR_URL` (`http://whatsapp-sidecar:8081`), `SIDECAR_TOKEN`, `MESSAGES_DB` (`/data/messages.db`), `wa_data` (ro) | Other services' tokens/keys, broker secrets (`DECISION_SIGNING_KEY`, `BROKER_SECRETS_KEY`, `SETUP_TOKEN`, `ORIGIN_SECRET`) |
| `whatsapp-sidecar` | `SIDECAR_TOKEN`, `DEVICE_NAME`, `TZ`, `wa_data` (rw) | Everything else |
| `plugin-github` | `PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY`, `PLUGIN_SECRETS_DIR` (the runtime's generic names, fed from `PLUGIN_TOKEN_GITHUB` / `PLUGIN_SECRETS_KEY_GITHUB`); the App id, slug and private key are console config, not env (+ the optional read-only `/run/secrets/github` bind holding the PEM, a file alternative to pasting it) | Other services' tokens/keys, broker secrets, `SIDECAR_TOKEN` |
| `plugin-google` | `PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY`, `PLUGIN_SECRETS_DIR` (the runtime's generic names, fed from `PLUGIN_TOKEN_GOOGLE` / `PLUGIN_SECRETS_KEY_GOOGLE`); nothing Google-specific: the OAuth client id and secret are console config, and the broker passes the redirect URI with each connect | Other services' tokens/keys, broker secrets, `SIDECAR_TOKEN`, `SITE_DOMAIN` |
| `edge` | `SITE_DOMAIN`, `ORIGIN_SECRET`, origin certificate + key, Cloudflare origin-pull CA | Every other secret |

The table names the `.env` entries each container is fed from. Inside a
plugin container the names are generic: `aab_plugin_runtime.from_env` reads
`PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY` and `PLUGIN_SECRETS_DIR`, and compose
maps the service's own values onto them (`PLUGIN_TOKEN:
${PLUGIN_TOKEN_WHATSAPP}`, `PLUGIN_SECRETS_KEY:
${PLUGIN_SECRETS_KEY_WHATSAPP}`, `PLUGIN_SECRETS_DIR: /secrets`), so the
image does not depend on which service it runs as. All three plugin
services (`plugin-whatsapp`, `plugin-github`, `plugin-google`) are wired
this way.

Third-party credentials are not in the env split at all (`docs/configuration.md`):
the owner enters them in the console. The Telegram bot token is stored in
`broker.db`, encrypted under `BROKER_SECRETS_KEY`; the GitHub App id and key
and the Google OAuth client id and secret are entered in each plugin's config
form and relayed once to that plugin's `/configure`, never stored by the
broker.

`BROKER_PORT` and `GITHUB_APP_KEY_DIR` are read by compose itself (port
mapping, bind source) and are not passed into any container.

### GitHub App private key

Two ways, pick one:

1. **Console** (the default): paste the key into the GitHub plugin's config
   form (Plugins > GitHub). It is relayed once to `plugin-github` and stored
   only in `github_secrets`, encrypted under `PLUGIN_SECRETS_KEY_GITHUB`.
2. **File bind** (optional): put the PEM at `${GITHUB_APP_KEY_DIR}/app.pem` on
   the host (default `./data/github-app`, git-ignored and never synced by
   `deploy/push.sh`), readable by uid 10001 only
   (`sudo install -o 10001 -g 10001 -m 0400 app.pem data/github-app/`), and
   name `/run/secrets/github/app.pem` in the GitHub plugin's config form
   (the key-path field; the plugin reads only files inside
   `/run/secrets/github`, see `docs/plugins/github.md`). Only
   `plugin-github` mounts that directory, read-only.

## What each volume holds

| Volume | Mounted by | Holds | Encrypted by |
|---|---|---|---|
| `broker_data` | broker (`/gwdata`) | `broker.db`: owner account, sessions, admin-token/key hashes, grants, plugin enable flags and non-secret config, console settings, Telegram link state and bot token, hidden resources, action queue, decision record, capacity ledger, audit log | `BROKER_SECRETS_KEY` for the secrets entered in the console (the Telegram bot token); the rest is hashes or non-secret (the decision chain is HMAC-signed with `DECISION_SIGNING_KEY`) |
| `wa_data` | whatsapp-sidecar (rw), plugin-whatsapp (ro) | `session.db` (the WhatsApp account session, whatsmeow's own store), `messages.db` (the archive) | **nothing**: the one credential not encrypted at rest, mitigated by volume scoping and non-root containers |
| `whatsapp_secrets` | plugin-whatsapp (`/secrets`) | Nothing today: the WhatsApp manifest has no config, and the store exists because the runtime provides one | `PLUGIN_SECRETS_KEY_WHATSAPP` |
| `github_secrets` | plugin-github (`/secrets`) | The GitHub plugin's console config (App id and slug, the private key if pasted, the PAT fallback if used), the installation id, connect `state` nonces | `PLUGIN_SECRETS_KEY_GITHUB` |
| `google_secrets` | plugin-google (`/secrets`) | OAuth client id and secret, refresh token, granted scopes, connect `state` nonces with their redirect URI | `PLUGIN_SECRETS_KEY_GOOGLE` |
| `caddy_data` | edge | Caddy's runtime state | n/a |

Compose prefixes names with the project (`aab_broker_data`, ...). Back up all
of them together with `.env`; see `deploy/DEPLOY.md` > Operations > Backups.
`BROKER_SECRETS_KEY` protects the secrets entered in the console (today the
Telegram bot token); a `broker_data` backup restored without it means
re-entering them, and the broker refuses to boot while encrypted values exist
and the key is missing.

## Rotating secrets

Always `python scripts/init_secrets.py --rotate NAME` (add `--out <path>` if
`.env` is elsewhere). It rewrites exactly one line, never prints the value, and
tells you the follow-up. Then recreate the affected containers so they pick up
the new environment (`docker compose up -d <services>`; add
`-f docker-compose.public.yml` in public mode).

| Secret | Who holds it | After rotating |
|---|---|---|
| `SETUP_TOKEN` | broker | Nothing, unless the owner does not exist yet: then use the new value on the setup page. `docker compose up -d broker`. |
| `ORIGIN_SECRET` | broker, edge, Cloudflare Transform Rule | Update the Transform Rule's `X-AAB-Origin` value first, then `up -d broker edge` (public overlay). Requests in between get 403. |
| `BROKER_SECRETS_KEY` | broker | `up -d broker`. Secrets entered in the console no longer decrypt: Channels > Telegram shows "re-enter required" (Telegram stays off until then); paste the bot token again. |
| `DECISION_SIGNING_KEY` | broker | `up -d broker`. Existing decision rows no longer verify under the new key (`verify` reports the first old row as bad). Rotate only on suspected compromise. |
| `PLUGIN_TOKEN_WHATSAPP` / `_GITHUB` / `_GOOGLE` | broker and that plugin service | `up -d broker plugin-<service>`: both ends must restart together, calls fail with 503 in between. |
| `PLUGIN_SECRETS_KEY_WHATSAPP` | plugin-whatsapp | `up -d plugin-whatsapp`. Its store holds nothing today, so nothing needs re-entering. The WhatsApp session (in `wa_data`) is unaffected. |
| `PLUGIN_SECRETS_KEY_GITHUB` | plugin-github | `up -d plugin-github`, then clear the old store (below), re-enter the GitHub plugin's config in the console and connect again. |
| `PLUGIN_SECRETS_KEY_GOOGLE` | plugin-google | `up -d plugin-google`, then clear the old store (below), re-enter the Google client id and secret in the console and connect again. |
| `SIDECAR_TOKEN` | plugin-whatsapp, whatsapp-sidecar | `up -d plugin-whatsapp whatsapp-sidecar`. No re-pairing needed. |

Third-party values are rotated at their source, then re-entered where they
live (the console for credentials, `.env` for Cloudflare Access):

| Value | Where to rotate | Then |
|---|---|---|
| Telegram bot token | @BotFather `/revoke` | Paste the new token in the console (Channels > Telegram). The poll loop picks it up within seconds, no restart; a token for a different bot drops the link, so link again. |
| GitHub App private key | GitHub App > Private keys: generate new, delete old | Paste it in the GitHub plugin form, or replace the file in `GITHUB_APP_KEY_DIR` and `docker compose restart plugin-github` (a plain `up -d` does not recreate an unchanged container; the restart also drops tokens cached under the old key). |
| Google OAuth client secret | Google Cloud Console > Credentials > reset secret | Re-enter it in the Google plugin form. |
| `CF_ACCESS_AUD` / team domain | Cloudflare Zero Trust | `up -d broker`. |
| Origin certificate / AOP CA | Cloudflare SSL/TLS > Origin Server | Replace files in `edge/certs`, `up -d edge`. |

**Clearing a plugin's old store.** After its `PLUGIN_SECRETS_KEY_<SERVICE>`
changes, a plugin service reports "reconnect required" and fails closed: its
encrypted files no longer decrypt, and the store refuses every write while
they exist (the runtime reads a slot before it merges a change into it), so
the console can neither save config nor start a connect until the old files
are gone. Remove them from inside the container, which runs as the owner of
`/secrets`:

```bash
docker compose exec plugin-google sh -c 'rm -f /secrets/*.secrets'   # or plugin-github
```

Then re-enter the plugin's secret config fields in the console and connect
again. Nothing outside that service's volume is touched.

Losing (not rotating) a `PLUGIN_SECRETS_KEY_<SERVICE>` has the same effect as
rotating it: that plugin shows "reconnect required", its old store must be
cleared as above, and nothing else is lost.
