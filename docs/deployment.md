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
> volumes and env split below are final. The images, a local run and the
> read-only `wa_data` mount were verified under Docker after the 0.2.0 tag;
> [Verify after `docker compose up`](#verify-after-docker-compose-up) has the
> recorded outputs. Pairing a real phone was not part of that run.

## Containers, networks, volumes

| Service | Image / build | Networks | Published port | Volumes |
|---|---|---|---|---|
| `edge` (public overlay only) | `caddy:2-alpine` | `edge_net` | `443` | `caddy_data`, `edge/Caddyfile` (ro), `edge/certs` (ro) |
| `broker` | `./broker` | `edge_net`, `net_whatsapp`, `net_github`, `net_google` | `127.0.0.1:${BROKER_PORT:-8080}` in the base file only; none in public mode | `broker_data` |
| `plugin-whatsapp` | `./plugins/whatsapp` | `net_whatsapp`, `wa_internal` | none | `wa_data` (ro), `whatsapp_secrets` |
| `whatsapp-sidecar` | `./sidecars/whatsapp` | `wa_internal` | none | `wa_data` (rw), `wa_session` (rw, this service only) |
| `plugin-github` | `./plugins/github` | `net_github` | none | `github_secrets`, `${GITHUB_APP_KEY_DIR}` bind at `/run/secrets/github` (ro) |
| `plugin-google` | `./plugins/google` | `net_google` | none | `google_secrets` |

- `edge_net` carries edge ↔ broker only, so a compromised edge cannot reach any
  plugin's `/perform`.
- `net_whatsapp`, `net_github` and `net_google` each carry broker ↔ one
  plugin service. Plugins answer on `:8090` (`PLUGIN_URL_<SERVICE>`),
  authenticated by `X-Plugin-Token`. Only the broker is on all three, so a
  plugin service cannot reach another one at all: `plugin-github` cannot
  resolve `plugin-google`, let alone open a connection to it.
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
and use `https://<SITE_DOMAIN>` instead of `http://127.0.0.1:8080`. The
expected outputs below are the ones recorded when 0.2.0 was verified
(Docker Engine 29.7.2, Compose v5.5.0, Docker Desktop on Windows with WSL2).

Admin calls without the console: log in with `POST /auth/login` and keep the
`aab_session` cookie (`curl -c jar` / `-b jar`). Every cookie-authenticated
write also needs `-H 'X-Requested-With: aab-console'`, otherwise it answers
`403 {"error":"missing x-requested-with: aab-console header","code":"csrf"}`.

On Windows, Git Bash rewrites a leading `/` in arguments, so
`docker compose exec whatsapp-sidecar ls /data` looks for
`C:/Program Files/Git/data`. Prefix such commands with `MSYS_NO_PATHCONV=1`,
or run them from PowerShell. If port 8080 is taken on the host (a WA_GW
gateway, for example), set `BROKER_PORT` in `.env`.

1. **Containers.** `docker compose ps`: every service is running, and
   `broker`, `plugin-whatsapp`, `plugin-github` and `plugin-google` show
   `Up ... (healthy)` within a minute. The sidecar has no healthcheck and
   shows plain `Up`. `docker compose exec <service> id` answers
   `uid=10001(aab) gid=10001(aab) groups=10001(aab)` in all five.
   `docker compose logs broker` shows no boot refusal. Each plugin's log
   shows `"GET /manifests HTTP/1.1" 200 OK` (the broker's discovery). The
   sidecar's log shows `internal API listening on :8081` and the pairing QR.
2. **Published ports.** `docker compose ps` shows a host port only for the
   broker (`127.0.0.1:8080->8080/tcp`), or in public mode only for `edge`
   (`443`). The plugins show `8090/tcp` with no `->`: that is the image's
   `EXPOSE`, not a published port. The sidecar shows none. Each plugin
   service is alone on its network with the broker:
   `docker compose exec plugin-github python -c "import socket;
   socket.create_connection(('plugin-google', 8090), timeout=3)"` fails
   with `socket.gaierror: [Errno -2] Name or service not known` (the same
   for every pair of plugin services, and for any plugin but
   `plugin-whatsapp` towards `whatsapp-sidecar:8081`), while the broker
   reaches all three (step 5 lists all five plugins).
3. **Health.** `curl -s http://127.0.0.1:8080/health` and `/v1/health`
   answer `200 {"status":"ok","version":"0.2.0"}`. They report liveness only,
   never plugin or connection state.
4. **Setup page.** `/admin` shows the setup page until the owner exists:
   `GET /auth/status` answers `{"setup_completed":false,"login_required":false}`.
   `POST /auth/setup` with `{setup_token, username, password}` answers
   `{"username":"...","setup_completed":true}`. A second setup answers
   `409 setup_completed`, and a wrong token answers
   `403 {"error":"invalid setup token","code":"forbidden"}`. Then the console
   shows the login page.
5. **Plugin health cards.** After logging in, the Overview has one card per
   discovered plugin: `whatsapp`, `github`, `gmail`, `gcal` and `gdrive`,
   all disabled on first boot. `GET /v1/admin/plugins` answers
   `{"items": [...], "refused": []}` with those five, each naming its
   `service`. A service that was down at boot is not listed yet: the broker
   logs `plugin service google not reachable yet: plugin service unreachable
   (ConnectError)` and retries discovery at most every 30 seconds (verified:
   the Google plugins were listed again about 30 seconds after
   `docker compose start plugin-google`, with no broker restart). If one
   stays missing, the container is down or its token does not match the
   broker's (after rotating a `PLUGIN_TOKEN_<SERVICE>`, recreate both ends).
   A manifest that fails the pin against the vendored copy is refused and
   audited (`plugin.refused`). A health refresh is
   `POST /v1/admin/plugins/<id>/health` (a `GET` answers 405). Without
   credentials:
   - **WhatsApp** enables (it has nothing to configure). Its `last_health`
     shows `"health": "waiting for QR pairing"`, `"archive": "present"` and
     `"connection": {"waiting_for_qr": true, ...}` until the phone scans the
     QR. `POST /v1/admin/plugins/whatsapp/connect/start` answers
     `{"kind":"qr"}`, and `GET .../connect/qr.png` answers a 512x512
     `image/png` with `cache-control: no-store`.
   - **GitHub** enables but stays unhealthy: `"health": "not configured: set
     app_id, app_slug and private_key_pem (or private_key_path), or a pat"`.
     `connect/start` answers `400 plugin: configure app_id, app_slug and
     private_key_pem (or a pat) first`.
   - **Gmail, Calendar, Drive** refuse to enable:
     `400 {"error":"required config missing: ['client_id']","code":"invalid_config"}`.
     Their health says `not configured: set the OAuth client id and secret`,
     and `POST /v1/admin/plugins/google/connect/start` answers
     `400 plugin: set the Google OAuth client id and secret first`.
6. **An agent key before pairing.** Create one (Agent keys, or
   `POST /v1/admin/keys` with `{"name": "...", "role": "read-only",
   "capabilities": [{"target": "whatsapp", "actions": ["list_chats"],
   "mode": "direct"}]}`). Until WhatsApp is paired the owner ceiling `P` is
   empty (it holds only plugins that are enabled **and** connected), so:
   - `GET /v1/me` lists `whatsapp` with `"capabilities": []`.
   - `POST /v1/targets/whatsapp/actions/list_chats` answers
     `503 {"error":"target is not connected","code":"not_connected"}`,
     never a 500.
   - MCP `initialize` answers with `serverInfo`
     `{"name":"agent-authority-broker","version":"0.2.0"}`, and `tools/list`
     holds only the 12 generic tools (`get_my_access`, `list_targets`,
     `resolve_resource`, `request_permission`, `get_permission_status`,
     `list_my_permissions`, `delegate`, `list_my_delegations`,
     `revoke_delegation`, `get_action_status`, `list_my_actions`,
     `cancel_action`). `whatsapp_list_chats` appears once the device is
     paired.
7. **The read-only `wa_data` mount.** The sidecar creates the archive when it
   starts, before pairing:
   - `docker compose exec plugin-whatsapp ls -l /data` lists only
     `messages.db`, `messages.db-wal` and `messages.db-shm`. whatsmeow's
     `session.db` (with its own `-wal` and `-shm`) is in the sidecar's
     `/session` (`docker compose exec whatsapp-sidecar ls -l /session`), a
     volume `plugin-whatsapp` does not mount:
     `docker compose exec plugin-whatsapp ls /session` fails with
     `ls: cannot access '/session': No such file or directory`.
     `docker compose exec plugin-whatsapp touch /data/probe` fails with
     `touch: cannot touch '/data/probe': Read-only file system`, and `mount`
     inside the container shows `/data` as `ext4 (ro,relatime)`.
   - After pairing, archive reads keep working while the sidecar writes:
     call `list_chats` or `read_messages` with an agent key while messages
     arrive. Each call answers 200 with current rows.
   - `docker compose stop whatsapp-sidecar`: the sidecar closes the archive,
     SQLite folds the WAL into `messages.db` and removes `messages.db-wal`
     and `messages.db-shm`, and archive reads answer
     `503 message archive temporarily unavailable` (not performed, safe to
     retry), never stale or partial rows. A health refresh then stores
     `{"healthy": false, "error": "sidecar unreachable (ConnectError)",
     "status": 503, "enforcement": "proxy"}` and keeps `connected` as it
     was. After `docker compose start whatsapp-sidecar`, reads work again
     without restarting the plugin.
   - After a crash instead (`docker compose kill whatsapp-sidecar`, or an
     OOM kill), `-wal` and `-shm` stay behind and reads keep answering 200
     with the last committed rows: SQLite rebuilds the WAL index in memory
     from the `-wal` file because it cannot write the `-shm`. Nothing is
     writing, so nothing is partial. The next sidecar start recovers the
     WAL.
8. **Decision record and skill doc.** After the first agent call, Overview's
   chain verification (or `GET /v1/admin/decisions/verify`, or
   `aab decisions verify`) answers
   `{"ok":true,"checked":<rows>,"first_bad_id":null,"signed":true}`.
   `GET /skill` answers 200 `text/markdown` with the request's base URL
   filled in, and `GET /v1/me/skill` answers the same guide filtered to the
   calling key.
9. **Public mode only:** the checks in `deploy/DEPLOY.md` step 9 (health
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
| `broker` | `SETUP_TOKEN`, `BROKER_SECRETS_KEY`, `DECISION_SIGNING_KEY`, `ORIGIN_SECRET` (public overlay only; forced empty in the base file), `CF_ACCESS_ENABLED/TEAM_DOMAIN/AUD/ALLOWED_EMAILS`, `ALLOW_INSECURE_ADMIN`, `MCP_ALLOWED_HOSTS`, `SITE_DOMAIN` (public overlay only: builds the OAuth redirect URI `https://<SITE_DOMAIN>/oauth/callback/<service>`), `PLUGIN_URL_<SERVICE>` and `PLUGIN_TOKEN_<SERVICE>` for `WHATSAPP`, `GITHUB`, `GOOGLE`, `BROKER_DB`, `TZ` | `SIDECAR_TOKEN`, any `PLUGIN_SECRETS_KEY_<SERVICE>`, the `wa_data` and `wa_session` volumes |
| `plugin-whatsapp` | `PLUGIN_TOKEN_WHATSAPP`, `PLUGIN_SECRETS_KEY_WHATSAPP`, `SIDECAR_URL` (`http://whatsapp-sidecar:8081`), `SIDECAR_TOKEN`, `MESSAGES_DB` (`/data/messages.db`), `wa_data` (ro) | Other services' tokens/keys, broker secrets (`DECISION_SIGNING_KEY`, `BROKER_SECRETS_KEY`, `SETUP_TOKEN`, `ORIGIN_SECRET`), the `wa_session` volume |
| `whatsapp-sidecar` | `SIDECAR_TOKEN`, `DEVICE_NAME`, `TZ`, `SESSION_DIR` (`/session`), `wa_data` (rw), `wa_session` (rw; the only container that mounts it) | Everything else |
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
| `wa_data` | whatsapp-sidecar (rw), plugin-whatsapp (ro) | `messages.db` (the archive) | nothing (the archive is message content, not a credential) |
| `wa_session` | whatsapp-sidecar (`/session`, rw), nobody else | `session.db` (the WhatsApp account session, whatsmeow's own store) | **nothing**: the one credential not encrypted at rest, mitigated by a volume only the sidecar mounts and non-root containers |
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
| `PLUGIN_SECRETS_KEY_WHATSAPP` | plugin-whatsapp | `up -d plugin-whatsapp`. Its store holds nothing today, so nothing needs re-entering. The WhatsApp session (in `wa_session`) is unaffected. |
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
