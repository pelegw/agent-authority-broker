# Deployment

This document tells how the Agent Authority Broker runs. It runs as one
Docker Compose project, with one container per plugin service. A strict split
controls which container receives which secret. The step-by-step public
runbook (EC2, Cloudflare) is `deploy/DEPLOY.md`. The design rationale and the
diagrams are in `docs/architecture.md` section 2. `docs/configuration.md`
tells what lives in files and what lives in the console. It also lists every
console setting with its bounds.

> Status (0.2.0): all three plugin services are real: `plugin-whatsapp`
> (plugin `whatsapp`), `plugin-github` (plugin `github`) and `plugin-google`
> (plugins `gmail`, `gcal`, `gdrive`). The owner enters the Telegram bot
> token and every plugin credential in the console. The topology, networks,
> volumes and env split below are final. After the 0.2.0 tag, a run under
> Docker verified the images, a local run and the read-only `wa_data` mount.
> [Verify after `docker compose up`](#verify-after-docker-compose-up) has the
> recorded outputs. That run did not pair a real phone.
>
> External plugins (0.3.0): [External plugins: the installer](#external-plugins-the-installer)
> describes the opt-in plugin installer, the calculated compose file set and
> the plugin base image. Their tests run without Docker. The tests use a fake
> Docker and local git repositories, and they read the compose files as YAML.
> Nobody has run the images, the socket-driven install or the acceptance test
> below under Docker yet.

## Containers, networks, volumes

| Service | Image / build | Networks | Published port | Volumes |
|---|---|---|---|---|
| `edge` (public overlay only) | `caddy:2-alpine` | `edge_net` | `443` | `caddy_data`, `edge/Caddyfile` (ro), `edge/certs` (ro) |
| `broker` | `./broker` | `edge_net`, `net_whatsapp`, `net_github`, `net_google`; `net_installer` with the installer; `net_<service>` per installed plugin | `127.0.0.1:${BROKER_PORT:-8080}` in the base file only; none in public mode | `broker_data` |
| `plugin-whatsapp` | `./plugins/whatsapp` | `net_whatsapp`, `wa_internal` | none | `wa_data` (ro), `whatsapp_secrets` |
| `whatsapp-sidecar` | `./sidecars/whatsapp` | `wa_internal` | none | `wa_data` (rw), `wa_session` (rw, this service only) |
| `plugin-github` | `./plugins/github` | `net_github` | none | `github_secrets`, `${GITHUB_APP_KEY_DIR}` bind at `/run/secrets/github` (ro) |
| `plugin-google` | `./plugins/google` | `net_google` | none | `google_secrets` |
| `aab-installer` (installer overlay only) | `./installer` | `net_installer` | none | `/var/run/docker.sock`, the checkout `${AAB_HOME}` at the same path |
| `plugin-<service>` (each installed external plugin) | `./plugins.d/<service>/src` (the plugin's own Dockerfile) | `net_<service>` | none | `<service>_secrets`, the `<service>_*` volumes its descriptor declares |

- `edge_net` carries edge ↔ broker only, so a compromised edge cannot reach any
  plugin's `/perform`.
- `net_whatsapp`, `net_github` and `net_google` each carry broker ↔ one
  plugin service. Plugins answer on `:8090` (`PLUGIN_URL_<SERVICE>`), and
  `X-Plugin-Token` authenticates each call. Only the broker is on all three.
  Thus a plugin service cannot reach another one at all. `plugin-github`
  cannot resolve `plugin-google`, so it cannot open a connection to it.
- `wa_internal` carries plugin-whatsapp ↔ sidecar only. The broker is not on
  it and cannot reach the sidecar or its archive.
- `net_installer` (installer overlay only) carries broker ↔ `aab-installer`
  only. No plugin (in-tree or installed) can reach the installer, and the
  edge cannot reach it. The installer is root on the machine.
- Each installed external plugin gets `net_<service>`, which it shares with
  the broker only, exactly like the in-tree plugins. The installer writes its
  overlay. The plugin's repository never supplies it.
- No network is `internal: true`: the broker (Telegram, Cloudflare JWKS), the
  plugins (GitHub, Google) and the sidecar (WhatsApp) all need egress.
- Every image runs as the non-root user `aab` (uid 10001).
- The build context of a plugin is `./plugins/<service>`, with the named
  context `runtime` (`./plugin-runtime`, the `aab-plugin-runtime` package).
  Each plugin package ships its own manifests. The broker keeps
  byte-identical vendored copies. It pins every manifest that a service
  offers against them.
- Each plugin image runs one uvicorn worker on `:8090`, with a TCP
  healthcheck. Every plugin API route needs the token, so the check only
  opens the port. The broker's healthcheck calls `/health`. The sidecar has
  no Docker healthcheck.

## Local run

```bash
python scripts/init_secrets.py          # writes .env (0600), prints a checklist, never a secret
docker compose up -d --build
# then browse to http://127.0.0.1:8080/admin: the setup page asks for SETUP_TOKEN (in .env)
```

Compose publishes only the broker, on loopback. Origin lockdown is off,
because the base file forces `ORIGIN_SECRET` to empty. Cloudflare Access is
not necessary. Third-party credentials (Telegram bot token, GitHub App,
Google OAuth client) are not in `.env` at all. You enter them in the console
when you enable the feature that needs them (`docs/configuration.md`).

The WhatsApp pairing QR is in three places:

- The console shows it (Plugins > WhatsApp > Connect).
- `docker compose logs whatsapp-sidecar` prints it.
- The broker serves it as a PNG at
  `/v1/admin/plugins/whatsapp/connect/qr.png` (admin only).

## Public run

```bash
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d --build
```

The overlay makes these changes:

- It removes the broker's published port (`ports: !reset []`).
- It sets `ORIGIN_SECRET` on the broker. This turns origin lockdown on, and
  the boot interlock then demands Cloudflare Access.
- It adds `SITE_DOMAIN` to `MCP_ALLOWED_HOSTS`.
- It adds the Caddy `edge` on `edge_net`, which publishes `443`.

Cloudflare must be in front, with these parts:

- A Transform Rule that injects `X-AAB-Origin`.
- Authenticated Origin Pulls.
- An Access application on `/admin*`, `/auth*`, `/v1/admin*` and `/oauth*`.

The full procedure is in `deploy/DEPLOY.md`.

## The compose file set

A script calculates which compose files make up a deployment. Nobody lists
them by hand.

```bash
scripts/compose-files.sh            # prints e.g. -f docker-compose.yml -f docker-compose.public.yml
C="docker compose $(scripts/compose-files.sh)"
$C up -d --build
$C ps
```

It prints these files, in this order:

1. `docker-compose.yml`.
2. `docker-compose.public.yml`, when `.env` sets `SITE_DOMAIN` (the public
   deploy).
3. `docker-compose.installer.yml`, when `INSTALLER_ENABLED=true`.
4. `plugins.d/<service>/compose.yml` for every installed external plugin. It
   takes only directories whose name is a valid service name.

`deploy/push.sh`, the installer and these docs all use it. Thus the machine,
a deploy and an install always agree on what the stack is. On a local run
with nothing set, it prints just `-f docker-compose.yml`. Then plain
`docker compose ...` is the same thing.

Register these OAuth redirect URIs at the providers:

- `https://<SITE_DOMAIN>/oauth/callback/google` (Google OAuth client).
- `https://<SITE_DOMAIN>/oauth/callback/github` (GitHub App Setup URL).

The broker serves the callback page without an owner credential, because the
provider's cross-site redirect carries no SameSite=Strict cookie. Access
still applies. The page sends the one-time code through the admin-guarded
`connect/finish` to the plugin. The broker never sees client secrets or
refresh tokens. The broker does not log the code: its access line carries
the path without the query string (`docs/logging.md`).

## Verify after `docker compose up`

Do these checks after the first `docker compose up -d --build` on a new
machine. Do them again after any change to the images or compose files. In
public mode, add `-f docker-compose.yml -f docker-compose.public.yml` to
every compose command. Also use `https://<SITE_DOMAIN>` instead of
`http://127.0.0.1:8080`. The expected outputs below come from the 0.2.0
verification run (Docker Engine 29.7.2, Compose v5.5.0, Docker Desktop on
Windows with WSL2).

To make admin calls without the console, log in with `POST /auth/login`.
Keep the `aab_session` cookie (`curl -c jar` / `-b jar`). Every
cookie-authenticated write also needs `-H 'X-Requested-With: aab-console'`.
Without it, the write gets
`403 {"error":"missing x-requested-with: aab-console header","code":"csrf"}`.

On Windows, Git Bash rewrites a leading `/` in arguments. Thus
`docker compose exec whatsapp-sidecar ls /data` looks for
`C:/Program Files/Git/data`. Put `MSYS_NO_PATHCONV=1` before such commands,
or run them from PowerShell. If a different program uses port 8080 on the
machine (a WA_GW gateway, for example), set `BROKER_PORT` in `.env`.

1. **Containers.** Run `docker compose ps`. Every service runs. `broker`,
   `plugin-whatsapp`, `plugin-github` and `plugin-google` show
   `Up ... (healthy)` within a minute. The sidecar has no healthcheck and
   shows plain `Up`. `docker compose exec <service> id` answers
   `uid=10001(aab) gid=10001(aab) groups=10001(aab)` in all five.
   `docker compose logs broker` shows no rejected boot. Each plugin's log
   shows `request method=GET path=/manifests status=200 ... actor=plugin:<service>`
   (the broker's discovery). The broker's log shows `plugin registry ready`
   with all five plugins. Every line carries a request id. `docs/logging.md`
   gives the format, the levels and how to follow one request across
   services. The sidecar's log shows `internal API listening on :8081` and
   the pairing QR.
2. **Published ports.** `docker compose ps` shows a published port only for
   the broker (`127.0.0.1:8080->8080/tcp`). In public mode, it shows one only
   for `edge` (`443`). The plugins show `8090/tcp` with no `->`. That is the
   image's `EXPOSE`, not a published port. The sidecar shows none. Each
   plugin service is alone on its network with the broker.
   `docker compose exec plugin-github python -c "import socket;
   socket.create_connection(('plugin-google', 8090), timeout=3)"` fails
   with `socket.gaierror: [Errno -2] Name or service not known`. The result
   is the same for every pair of plugin services. It is also the same for
   any plugin but `plugin-whatsapp` towards `whatsapp-sidecar:8081`. The
   broker reaches all three (step 5 lists all five plugins).
3. **Health.** `curl -s http://127.0.0.1:8080/health` and `/v1/health`
   answer `200 {"status":"ok","version":"0.3.0"}`. `HEAD` answers the status
   alone. An anonymous call gets liveness only, never plugin or connection
   state. A **monitor token** gets the full health summary from the same
   paths. To make one, use Account > Admin tokens with scope "monitor", or
   `aab tokens create --scope monitor`. Send it as
   `Authorization: Bearer aab_monitor_...`. Some monitors cannot set
   headers, such as UptimeRobot's free plan. They can send the token as the
   password of HTTP Basic auth (`curl -u uptimerobot:aab_monitor_...`). The
   summary does three things:
   - It checks the database.
   - It refreshes the `/status` of every enabled plugin live, in parallel.
     It stores the result as the plugin cards store it.
   - It reads the Telegram channel.

   Then it answers `200 {"status":"ok", ...}` or
   `503 {"status":"degraded", "failing":["plugins.whatsapp", ...], ...}`.
   `checks.database`, `checks.plugins.<id>` and `checks.telegram` each carry
   `ok`. These rules apply:
   - A disabled plugin or channel is never a failure.
   - An enabled plugin must be reachable, healthy and connected.
   - An enabled Telegram channel must have a readable token, a linked chat,
     a running poll loop and fewer than three poll errors in a row.

   The call takes one plugin timeout at most. A monitor token opens nothing
   else, and an admin token opens nothing here. The probe is outside
   Cloudflare Access, so the monitor needs no Access service token. Failed
   attempts feed the login rate limiter. The owner's own view of the same
   summary is `GET` or `HEAD` `/v1/admin/health` (session or admin token,
   behind Access). The broker writes none of these routes to the access log.
4. **Setup page.** `/admin` shows the setup page until the owner exists.
   `GET /auth/status` answers `{"setup_completed":false,"login_required":false}`.
   `POST /auth/setup` with `{setup_token, username, password}` answers
   `{"username":"...","setup_completed":true}`. A second setup answers
   `409 setup_completed`. A wrong token answers
   `403 {"error":"invalid setup token","code":"forbidden"}`. Then the console
   shows the login page.
5. **Plugin health cards.** After you log in, the Overview has one card per
   discovered plugin: `whatsapp`, `github`, `gmail`, `gcal` and `gdrive`.
   All start disabled on first boot. `GET /v1/admin/plugins` answers
   `{"items": [...], "refused": []}` with those five, each with its
   `service`. A service that was down at boot is not in the list yet. The
   broker logs `plugin service not reachable yet; will retry service=google status=503
   retry_seconds=30`. It retries discovery at most every 30 seconds. The
   verification run confirmed this: the Google plugins came back in the list
   about 30 seconds after `docker compose start plugin-google`, with no
   broker restart. If one stays missing, the container is down or its token
   does not match the broker's. After you rotate a `PLUGIN_TOKEN_<SERVICE>`,
   recreate both ends. The broker rejects and audits a manifest that fails
   the pin against the vendored copy (`plugin.refused`). A health refresh is
   `POST /v1/admin/plugins/<id>/health` (a `GET` answers 405). Without
   credentials, the plugins behave as follows:
   - **WhatsApp** enables, because it has nothing to configure. Until the
     phone scans the QR, its `last_health` shows
     `"health": "waiting for QR pairing"`, `"archive": "present"` and
     `"connection": {"waiting_for_qr": true, ...}`.
     `POST /v1/admin/plugins/whatsapp/connect/start` answers
     `{"kind":"qr"}`. `GET .../connect/qr.png` answers a 512x512
     `image/png` with `cache-control: no-store`.
   - **GitHub** enables but stays unhealthy: `"health": "not configured: set
     app_id, app_slug and private_key_pem (or private_key_path), or a pat"`.
     `connect/start` answers `400 plugin: configure app_id, app_slug and
     private_key_pem (or a pat) first`.
   - **Gmail, Calendar, Drive** do not enable:
     `400 {"error":"required config missing: ['client_id']","code":"invalid_config"}`.
     Their health says `not configured: set the OAuth client id and secret`.
     `POST /v1/admin/plugins/google/connect/start` answers
     `400 plugin: set the Google OAuth client id and secret first`.
6. **An agent key before pairing.** Create one (Agent keys, or
   `POST /v1/admin/keys` with `{"name": "...", "role": "read-only",
   "capabilities": [{"target": "whatsapp", "actions": ["list_chats"],
   "mode": "direct"}]}`). Until the phone pairs WhatsApp, the owner ceiling
   `P` is empty. `P` holds only the plugins that the owner enabled **and**
   connected. Thus:
   - `GET /v1/me` lists `whatsapp` with `"capabilities": []`.
   - `POST /v1/targets/whatsapp/actions/list_chats` answers
     `503 {"error":"target is not connected","code":"not_connected"}`,
     never a 500.
   - MCP `initialize` answers with `serverInfo`
     `{"name":"agent-authority-broker","version":"0.3.0"}`. `tools/list`
     holds only the 12 generic tools: `get_my_access`, `list_targets`,
     `resolve_resource`, `request_permission`, `get_permission_status`,
     `list_my_permissions`, `delegate`, `list_my_delegations`,
     `revoke_delegation`, `get_action_status`, `list_my_actions`,
     `cancel_action`. `whatsapp_list_chats` appears after the device pairs.
7. **The read-only `wa_data` mount.** The sidecar creates the archive when it
   starts, before pairing.
   - `docker compose exec plugin-whatsapp ls -l /data` lists only
     `messages.db`, `messages.db-wal` and `messages.db-shm`. whatsmeow's
     `session.db` (with its own `-wal` and `-shm`) is in the sidecar's
     `/session` (`docker compose exec whatsapp-sidecar ls -l /session`).
     `plugin-whatsapp` does not mount that volume.
     `docker compose exec plugin-whatsapp ls /session` fails with
     `ls: cannot access '/session': No such file or directory`.
     `docker compose exec plugin-whatsapp touch /data/probe` fails with
     `touch: cannot touch '/data/probe': Read-only file system`. `mount`
     inside the container shows `/data` as `ext4 (ro,relatime)`.
   - After pairing, archive reads continue to work while the sidecar
     writes. To check, call `list_chats` or `read_messages` with an agent
     key while messages arrive. Each call answers 200 with current rows.
   - Run `docker compose stop whatsapp-sidecar`. The sidecar closes the
     archive. SQLite folds the WAL into `messages.db` and removes
     `messages.db-wal` and `messages.db-shm`. Archive reads answer
     `503 message archive temporarily unavailable` (not done, safe to
     retry), never stale or partial rows. A health refresh then stores
     `{"healthy": false, "error": "sidecar unreachable (ConnectError)",
     "status": 503, "enforcement": "proxy"}`. It keeps `connected` as it
     was. After `docker compose start whatsapp-sidecar`, reads work again
     with no plugin restart.
   - After a crash (`docker compose kill whatsapp-sidecar`, or an OOM
     kill), `-wal` and `-shm` stay behind. Reads continue to answer 200
     with the last committed rows. SQLite rebuilds the WAL index in memory
     from the `-wal` file, because it cannot write the `-shm`. Nothing
     writes, so nothing is partial. The next sidecar start recovers the WAL.
8. **Decision record and skill doc.** After the first agent call, the chain
   verification on the Overview answers
   `{"ok":true,"checked":<rows>,"first_bad_id":null,"signed":true}`.
   `GET /v1/admin/decisions/verify` and `aab decisions verify` give the same
   answer. `GET /skill` answers 200 `text/markdown`, with the base URL of the
   request in it. `GET /v1/me/skill` answers the same guide, filtered to the
   calling key.
9. **Public mode only:** do the checks in `deploy/DEPLOY.md` step 9. They
   cover health through Cloudflare, no direct access to the origin IP, and
   the admin plane behind Access.

## The env split

`scripts/init_secrets.py` writes every value into one `.env`. But compose
uses no `env_file`. Each service names exactly the variables it receives,
under `environment:`. **No service receives the whole `.env`.** The env key
is the *service*, not the plugin id. `plugin-google` holds gmail, gcal and
gdrive behind one token and one key.

| Container | Receives | Must never receive |
|---|---|---|
| `broker` | `SETUP_TOKEN`, `BROKER_SECRETS_KEY`, `DECISION_SIGNING_KEY`, `ORIGIN_SECRET` (public overlay only; forced empty in the base file), `CF_ACCESS_ENABLED/TEAM_DOMAIN/AUD/ALLOWED_EMAILS`, `ALLOW_INSECURE_ADMIN`, `MCP_ALLOWED_HOSTS`, `SITE_DOMAIN` (public overlay only: builds the OAuth redirect URI `https://<SITE_DOMAIN>/oauth/callback/<service>`), `PLUGIN_URL_<SERVICE>` and `PLUGIN_TOKEN_<SERVICE>` for `WHATSAPP`, `GITHUB`, `GOOGLE` and each installed external plugin (from its rendered overlay), `INSTALLER_URL` and `INSTALLER_TOKEN` (installer overlay only), `BROKER_DB`, `TZ`, `LOG_LEVEL`, `LOG_FORMAT` | `SIDECAR_TOKEN`, any `PLUGIN_SECRETS_KEY_<SERVICE>`, `INSTALLER_ALLOWED_SOURCES`, the Docker socket, the `wa_data` and `wa_session` volumes |
| `plugin-whatsapp` | `PLUGIN_TOKEN_WHATSAPP`, `PLUGIN_SECRETS_KEY_WHATSAPP`, `SIDECAR_URL` (`http://whatsapp-sidecar:8081`), `SIDECAR_TOKEN`, `MESSAGES_DB` (`/data/messages.db`), `LOG_LEVEL`, `LOG_FORMAT`, `wa_data` (ro) | Other services' tokens/keys, broker secrets (`DECISION_SIGNING_KEY`, `BROKER_SECRETS_KEY`, `SETUP_TOKEN`, `ORIGIN_SECRET`), the `wa_session` volume |
| `whatsapp-sidecar` | `SIDECAR_TOKEN`, `DEVICE_NAME`, `TZ`, `LOG_LEVEL`, `SESSION_DIR` (`/session`), `wa_data` (rw), `wa_session` (rw; the only container that mounts it) | Everything else |
| `plugin-github` | `PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY`, `PLUGIN_SECRETS_DIR` (the runtime's generic names, fed from `PLUGIN_TOKEN_GITHUB` / `PLUGIN_SECRETS_KEY_GITHUB`), `LOG_LEVEL`, `LOG_FORMAT`; the App id, slug and private key are console config, not env (+ the optional read-only `/run/secrets/github` bind holding the PEM, a file alternative to pasting it) | Other services' tokens/keys, broker secrets, `SIDECAR_TOKEN` |
| `plugin-google` | `PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY`, `PLUGIN_SECRETS_DIR` (the runtime's generic names, fed from `PLUGIN_TOKEN_GOOGLE` / `PLUGIN_SECRETS_KEY_GOOGLE`), `LOG_LEVEL`, `LOG_FORMAT`; nothing Google-specific: the OAuth client id and secret are console config, and the broker passes the redirect URI with each connect | Other services' tokens/keys, broker secrets, `SIDECAR_TOKEN`, `SITE_DOMAIN` |
| `plugin-<service>` (each installed external plugin) | `PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY`, `PLUGIN_SECRETS_DIR` (the runtime's generic names, fed from `PLUGIN_TOKEN_<SERVICE>` / `PLUGIN_SECRETS_KEY_<SERVICE>`), the literal `environment` of its descriptor, its allowlisted `env_passthrough` (`TZ`, `LOG_LEVEL`, `LOG_FORMAT`), `<service>_secrets` at `/secrets` and the `<service>_*` volumes it declares | Other services' tokens/keys, broker and installer secrets, `SIDECAR_TOKEN`, any bind mount, any other volume or network (the installer renders its overlay; the repository supplies none) |
| `aab-installer` (installer overlay only) | `INSTALLER_TOKEN`, `INSTALLER_ALLOWED_SOURCES`, `AAB_HOME`, `LOG_LEVEL`, `LOG_FORMAT`; the Docker socket and the checkout at `AAB_HOME` (same path inside), so it can read `.env`: it is root on the host | Any other variable in its environment, a published port, any network but `net_installer` |
| `edge` | `SITE_DOMAIN`, `ORIGIN_SECRET`, origin certificate + key, Cloudflare origin-pull CA | Every other secret |

The table names the `.env` entries that feed each container. Inside a plugin
container, the names are generic. `aab_plugin_runtime.from_env` reads
`PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY` and `PLUGIN_SECRETS_DIR`. Compose maps
the service's own values onto them: `PLUGIN_TOKEN:
${PLUGIN_TOKEN_WHATSAPP}`, `PLUGIN_SECRETS_KEY:
${PLUGIN_SECRETS_KEY_WHATSAPP}`, `PLUGIN_SECRETS_DIR: /secrets`. Thus the
image does not depend on the service it runs as. All three plugin services
(`plugin-whatsapp`, `plugin-github`, `plugin-google`) use this wiring.

Third-party credentials are not in the env split at all
(`docs/configuration.md`). The owner enters them in the console:

- The broker stores the Telegram bot token in `broker.db`, encrypted under
  `BROKER_SECRETS_KEY`.
- The broker stores the installer's read-only GitHub token (for private
  plugin repositories) in the same way. The broker sends it to the installer
  per request, and the installer keeps none.
- The owner enters the GitHub App id and key, and the Google OAuth client id
  and secret, in each plugin's config form. The broker sends them one time
  to that plugin's `/configure` and never stores them.

Compose itself reads `BROKER_PORT` and `GITHUB_APP_KEY_DIR` (port mapping,
bind source). It passes them into no container.

### GitHub App private key

There are two ways. Pick one:

1. **Console** (the default): paste the key into the GitHub plugin's config
   form (Plugins > GitHub). The broker sends it one time to `plugin-github`.
   The plugin stores it only in `github_secrets`, encrypted under
   `PLUGIN_SECRETS_KEY_GITHUB`.
2. **File bind** (optional): do these steps.
   - Put the PEM at `${GITHUB_APP_KEY_DIR}/app.pem` on the machine. The
     default directory is `./data/github-app`. Git ignores it, and
     `deploy/push.sh` never syncs it.
   - Make the file readable by uid 10001 only
     (`sudo install -o 10001 -g 10001 -m 0400 app.pem data/github-app/`).
   - Write `/run/secrets/github/app.pem` in the key-path field of the GitHub
     plugin's config form. The plugin reads only files inside
     `/run/secrets/github` (see `docs/plugins/github.md`).

   Only `plugin-github` mounts that directory, read-only.

## What each volume holds

| Volume | Mounted by | Holds | Encrypted by |
|---|---|---|---|
| `broker_data` | broker (`/gwdata`) | `broker.db`: owner account, sessions, admin-token/key hashes, grants, plugin enable flags and non-secret config, console settings, Telegram link state and bot token, the installer's GitHub token, hidden resources, action queue, decision record, capacity ledger, audit log | `BROKER_SECRETS_KEY` for the secrets entered in the console (the Telegram bot token, the installer's GitHub token); the rest is hashes or non-secret (the decision chain is HMAC-signed with `DECISION_SIGNING_KEY`) |
| `wa_data` | whatsapp-sidecar (rw), plugin-whatsapp (ro) | `messages.db` (the archive) | nothing (the archive is message content, not a credential) |
| `wa_session` | whatsapp-sidecar (`/session`, rw), nobody else | `session.db` (the WhatsApp account session, whatsmeow's own store) | **nothing**: the one credential not encrypted at rest, mitigated by a volume only the sidecar mounts and non-root containers |
| `whatsapp_secrets` | plugin-whatsapp (`/secrets`) | Nothing today: the WhatsApp manifest has no config, and the store exists because the runtime provides one | `PLUGIN_SECRETS_KEY_WHATSAPP` |
| `github_secrets` | plugin-github (`/secrets`) | The GitHub plugin's console config (App id and slug, the private key if pasted, the PAT fallback if used), the installation id, connect `state` nonces | `PLUGIN_SECRETS_KEY_GITHUB` |
| `google_secrets` | plugin-google (`/secrets`) | OAuth client id and secret, refresh token, granted scopes, connect `state` nonces with their redirect URI | `PLUGIN_SECRETS_KEY_GOOGLE` |
| `<service>_secrets` (each installed plugin) | `plugin-<service>` (`/secrets`) | That plugin's own secret store (what its console config marks secret) | `PLUGIN_SECRETS_KEY_<SERVICE>` |
| `<service>_*` (declared by an installed plugin, e.g. `finance_data`) | `plugin-<service>`, at the path its descriptor names | That plugin's data (the finance database) | whatever the plugin does (`finance_data`: nothing, plain SQLite) |
| `caddy_data` | edge | Caddy's runtime state | n/a |

Compose puts the project name before each name (`aab_broker_data`, ...).
Back up all of them together with `.env` (see `deploy/DEPLOY.md` >
Operations > Backups). `BROKER_SECRETS_KEY` protects the secrets that the
owner enters in the console: the Telegram bot token and the installer's
GitHub token. If you restore a `broker_data` backup without that key, enter
those secrets again. The broker does not start while encrypted values exist
and the key is missing.

## Logs

Every service logs to stdout, one line per event. Docker rotates the logs:
the `x-logging` anchor in both compose files sets five 10 MB `json-file`
files per container. Each line carries the request id of its request,
across broker, plugin and sidecar. The broker's decision rows for that
request carry the same id. `LOG_LEVEL` and `LOG_FORMAT` (`text` or `json`)
in `.env` set the level and the format. Restart to apply a change. To follow
one service, use `docker compose logs -f --since 10m broker`. To follow one
request, use `docker compose logs --no-log-prefix | grep <request-id>`.
[logging.md](logging.md) gives the format, the never-logged list, the
redaction backstop and how to ship logs to a collector.

## Rotating secrets

Always use `python scripts/init_secrets.py --rotate NAME`. Add
`--out <path>` if `.env` is in a different place. The script rewrites
exactly one line and never prints the value. It tells you the follow-up.
Then recreate the affected containers, so they get the new environment
(`docker compose up -d <services>`; add `-f docker-compose.public.yml` in
public mode).

| Secret | Who holds it | After rotating |
|---|---|---|
| `SETUP_TOKEN` | broker | Nothing, unless the owner does not exist yet: then use the new value on the setup page. `docker compose up -d broker`. |
| `ORIGIN_SECRET` | broker, edge, Cloudflare Transform Rule | Update the Transform Rule's `X-AAB-Origin` value first, then `up -d broker edge` (public overlay). Requests in between get 403. |
| `BROKER_SECRETS_KEY` | broker | `up -d broker`. Secrets entered in the console no longer decrypt: Channels > Telegram shows "re-enter required" (Telegram stays off until then); paste the bot token again. The installer's GitHub token, if set, shows "re-enter required" in Plugins > + Add plugin (inspect, install and upgrade answer 409 until then); paste it again. |
| `DECISION_SIGNING_KEY` | broker | `up -d broker`. Existing decision rows no longer verify under the new key (`verify` reports the first old row as bad). Rotate only on suspected compromise. |
| `PLUGIN_TOKEN_WHATSAPP` / `_GITHUB` / `_GOOGLE`, and `PLUGIN_TOKEN_<SERVICE>` of an installed plugin | broker and that plugin service | `up -d broker plugin-<service>`: both ends must restart together, calls fail with 503 in between. |
| `PLUGIN_SECRETS_KEY_WHATSAPP` | plugin-whatsapp | `up -d plugin-whatsapp`. Its store holds nothing today, so nothing needs re-entering. The WhatsApp session (in `wa_session`) is unaffected. |
| `PLUGIN_SECRETS_KEY_GITHUB` | plugin-github | `up -d plugin-github`, then clear the old store (below), re-enter the GitHub plugin's config in the console and connect again. |
| `PLUGIN_SECRETS_KEY_GOOGLE` | plugin-google | `up -d plugin-google`, then clear the old store (below), re-enter the Google client id and secret in the console and connect again. |
| `SIDECAR_TOKEN` | plugin-whatsapp, whatsapp-sidecar | `up -d plugin-whatsapp whatsapp-sidecar`. No re-pairing needed. |
| `PLUGIN_SECRETS_KEY_<SERVICE>` of an installed plugin | that plugin | `up -d plugin-<service>`, then clear its old store (below) and re-enter its secret settings. |
| `INSTALLER_TOKEN` | broker, aab-installer | `$C up -d broker aab-installer` (with `C` from [The compose file set](#the-compose-file-set)). A job in flight is lost: the installer marks it failed on restart. |

Rotate third-party values at their source. Then enter them again where they
live: the console for credentials, `.env` for Cloudflare Access.

| Value | Where to rotate | Then |
|---|---|---|
| Telegram bot token | @BotFather `/revoke` | Paste the new token in the console (Channels > Telegram). The poll loop picks it up within seconds, no restart; a token for a different bot drops the link, so link again. |
| GitHub App private key | GitHub App > Private keys: generate new, delete old | Paste it in the GitHub plugin form, or replace the file in `GITHUB_APP_KEY_DIR` and `docker compose restart plugin-github` (a plain `up -d` does not recreate an unchanged container; the restart also drops tokens cached under the old key). |
| Google OAuth client secret | Google Cloud Console > Credentials > reset secret | Re-enter it in the Google plugin form. |
| `CF_ACCESS_AUD` / team domain | Cloudflare Zero Trust | `up -d broker`. |
| Origin certificate / AOP CA | Cloudflare SSL/TLS > Origin Server | Replace files in `edge/certs`, `up -d edge`. |
| The installer's GitHub token (private plugin repositories) | GitHub > Settings > Developer settings > tokens: regenerate, or create a new one and delete the old | Paste it in the console (Plugins > + Add plugin, Replace). Used from the next inspect, install or upgrade; no restart. |

**Clearing a plugin's old store.** After its `PLUGIN_SECRETS_KEY_<SERVICE>`
changes, a plugin service reports "reconnect required" and fails closed. Its
encrypted files no longer decrypt. The store rejects every write while they
exist, because the runtime reads a slot before it merges a change into it.
Thus the console cannot save config or start a connect until the old files
are gone. Remove them from inside the container, which runs as the owner of
`/secrets`:

```bash
docker compose exec plugin-google sh -c 'rm -f /secrets/*.secrets'   # or plugin-github
```

Then enter the plugin's secret config fields in the console again, and
reconnect. This step touches nothing outside the volume of that service.

If you lose a `PLUGIN_SECRETS_KEY_<SERVICE>` (not rotate it), the effect is
the same as a rotation. That plugin shows "reconnect required". Clear its old
store as above. You lose nothing else.

## External plugins: the installer

`aab-installer` (`installer/`, `docker-compose.installer.yml`) installs
plugins that live in their own repositories (`docs/plugin-packaging.md`).
It is opt-in. It holds the Docker socket, so it is **root on the machine**
by implication. Structure limits it:

- Only the broker reaches it (`net_installer`).
- Each call needs `INSTALLER_TOKEN`, compared in constant time. With an
  empty token, the installer does not start.
- It installs only sources in `INSTALLER_ALLOWED_SOURCES` (env-only). An
  empty list rejects everything.
- It accepts only a release tag or a full commit.
- It installs only the commit that the owner reviewed.
- It makes each plugin's overlay from the plugin's descriptor through a
  fixed template.
- It never runs anything from a plugin repository on the machine. The
  plugin's Dockerfile runs inside `docker build`, like any image.

### Enabling it

1. Set these values in `.env`:
   - `INSTALLER_ENABLED=true`.
   - `INSTALLER_ALLOWED_SOURCES=github.com/<you>/*`. The list is
     comma-separated, and `*` is one path segment.
   - `AAB_HOME`: the absolute path of the checkout, as the Docker daemon
     sees it (default `/opt/aab`). The installer mounts it at the same path.
     Thus compose resolves every relative path exactly as on the machine.

   `scripts/init_secrets.py` generates `INSTALLER_TOKEN`.
   `--rotate INSTALLER_TOKEN` appends it to an older `.env`.
   `deploy/push.sh` does that, and also appends `AAB_HOME`.
2. Get the plugin base image. Run `docker login ghcr.io` with a
   `read:packages` token. Then run
   `docker pull ghcr.io/pelegw/aab-plugin-base:<version>` for each base
   version that your plugins name. The installer drives the Docker daemon of
   the machine, but it holds no registry credentials of its own. Thus its
   builds use the image from the daemon's store.
3. Run `docker compose $(scripts/compose-files.sh) up -d --build` (or
   `deploy/push.sh`). `aab-installer` starts. Compose recreates the broker
   on `net_installer`, with `INSTALLER_URL` and `INSTALLER_TOKEN`. The
   console's + Add plugin now inspects. Before, it told how to turn the
   installer on.
4. Only for private plugin repositories: in the console, open Plugins, + Add
   plugin. Paste a read-only GitHub token into "GitHub token for private
   plugin repositories" and choose Set. Use a fine-grained token for only the
   plugin repositories, with Contents: read-only. A classic token with
   `repo` also works. The token is not a `.env` line. The broker stores it
   encrypted under `BROKER_SECRETS_KEY`. The broker sends it to the
   installer with each inspect, install and upgrade request. The installer
   keeps no copy (`docs/configuration.md`). Nothing reads an
   `INSTALLER_GIT_TOKEN=` line in an older `.env`. Delete that line.

To turn the installer off again, set `INSTALLER_ENABLED=false`, then run
`up -d`. This stops install, upgrade and remove. Installed plugins continue
to run, because their overlays stay in the file set.

### Where installed plugins live

```
plugins.d/<service>/src/          the plugin repository at the reviewed commit
plugins.d/<service>/compose.yml   the overlay rendered from its descriptor
plugins.d/<service>/install.json  source, ref, commit, plugins, volumes, when
plugins.d/_installer/             job state and log lines; temporary clones
```

`plugins.d/` belongs to the installer:

- Git ignores it.
- `deploy/push.sh` never syncs or deletes it. rsync excludes it, and the git
  archive never contains it.
- Nothing else writes there.

Each installed service adds `PLUGIN_TOKEN_<SERVICE>` and
`PLUGIN_SECRETS_KEY_<SERVICE>` to `.env`. `scripts/init_secrets.py`
generates them, like every other secret. The owner's approval of each
installed manifest is the pin in `broker.db` (`plugin_pins`).

### Removal and purge

Remove (the Remove button on the plugin card) does these steps:

- It stops and deletes `plugin-<service>`.
- It deletes `plugins.d/<service>/`.
- It recreates the broker without the service.
- It removes the network of the service.
- It unpins every plugin in the service. Agents get 404 at once. The plugin
  rows stay, disabled.

Without purge, the volumes of the service stay. Its two `.env` secrets
become comments (`#aab-retired# PLUGIN_TOKEN_<SERVICE>=...`). If you install
the same service again, the installer restores those secrets, so the kept
volumes still decrypt. With or without purge, plugin rows and decision rows
that name its plugins stay in `broker.db`.

**WARNING: Back up the volumes and the two secrets before a purge.** With
purge, the installer deletes the `<service>_*` volumes
(`docker volume rm aab_<service>_...`) and both secrets for good.

### Backups

Add these items to the backup:

- The volumes of every installed plugin: `aab_<service>_secrets` (it needs
  its `PLUGIN_SECRETS_KEY_<SERVICE>`) and the volumes that its descriptor
  declares.
- `plugins.d/`, without `_installer/`.

`deploy/DEPLOY.md` > Operations > Backups backs up every `aab_` volume.
Restore volumes, `plugins.d/` and `.env` together with the `broker_data`
from the same backup. That `broker_data` holds the pins.

### Acceptance test

This end-to-end check shows that the installer works on a machine. Run it
locally under Docker, then on the server from the branch's deploy. The
fixture is the echo plugin, packaged as an external repository. It has the
shape that `installer/tests/conftest.py` builds: `aab-plugin.yaml` with
service `echo`, plugin `echo`, the manifest, and a Dockerfile `FROM` the
base image. Push it to a repository under `INSTALLER_ALLOWED_SOURCES` and
tag it `v0.1.0`.

1. Build the images. Run `docker build --build-context runtime=plugin-runtime
   -t ghcr.io/pelegw/aab-plugin-base:<version> plugins/base`, or pull the
   published one. Then run `$C build aab-installer` with the installer
   enabled.
2. `docker compose -f docker-compose.yml -f docker-compose.installer.yml config -q`
   with a throwaway `.env` (`INSTALLER_ENABLED=true`) answers nothing.
3. Run `$C up -d`. `$C ps` shows `aab-installer` healthy, with no published
   port. `docker compose exec broker python -c "import urllib.request;
   print(urllib.request.urlopen('http://aab-installer:8070/health').read())"`
   answers `{"ok":true}`. The same command from a plugin container cannot
   resolve `aab-installer`.
4. In the console, open Plugins, + Add plugin. Enter the repository and
   `v0.1.0`, then choose Inspect. The review lists echo's actions with their
   side effects and modes. It also lists the secret setting `api_secret`,
   and the volumes: `echo_secrets` and the declared ones. Choose Install. The
   job panel shows the clone, the added `.env` entries, the overlay that the
   installer wrote, the build, `up -d broker`, the "being recreated" line,
   then `done`.
5. The echo card appears disabled, with "installed from <source>@v0.1.0".
   Enable it. `plugins.d/echo/compose.yml` has one network, no ports, no
   binds and the two volumes. `.env` has `PLUGIN_TOKEN_ECHO` and
   `PLUGIN_SECRETS_KEY_ECHO`. The audit log has `plugin.pin`, then
   `plugin.install`, under the owner.
6. Create an agent key with an echo capability. `GET /v1/me/skill` with that
   key lists Echo. A read action answers 200.
7. Remove the plugin with no purge. Then check these results:
   - The job completes.
   - The card is gone.
   - The agent's call answers 404.
   - The `aab_echo_*` volumes remain.
   - `.env` holds the retired lines.
   - `$C config --services` no longer lists `plugin-echo`.

   Install again. The installer restores the same secrets. Then Remove with
   purge: the volumes and the secrets are gone. The console is back to its
   previous state.
8. A private repository: make the fixture repository private. Inspect now
   fails with `clone_failed`. In + Add plugin, set a fine-grained read-only
   token for it in "GitHub token for private plugin repositories". The badge
   changes to set. Inspect and Install now succeed. The token is in no job
   log (`plugins.d/_installer/jobs/`), no `install.json` and no `.env`. It
   is not in `$C logs broker` or in `$C logs aab-installer`. The audit log
   has `installer.git_token.set` under the owner. Clear the token: Inspect
   fails again.
