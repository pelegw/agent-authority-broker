# Deployment

How the Agent Authority Broker runs: one Docker Compose project, one container
per plugin service, and a strict split of which container receives which
secret. The step-by-step public runbook (EC2, Cloudflare) is
`deploy/DEPLOY.md`; the design rationale and diagrams are in
`docs/architecture.md` section 2.

> Status (0.2.0 in progress): `plugin-whatsapp`, `plugin-github` and
> `plugin-google` are placeholder images that only idle until phases 4, 6 and 7
> fill them in. The topology, networks, volumes and env split below are final.

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
- Plugin build contexts are `./plugins/<service>` with two named contexts:
  `runtime` (`./plugin-runtime`, the `aab-plugin-runtime` package) and `repo`
  (the repo root, for `VERSION` and the vendored manifests).

## Local run

```bash
python scripts/init_secrets.py          # writes .env (0600), prints a checklist, never a secret
docker compose up -d --build
# then browse to http://127.0.0.1:8080/admin: the setup page asks for SETUP_TOKEN (in .env)
```

Only the broker is published, on loopback. Origin lockdown is off
(`ORIGIN_SECRET` is forced empty in the base file) and Cloudflare Access is not
required. Third-party values in `.env` (Telegram, GitHub App, Google OAuth) can
stay blank until you enable the feature that needs them.

The WhatsApp pairing QR is printed in `docker compose logs whatsapp-sidecar`
and served as a PNG at `/v1/admin/plugins/whatsapp/connect/qr.png` (admin only).

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
broker renders the admin-guarded callback page and relays the one-time code to
the plugin; it never sees client secrets or refresh tokens.

## The env split

`scripts/init_secrets.py` writes every value into one `.env`, but compose uses
no `env_file`: each service names exactly the variables it receives under
`environment:`. **No service receives the whole `.env`.** Env is keyed by
*service*, not plugin id: `plugin-google` hosts gmail, gcal and gdrive behind
one token and one key.

| Container | Receives | Must never receive |
|---|---|---|
| `broker` | `SETUP_TOKEN`, `BROKER_SECRETS_KEY`, `DECISION_SIGNING_KEY`, `ORIGIN_SECRET` (public overlay only; forced empty in the base file), `CF_ACCESS_ENABLED/TEAM_DOMAIN/AUD/ALLOWED_EMAILS`, `ALLOW_INSECURE_ADMIN`, `MCP_ALLOWED_HOSTS`, `TELEGRAM_BOT_TOKEN`, `PLUGIN_URL_<SERVICE>` and `PLUGIN_TOKEN_<SERVICE>` for `WHATSAPP`, `GITHUB`, `GOOGLE`, `BROKER_DB`, `TZ` | `SIDECAR_TOKEN`, any `PLUGIN_SECRETS_KEY_<SERVICE>`, GitHub App id/private key, Google OAuth client id/secret, the `wa_data` volume |
| `plugin-whatsapp` | `PLUGIN_TOKEN_WHATSAPP`, `PLUGIN_SECRETS_KEY_WHATSAPP`, `SIDECAR_URL` (`http://whatsapp-sidecar:8081`), `SIDECAR_TOKEN`, `MESSAGES_DB` (`/data/messages.db`), `wa_data` (ro) | Other services' tokens/keys, broker secrets (`DECISION_SIGNING_KEY`, `BROKER_SECRETS_KEY`, `SETUP_TOKEN`, `ORIGIN_SECRET`), `TELEGRAM_BOT_TOKEN` |
| `whatsapp-sidecar` | `SIDECAR_TOKEN`, `DEVICE_NAME`, `TZ`, `wa_data` (rw) | Everything else |
| `plugin-github` | `PLUGIN_TOKEN_GITHUB`, `PLUGIN_SECRETS_KEY_GITHUB`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` (+ the `/run/secrets/github` bind holding the PEM) | Other services' tokens/keys, broker secrets, `SIDECAR_TOKEN`, `TELEGRAM_BOT_TOKEN` |
| `plugin-google` | `PLUGIN_TOKEN_GOOGLE`, `PLUGIN_SECRETS_KEY_GOOGLE`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `SITE_DOMAIN` (to build the redirect URI) | Other services' tokens/keys, broker secrets, `SIDECAR_TOKEN`, `TELEGRAM_BOT_TOKEN` |
| `edge` | `SITE_DOMAIN`, `ORIGIN_SECRET`, origin certificate + key, Cloudflare origin-pull CA | Every other secret |

`BROKER_PORT` and `GITHUB_APP_KEY_DIR` are read by compose itself (port
mapping, bind source) and are not passed into any container.

### GitHub App private key

Two ways, pick one:

1. **Console upload** when connecting GitHub: the key is relayed once to
   `plugin-github` and stored only in `github_secrets`, encrypted under
   `PLUGIN_SECRETS_KEY_GITHUB`.
2. **File bind**: put the PEM at `${GITHUB_APP_KEY_DIR}/app.pem` on the host
   (default `./data/github-app`, git-ignored and never synced by
   `deploy/push.sh`), readable by uid 10001 only
   (`sudo install -o 10001 -g 10001 -m 0400 app.pem data/github-app/`), and set
   `GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github/app.pem`. Only
   `plugin-github` mounts that directory, read-only.

## What each volume holds

| Volume | Mounted by | Holds | Encrypted by |
|---|---|---|---|
| `broker_data` | broker (`/gwdata`) | `broker.db`: owner account, sessions, admin-token/key hashes, grants, plugin enable flags and non-secret config, hidden resources, action queue, decision record, capacity ledger, audit log | nothing (hashes only; the decision chain is HMAC-signed with `DECISION_SIGNING_KEY`) |
| `wa_data` | whatsapp-sidecar (rw), plugin-whatsapp (ro) | `session.db` (the WhatsApp account session, whatsmeow's own store), `messages.db` (the archive) | **nothing**: the one credential not encrypted at rest, mitigated by volume scoping and non-root containers |
| `whatsapp_secrets` | plugin-whatsapp (`/secrets`) | plugin-whatsapp's own stored config/secrets | `PLUGIN_SECRETS_KEY_WHATSAPP` |
| `github_secrets` | plugin-github (`/secrets`) | GitHub App private key (if uploaded), installation id, connect `state` nonces | `PLUGIN_SECRETS_KEY_GITHUB` |
| `google_secrets` | plugin-google (`/secrets`) | OAuth client secret (if set from the console), refresh token, connect `state` nonces | `PLUGIN_SECRETS_KEY_GOOGLE` |
| `caddy_data` | edge | Caddy's runtime state | n/a |

Compose prefixes names with the project (`aab_broker_data`, ...). Back up all
of them together with `.env`; see `deploy/DEPLOY.md` > Operations > Backups.
The broker's `plugin_secrets` table is reserved and unused in 0.2.0, so
`BROKER_SECRETS_KEY` currently protects nothing.

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
| `BROKER_SECRETS_KEY` | broker | `up -d broker`. Nothing is encrypted under it in 0.2.0. |
| `DECISION_SIGNING_KEY` | broker | `up -d broker`. Existing decision rows no longer verify under the new key (`verify` reports the first old row as bad). Rotate only on suspected compromise. |
| `PLUGIN_TOKEN_WHATSAPP` / `_GITHUB` / `_GOOGLE` | broker and that plugin service | `up -d broker plugin-<service>`: both ends must restart together, calls fail with 503 in between. |
| `PLUGIN_SECRETS_KEY_WHATSAPP` | plugin-whatsapp | `up -d plugin-whatsapp`; its stored config no longer decrypts, so re-save the WhatsApp plugin settings in the console. The WhatsApp session (in `wa_data`) is unaffected. |
| `PLUGIN_SECRETS_KEY_GITHUB` | plugin-github | `up -d plugin-github`, then reconnect GitHub from the console (stored App key and installation no longer decrypt). |
| `PLUGIN_SECRETS_KEY_GOOGLE` | plugin-google | `up -d plugin-google`, then reconnect Google from the console (the refresh token no longer decrypts). |
| `SIDECAR_TOKEN` | plugin-whatsapp, whatsapp-sidecar | `up -d plugin-whatsapp whatsapp-sidecar`. No re-pairing needed. |

Third-party values are rotated at their source and edited in `.env` by hand:

| Value | Where to rotate | Then |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather `/revoke` | `up -d broker`; re-link Telegram from the console if the bot changed. |
| `GITHUB_APP_PRIVATE_KEY_PATH` file / App key | GitHub App > Private keys: generate new, delete old | Replace the file (or re-upload in the console), `up -d plugin-github`. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google Cloud Console > Credentials > reset secret | `up -d plugin-google` (and re-save it in the console if it was set there). |
| `CF_ACCESS_AUD` / team domain | Cloudflare Zero Trust | `up -d broker`. |
| Origin certificate / AOP CA | Cloudflare SSL/TLS > Origin Server | Replace files in `edge/certs`, `up -d edge`. |

Losing (not rotating) a `PLUGIN_SECRETS_KEY_<SERVICE>` has the same effect as
rotating it: that plugin shows "reconnect required"; nothing else is lost.
