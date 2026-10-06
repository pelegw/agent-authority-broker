# Owner authentication

This document describes how the owner of the broker proves who they are.
Agents never use any of this. They authenticate with `aab_` keys (phase 2),
and those keys carry no admin power.

## The pieces

| Piece | Where | What it is |
|---|---|---|
| Owner principal | `principals` table, `identity/principals.py` | The one human the broker acts for. Username + scrypt password hash. |
| Setup token | `SETUP_TOKEN` in `.env`, `identity/setup.py` | One-time authorization to create the owner. |
| Session | `aab_session` cookie + `sessions` table, `identity/sessions.py` | What the console uses after a password login. |
| Admin token | `aab_admin_...`, `admin_tokens` table, `identity/admin_tokens.py` | Bearer credential for the CLI, scripts and deploys. |
| Monitor token | `aab_monitor_...`, the same table with `scope = monitor` | Accepted by `/health` and `/v1/health` only (Bearer, or the Basic-auth password), for an uptime monitor. |
| Cloudflare Access | `Cf-Access-Jwt-Assertion` header, `cf_access.py` | The outer layer in public deployments. |
| Guard | `deps.require_admin` | Combines all of the above; returns an `AdminContext`. |

There is no static admin token in the environment. Every admin credential is a
session row or an admin-token row. Thus the owner can list and revoke each one
on its own.

## Setup (once)

1. `scripts/init_secrets.py` writes a random `SETUP_TOKEN` into `.env`.
2. The owner calls `POST /auth/setup {setup_token, username, password}`, from
   the console's setup page or with `aab setup --username <name>`. The CLI
   reads the token from `SETUP_TOKEN` or `--setup-token`, and the password
   with getpass.
3. The broker checks these conditions, in this order:
   - Setup is already done (`app_config.setup_completed = 1` **or** any
     principal exists) -> **409**. Either signal alone closes setup.
   - The configuration has no `SETUP_TOKEN` -> **403** `setup_disabled`.
   - The token is missing -> **401**. The token is wrong -> **403**. The
     compare is constant-time. Failures count toward the per-IP limiter, and
     the audit log records them without the value.
   - The username must match `^[a-z0-9_.-]{3,32}$`, and the password must
     have at least 12 characters. Else -> **400**.
4. The broker creates the owner (scrypt, n=2^15, r=8, p=1, 64-byte key,
   16-byte random salt). It sets `setup_completed = 1` and audits `auth.setup`
   with `actor_via = 'setup'`.

After that, the token is inert, even if it stays in `.env`. Remove it anyway.
v0.2 is single-owner: the code rejects any attempt to create a second
principal.

## Login and sessions

`POST /auth/login {username, password}` verifies the password and sets the
session cookie.

- **Timing**: verification always runs one scrypt, also for an unknown
  username (against a dummy salt). Thus the response time does not show which
  usernames exist. An unknown user and a wrong password return the same 401
  body.
- **Rate limit**: 5 failures per minute per client IP. That is the IP that
  `deps.client_ip` trusts: `CF-Connecting-IP` only behind a verified edge.
  Further attempts get **429** before the broker checks any password. Wrong
  setup tokens count toward the same limit. Wrong current passwords on a
  password change also count. The limiter is in-process (one worker) and
  resets on restart.
- **Audit**: failures are `auth.login_failed` (result `denied`). Successes are
  `auth.login`. The audit records a value from the username box only if it is
  a well-formed username. Thus a password typed there never gets into the log.
- **Cookie**: `aab_session`, 32 random bytes, `HttpOnly`, `SameSite=Strict`,
  `Path=/`, and `Secure` in public mode (`ORIGIN_SECRET` set). The database
  stores only the sha256 of the value. Session ids in the API are those
  hashes, and a client cannot replay a hash as a cookie.
- **Expiry**: a session ends at the earlier of two limits. The idle limit is
  `SESSION_IDLE_SECONDS` (default 12h), measured from `last_seen_at`. The
  absolute limit is `SESSION_ABSOLUTE_SECONDS` (default 7d from login). The
  broker writes `last_seen_at` at most once a minute.
- `POST /auth/logout` deletes the session row and clears the cookie.
- `GET /auth/status` -> `{setup_completed, login_required}`. It tells the
  console whether to show setup, login, or the app. `GET /auth/me` ->
  username, surface (`session` | `token`) and the expiry time of the
  credential.
- `POST /v1/admin/password {current_password, new_password}` requires the
  current password, also with a valid credential. Then it logs out every
  other session. With a token, it logs out all sessions.

## Admin tokens

`POST /v1/admin/tokens {name, expires_in_hours?}` mints `aab_admin_<48 hex>`.
The plaintext appears in that response only. The database keeps its sha256.
`GET /v1/admin/tokens` lists the name, the created, expires and last-used
times, and the revoked and expired flags. It never lists the token or its
hash. `POST /v1/admin/tokens/{id}/revoke` kills one token. The broker
refreshes `last_used_at` at most once a minute.

Every token has a `scope`. The broker sets the scope at creation, and the
token's prefix spells it. `admin` (the default) is the owner credential above.
`monitor` mints `aab_monitor_<48 hex>`. `/health` and `/v1/health` accept a
monitor token in two forms:

- As `Authorization: Bearer ...`.
- As the password of HTTP Basic auth. Thus a monitor that cannot set headers
  can still send it.

With a monitor token, the probe answers the full health summary (200 ok, 503
degraded; `docs/deployment.md`) instead of the anonymous liveness line. The
broker rejects a monitor token everywhere else, before any lookup. It also
rejects an admin token or a session on the probe. Thus the admin plane stays
behind Cloudflare Access, even though the probe is outside it. Failed monitor
attempts feed the login rate limiter. Databases from before the column get it
by migration, and every token already in them is `admin`.

The CLI uses these tokens: `AAB_ADMIN_TOKEN` (or `--token`) plus `AAB_URL` (or
`--url`). Mint the first one from the console after you log in.

The owner manages sessions the same way: `GET /v1/admin/sessions` and
`POST /v1/admin/sessions/{id}/revoke`.

## The guard: `require_admin`

Every admin route (all of `/v1/admin/*` and `/auth/me`) is on one of the
routers in `main.ADMIN_ROUTERS`. These are `routers/admin.py`,
`admin_plugins.py`, `admin_keys.py`, `admin_ops.py`, `admin_telegram.py` and
`admin_settings.py`. Each router has a router-level `require_admin`
dependency. A test asserts that every admin path the app serves is on one of
those routers. The guard does these steps:

1. When Cloudflare Access is on, the guard first requires a valid Access JWT
   (**403**, reason not echoed). It checks the JWT before any credential
   lookup, so a caller who bypassed Cloudflare causes no database work.
2. If an `Authorization` header is present, it must be `Bearer aab_admin_...`.
   It must name a live token: not revoked, not expired, and with an enabled
   principal. When a header is present, the guard never falls back to the
   cookie.
3. Otherwise, the `aab_session` cookie must name a live session of an enabled
   principal.
4. Any credential failure is **401** `{"error": "admin authentication required",
   "code": "unauthorized"}`, whatever the reason (missing, wrong, expired,
   revoked, disabled).

It returns `AdminContext(principal_id, username, via, credential_id,
expires_at)`. The broker records every human action under that username and
surface, never as "admin".

## CSRF

A cookie-authenticated request with a method other than GET/HEAD/OPTIONS must
carry `X-Requested-With: aab-console`. Else the answer is **403** `csrf`.
`SameSite=Strict` already stops browsers from sending the cookie cross-site.
The custom header is the second lock. A cross-site form cannot set it. A
cross-site `fetch` needs a CORS preflight, and the broker never grants one.
Bearer-token requests are exempt, because browsers never attach them
automatically. `POST /auth/logout` follows the same rule when it carries a
cookie.

## Cloudflare Access

In public mode the broker does not start unless Access is on (or
`ALLOW_INSECURE_ADMIN=true`). With Access on, the broker requires the Access
JWT in these places, in addition to the owner credential where one applies:

- The console page (`/admin`).
- `/auth/*`.
- Every admin route.
- The OAuth callback page (`/oauth/callback/{service}`).

Thus a stolen session cookie or admin token is useless without the SSO
identity too. Point the Access application at `/admin*`, `/auth*`,
`/v1/admin*` and `/oauth*` (`deploy/DEPLOY.md`). Without `/oauth*`,
Cloudflare adds no Access JWT to the callback page. Then the broker rejects
that page, and connecting Google or GitHub fails in public mode. The CLI sends
`CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` (an Access service token)
when they are present.

## The OAuth callback page

`GET /oauth/callback/{service}` (`routers/oauth.py`) is the page where Google
and GitHub send the owner back after consent or an App installation. The
broker serves it **without** an owner credential, and it has to. The owner
arrives by a cross-site redirect from google.com or github.com. The
`aab_session` cookie is `SameSite=Strict`, so the browser does not send it on
that navigation. An owner-guarded page would answer 401, and a connect can
never finish. Cloudflare Access still applies in public mode, as for the
console page and `/auth/*`.

The page is safe without a credential, because it holds nothing and can do
nothing by itself. The server fills in only the validated service name and a
CSP nonce, never anything from the URL. Its script strips `code`, `state` and
`installation_id` from the address bar and history. Then it POSTs them to
`/v1/admin/plugins/{service}/connect/finish`. That POST is same-origin, so the
browser **does** send the Strict cookie. The POST keeps the full guard:
`require_admin` and the CSRF header. Without a live session, the POST gets a
401. Then the page asks the owner to log in to the console in another tab and
try again. The page keeps the code in memory only. The plugin checks `state`
(single use, 10 minutes), so a code from another person cannot complete a
connection. The page is not in the OpenAPI schema. The broker sends it with
`Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
`frame-ancestors 'none'` and a nonce CSP. No log line carries the
authorization code. The broker's access line carries the path without the
query string, and uvicorn's own access log is off (`docs/logging.md`).

## Later additions

2FA (TOTP) and passkeys (WebAuthn) are out of scope for v0.2. Both attach to
the same `principals` row, through extra columns or a `principal_credentials`
table in `_MIGRATIONS`. `/auth/login` checks them after the password and
before it creates a session. Nothing else changes: sessions, admin tokens and
`require_admin` stay as they are.
