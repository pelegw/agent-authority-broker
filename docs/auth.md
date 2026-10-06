# Owner authentication

How the human who owns the broker proves who they are. Agents never use any of
this: they authenticate with `aab_` keys (phase 2), which carry no admin power.

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
session row or an admin-token row, so each one can be listed and revoked on its
own.

## Setup (once)

1. `scripts/init_secrets.py` writes a random `SETUP_TOKEN` into `.env`.
2. The owner calls `POST /auth/setup {setup_token, username, password}` (the
   console's setup page, or `aab setup --username <name>`, which reads the
   token from `SETUP_TOKEN` or `--setup-token` and the password with getpass).
3. The broker checks, in this order:
   - setup already done (`app_config.setup_completed = 1` **or** any principal
     exists) -> **409**. Either signal alone closes setup.
   - no `SETUP_TOKEN` configured -> **403** `setup_disabled`.
   - token missing -> **401**; wrong -> **403** (constant-time compare; failures
     count toward the per-IP limiter and are audited without the value).
   - username `^[a-z0-9_.-]{3,32}$`, password at least 12 characters -> else **400**.
4. It creates the owner (scrypt, n=2^15, r=8, p=1, 64-byte key, 16-byte random
   salt), sets `setup_completed = 1`, and audits `auth.setup` with
   `actor_via = 'setup'`.

After that the token is inert, even if it stays in `.env`. Remove it anyway.
v0.2 is single-owner: the code refuses to create a second principal.

## Login and sessions

`POST /auth/login {username, password}` verifies the password and sets the
session cookie.

- **Timing**: verification always runs one scrypt, even for an unknown username
  (against a dummy salt), so response time does not reveal which usernames exist.
  Unknown user and wrong password return the same 401 body.
- **Rate limit**: 5 failures per minute per client IP (the IP `deps.client_ip`
  trusts: `CF-Connecting-IP` only behind a verified edge). Further attempts get
  **429** before any password is checked. Wrong setup tokens and wrong
  current-passwords on password change count toward the same limit. The
  limiter is in-process (one worker) and resets on restart.
- **Audit**: failures are `auth.login_failed` (result `denied`); successes
  `auth.login`. A value typed into the username box is only recorded if it is
  a well-formed username, so a password typed there never lands in the log.
- **Cookie**: `aab_session`, 32 random bytes, `HttpOnly`, `SameSite=Strict`,
  `Path=/`, and `Secure` in public mode (`ORIGIN_SECRET` set). The database
  stores only the sha256 of the value; session ids in the API are those hashes,
  which cannot be replayed as cookies.
- **Expiry**: the earlier of idle (`SESSION_IDLE_SECONDS`, default 12h, measured
  from `last_seen_at`) and absolute (`SESSION_ABSOLUTE_SECONDS`, default 7d from
  login). `last_seen_at` is written at most once a minute.
- `POST /auth/logout` deletes the session row and clears the cookie.
- `GET /auth/status` -> `{setup_completed, login_required}` tells the console
  whether to show setup, login, or the app. `GET /auth/me` -> username,
  surface (`session` | `token`) and when the credential expires.
- `POST /v1/admin/password {current_password, new_password}` requires the
  current password even with a valid credential, then logs out every other
  session (all sessions when done with a token).

## Admin tokens

`POST /v1/admin/tokens {name, expires_in_hours?}` mints `aab_admin_<48 hex>`.
The plaintext appears in that response only; the database keeps its sha256.
`GET /v1/admin/tokens` lists name, created/expires/last-used times, revoked and
expired flags, never the token or its hash. `POST /v1/admin/tokens/{id}/revoke`
kills one. `last_used_at` is refreshed at most once a minute.

Every token has a `scope`, fixed at creation and spelled in its prefix.
`admin` (the default) is the owner credential above. `monitor` mints
`aab_monitor_<48 hex>`, which `/health` and `/v1/health` accept, as
`Authorization: Bearer ...` or as the password of HTTP Basic auth (so a
monitor that cannot set headers can still send it), to answer the full
health summary (200 ok, 503 degraded; `docs/deployment.md`) instead of the
anonymous liveness line. A monitor token is refused everywhere else, before
any lookup, and an admin token or a session is refused on the probe, so the
admin plane stays behind Cloudflare Access even though the probe is outside
it. Failed monitor attempts feed the login rate limiter. Databases from
before the column get it by migration; every token already in them is `admin`.

The CLI uses these: `AAB_ADMIN_TOKEN` (or `--token`) plus `AAB_URL` (or `--url`).
Mint the first one from the console after logging in.

Sessions are managed the same way: `GET /v1/admin/sessions` and
`POST /v1/admin/sessions/{id}/revoke`.

## The guard: `require_admin`

Every admin route (all of `/v1/admin/*` and `/auth/me`) is on one of the
routers listed in `main.ADMIN_ROUTERS` (`routers/admin.py`, `admin_plugins.py`,
`admin_keys.py`, `admin_ops.py`, `admin_telegram.py`, `admin_settings.py`),
each behind a router-level `require_admin` dependency, and a test asserts that
every admin path the app serves is on one of those routers. The guard:

1. When Cloudflare Access is enabled, requires a valid Access JWT first
   (**403**, reason not echoed). Checked before any credential lookup, so a
   caller who bypassed Cloudflare causes no database work.
2. If an `Authorization` header is present, it must be `Bearer aab_admin_...`
   naming a token that is not revoked, not expired, and whose principal is not
   disabled. There is no fallback to the cookie when a header is sent.
3. Otherwise the `aab_session` cookie must name a live session of an enabled
   principal.
4. Any credential failure is **401** `{"error": "admin authentication required",
   "code": "unauthorized"}`, whatever the reason (missing, wrong, expired,
   revoked, disabled).

It returns `AdminContext(principal_id, username, via, credential_id,
expires_at)`. Every human action is recorded under that username and surface,
never as "admin".

## CSRF

Cookie-authenticated requests with a method other than GET/HEAD/OPTIONS must
carry `X-Requested-With: aab-console`, else **403** `csrf`. `SameSite=Strict`
already stops browsers sending the cookie cross-site; the custom header is the
second lock, since a cross-site form cannot set it and a cross-site `fetch`
would need a CORS preflight the broker never grants. Bearer-token requests are
exempt: browsers never attach them automatically. `POST /auth/logout` follows
the same rule when it carries a cookie.

## Cloudflare Access

In public mode the broker refuses to boot unless Access is enabled (or
`ALLOW_INSECURE_ADMIN=true`). With Access enabled, the Access JWT is required on
the console page (`/admin`), on `/auth/*`, on every admin route and on the
OAuth callback page (`/oauth/callback/{service}`), in addition to the owner
credential wherever one applies, so a stolen session cookie or admin token is
useless without the SSO identity too. Point the Access application at
`/admin*`, `/auth*`, `/v1/admin*` and `/oauth*` (`deploy/DEPLOY.md`). Without
`/oauth*` Cloudflare adds no Access JWT to the callback page, the broker
refuses it, and connecting Google or GitHub fails in public mode. The CLI
passes `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` (an Access service
token) when set.

## The OAuth callback page

`GET /oauth/callback/{service}` (`routers/oauth.py`) is where Google and GitHub
send the owner back after consent or an App installation. It is served
**without** an owner credential, and has to be: the owner arrives by a
cross-site redirect from google.com or github.com, and because the
`aab_session` cookie is `SameSite=Strict` the browser does not send it on that
navigation. An owner-guarded page would answer 401 and a connect could never
finish. Cloudflare Access still applies in public mode, as for the console
page and `/auth/*`.

The page is safe without a credential because it holds nothing and can do
nothing by itself. The server fills in only the validated service name and a
CSP nonce, never anything from the URL. Its script strips `code`, `state` and
`installation_id` from the address bar and history, then POSTs them to
`/v1/admin/plugins/{service}/connect/finish`. That POST is same-origin, so the
Strict cookie **is** sent, and it keeps the full guard: `require_admin` and the
CSRF header. Without a live session it is a 401, and the page asks the owner
to log in to the console in another tab and retry, keeping the code in memory
only. The plugin checks `state` (single use, 10 minutes), so a code planted by
someone else cannot complete a connection. The page is left out of the
OpenAPI schema and sent with `Cache-Control: no-store`,
`Referrer-Policy: no-referrer`, `frame-ancestors 'none'` and a nonce CSP. The
authorization code is not logged: the broker's access line carries the path
without the query string, and uvicorn's own access log is off
(`docs/logging.md`).

## Later additions

2FA (TOTP) and passkeys (WebAuthn) are out of scope for v0.2. Both attach to the
same `principals` row: extra columns or a `principal_credentials` table added
through `_MIGRATIONS`, checked in `/auth/login` after the password and before a
session is created. Nothing else changes: sessions, admin tokens and
`require_admin` stay as they are.
