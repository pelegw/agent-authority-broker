# Changelog

All notable changes to this project are documented here. The version number
lives only in `VERSION`.

## [0.2.0] - unreleased

First release as the Agent Authority Broker, the successor of WA_GW 0.1.0.
Clean break: new repo, new names (`aab_` keys, namespaced tools and endpoints),
no migration from WA_GW's database. Agents hold no credentials and neither
does the broker: each target runs in its own plugin container, every call is
evaluated live against `P(owner) ∩ G(grant chain) ∩ R(role)`, and every
decision is recorded in a hash-chained log.

### Added (phase 0: skeleton)
- Repository layout, single-source `VERSION`, hatch dynamic version.
- `broker.config` settings (ported from WA_GW) with the broker's own secrets
  (`SETUP_TOKEN`, `BROKER_SECRETS_KEY`, `DECISION_SIGNING_KEY`), session limits,
  delegation depth and scheduler tick; admin token and WhatsApp-specific
  settings removed.
- Full `broker.db` schema: principals, sessions, admin tokens, agent keys with
  delegation parent, grants, plugins, encrypted plugin secrets, hidden
  resources, queued actions, hash-chained decisions, capacity ledger, audit log.
- Ported security plumbing: origin-secret guard (`X-AAB-Origin`), Cloudflare
  Access JWT verification, fail-closed boot in public mode.
- `GET /health`, `GET /v1/health` returning `{status, version}`;
  compact `{"error", "code"}` error bodies.
- `scripts/init_secrets.py`: stdlib-only generator for every broker-owned
  secret (0600, no overwrite without `--force`, `--rotate NAME`, never prints
  a secret); `.env.example` generated from the same table.
- WhatsApp sidecar copied from WA_GW to `sidecars/whatsapp` (module path
  renamed, default device name `AAB`).
- Compose (local + public overlay with Caddy edge), EC2 deploy scripts, CI.
- Broker and sidecar images run as the non-root user `aab` (uid 10001); the
  broker image has a `HEALTHCHECK` on `/health`.

### Added (phase 1: identity)
- Single owner principal: scrypt password hash, one-time `SETUP_TOKEN`
  setup (inert afterwards), per-IP failed-login limiter.
- `aab_session` cookies (stored as sha256) with idle and absolute expiry;
  owner admin tokens `aab_admin_...`, stored hashed and revocable.
- `deps.require_admin` returns an `AdminContext` (session cookie or admin
  bearer; Cloudflare Access checked first when enabled; CSRF header on
  cookie writes; a uniform 401 on any credential failure).
- `/auth` status, setup, login, logout and `/auth/me`; password change,
  admin tokens and sessions over the admin API and the `aab` CLI.
  `docs/auth.md`.

### Added (phase 2: authority core)
- Manifest validation (resources, narrowings in the six forms, constraints,
  actions with side effects, modes, `target_permissions`, summary
  templates, skill text) and strict, extra-forbidding params models.
  Vendored manifests for `whatsapp` and `github` (sketch), and the `echo`
  test plugin that exercises every form.
- Grant algebra: `Capability` with `cap_le` and `meet` over the six forms,
  failing closed on anything uninterpretable; `narrow()` as the only
  producer of the sealed `NarrowedCapabilities`; roles; the virtual owner
  ceiling; per-key denies outside the lattice; `effective` re-walked on
  every call.
- `authority/store.py` is the only writer of grants: root grants refuse
  delegated keys, and child grants accept only `narrow()` output and
  re-check `grant_le` against the parent inside the insert transaction.
- `aab_` agent keys with principals, the parent chain (a dead ancestor or
  excess depth is 401) and merged denies.
- Hypothesis property suite (11 properties), including: `narrow()` never
  exceeds the parent, a leaf's authority stays within every ancestor's,
  breaking any link empties the leaf, a disabled plugin leaves no
  capability, random delegation sequences never exceed the parent, denies
  only grow along a chain. `docs/grant-algebra.md`,
  `docs/manifest-schema.md`.

### Added (phase 3: engine)
- `aab_plugin_runtime` (`plugin-runtime/`): hosts one or more adapters
  behind the internal plugin API with a constant-time `X-Plugin-Token`
  gate, a write-only Fernet secret store that refuses to boot without its
  key, `AdapterError` mapping to 4xx/502/503, and the connect endpoints.
  `docs/plugin-api.md`.
- Broker adapters (`InProcessAdapter`, `RemoteAdapter`) and the plugin
  registry: per-service env discovery, manifest pinning against the
  vendored copy (id and version), `plugins` rows, connection state for the
  owner ceiling.
- `policy.evaluate`; the hash-chained decision record (HMAC under
  `DECISION_SIGNING_KEY`, `verify`, downgrade guard); the capacity ledger
  (one charge per grant in the chain); `engine.perform` (decision before
  side effect, one queue choke point, post-filter of rows naming a hidden
  resource); the action queue, delivery with re-check and the scheduler;
  hidden resources.
- Agent REST (targets and actions, long poll, resolve, actions, `/v1/me`,
  permissions, per-key OpenAPI) and admin REST (plugins with the connect
  relay and the OAuth callback page, keys and grants, actions, hidden
  resources, decisions), with `aab` CLI commands. An import-graph test
  proves the agent surfaces cannot reach `services/admin.py` or
  `identity/`.
- Agent params that are not UTF-8 JSON (lone surrogate, NaN, Infinity) are
  a recorded 400 `invalid_params`, with no plugin call and no reservation.
  A long-poll call without a `cursor` is answered at once. REST binary
  results carry `X-Content-Type-Options: nosniff` and
  `Content-Disposition: attachment`.

### Added (phase 3: per-plugin containers)
- Compose runs `broker`, `plugin-whatsapp`, `whatsapp-sidecar`,
  `plugin-github` and `plugin-google` (plus `edge` in the public overlay)
  on three networks: `edge_net` (edge and broker), `broker_net` (broker and
  plugin services), `wa_internal` (plugin-whatsapp and sidecar). The broker
  no longer mounts `wa_data`.
- Env split: no `env_file`; each service names only the variables it
  receives. One secret volume per plugin service (`whatsapp_secrets`,
  `github_secrets`, `google_secrets`).
- `init_secrets.py` also generates `PLUGIN_TOKEN_<SERVICE>` and
  `PLUGIN_SECRETS_KEY_<SERVICE>` for `WHATSAPP`, `GITHUB` and `GOOGLE`
  (11 secrets in total).
- `docs/deployment.md`. `DEPLOY.md` adds `/oauth*` to the Access
  application, the OAuth redirect URIs and the plugin secret volumes in
  backups. CI validates both compose shapes with `docker compose config`.

### Added (phase 3: MCP)
- `/mcp` on the official SDK's low-level `Server` behind a stateless
  `StreamableHTTPSessionManager` (JSON responses), routed by a `BrokerApp`
  splitter under the origin guard; a fresh session manager per lifespan;
  DNS-rebinding protection from the Host allowlist.
- `aab_` key auth through the same `agent_auth.authenticate` as REST.
- The tool list is computed per request: generic tools plus one
  `<plugin>_<action>` tool per reachable action, from the same
  `services/agent.reachable_actions` as REST `/v1/targets` and
  `/v1/me/openapi.json`. Calls go through `engine.perform`. No approve
  tool. `tests/test_parity.py` checks REST/MCP parity. `docs/mcp.md`.

### Added (phase 3: Telegram and console-managed settings)
- `crypto.py`: broker-side secret store in `plugin_secrets` (slot
  `broker`), Fernet under `BROKER_SECRETS_KEY`. Boot fails closed when
  encrypted rows exist and the key is missing; a replaced key reads as
  "re-enter required".
- Telegram approvals: manifest-derived cards (every parameter the summary
  does not show, the delegation chain, grant breadth and duration); an
  oversized card is replaced by a button-less pointer to the console and
  re-checked at tap time;
  one-time link code from a private chat; chat id and user id both checked;
  kill switch. The bot token is entered in the console and the poll loop
  starts and stops with it, without a restart. The inbound half is
  imported only by `main.py` and the admin router. `AdminContext.via`
  gains `telegram`.
- Operator settings edited in the console (`runtime_settings`,
  `GET`/`PATCH /v1/admin/settings`): env values are the defaults, console
  values override them, each setting is typed and bounded, and changes
  are audited. The MCP Host allowlist is the env list plus console extras.

### Changed (configuration: files hold less)
- `.env` holds only generated bootstrap secrets and fail-closed exposure
  settings (`docs/configuration.md`). The `TELEGRAM_BOT_TOKEN`,
  `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`,
  `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` placeholders
  are gone from `init_secrets.py`, `.env.example` and compose; the owner
  enters these in the console (Channels > Telegram, Plugins > GitHub and
  Google), and a test keeps them out of every deployment file.
  `GITHUB_APP_KEY_DIR` stays as an optional file-based alternative for the
  GitHub App key.
- Telegram: an unset linked user matches nobody (WA_GW let anyone in the
  linked chat tap).

### Added (phase 4: WhatsApp plugin service)
- `plugins/whatsapp` (`aab_plugin_whatsapp`), hosted by the plugin
  runtime: every manifest action; CallScope chat visibility applied in SQL
  (empty `allow_only` means no chats, a malformed scope is 400); a hidden
  chat is 404 on get, read, chat-scoped search and media, and media is
  checked before the sidecar is called; rows carry `resource_ref`.
- The archive is opened read-only (`mode=ro`, `query_only`). The sidecar
  client follows the 503/502 contract, follows no redirects, ignores proxy
  env and checks the 64 KiB send limit before the network. The
  `sidecar_qr` connection proxies the QR, answers `none` once paired, and
  refuses disconnect with 409 and the unlink hint.
- JIDs: WA_GW's `normalize_jid` for the send path (tested against the Go
  sidecar's vectors), and a read path that also accepts status, broadcast
  and channel chats so they can be hidden and read; sends to them are 400.
  A canonical user part is required, so no alias spelling can name a
  hidden chat.
- Follow-ups: `check_new_messages` without a cursor is a bootstrap
  answered at once (holding it skipped whatever arrived mid-wait);
  `get_media` is served as a `nosniff` attachment named after the message
  id, with active types downgraded to `application/octet-stream`.
- The manifest's `config_schema` is empty (sidecar URL, token and archive
  path are deployment env); the broker's vendored copy must stay
  byte-identical.
- Image: `python:3.12-slim`, uid 10001, one uvicorn worker, TCP
  healthcheck. The container reads the generic `PLUGIN_TOKEN`,
  `PLUGIN_SECRETS_KEY` and `PLUGIN_SECRETS_DIR`; an empty `PLUGIN_TOKEN`
  or `SIDECAR_TOKEN` refuses to boot. CI job for the plugin.
  `docs/plugins/whatsapp.md`.

### Added (phase 4: owner console, pass 1)
- `/admin` and every `/admin/...` path serve one static page
  (`templates/console.html`: vanilla JS, hash router, no build step).
  `GET /auth/status` picks setup, login or the app; cookie auth with
  `X-Requested-With: aab-console` on every call; a 401 anywhere returns to
  the login page.
- Views: Overview (counts, a card per plugin, chain verification),
  Requests (drafts rendered from the manifest's `summary_template` with
  the key chain, note and every parameter; permission requests as
  readable capabilities, each stating the budget it adds), Scheduled,
  Decisions (filters, key and grant chains, `enforced_where`, verify),
  Plugins (enable/disable, config form from `config_schema` with
  write-only secret fields, health, connection panels for `sidecar_qr`,
  `github_app` and `google_oauth`), Agent keys (capability editor
  generated from the manifests, denies, plaintext once, edit, rotate,
  disable, revoke grants), Hidden resources (picked by name, stored by
  id), Account (password, admin tokens, sessions). Delegations, Channels
  and Settings came with pass 2 (below).
- `GET /v1/admin/plugins` carries a manifest projection
  (`plugins/manifest_view.py`) that the editors and approval cards are
  built from.
- A fresh nonce CSP per response (`form-action 'none'`, `base-uri 'none'`,
  `frame-ancestors 'none'`), because the page shows agent-written text to
  the person who approves those agents; no HTML-string sinks or inline
  handlers (tested); bidi override controls shown as visible markers;
  plugin-supplied links must be `https:`. `docs/console.md`.

### Added (phase 4: owner console, pass 2)
- Channels: the Telegram card (token state including "re-enter required",
  bot name, link state, poll-loop health, active). The bot token is
  write-only: the password field is read once and emptied before the
  request is sent, nothing ever writes a token back, and the status's
  token field is only compared with its state words (tested). Linking
  shows the one-time code and a `t.me` deep link (https on `t.me` only),
  warns that whoever sends the code first becomes the approver, drops the
  code on navigation and polls until linked or expired. Enable, disable,
  test message, unlink and clear token, each confirmed.
- Settings: every operator setting from `GET /v1/admin/settings` with its
  value, default, bounds, unit and source; one form that sends only the
  changed names, reset sends `null`; one input per setting type (tested
  against `runtime_settings.SPECS`). `mcp_allowed_hosts_extra` shows the
  file's hosts and says it applies at the next broker start. "What lives
  in files" lists the env-only keys and why they are not editable here.
- Delegations: the key forest from `/v1/admin/keys/tree` with status,
  live, depth and orphan badges, why a key is not live, a grants summary
  and the capabilities; expand and collapse; edit, disable and enable; and
  revoke (the key is disabled first, which stops it and its subtree at
  once, then its live grants are revoked, the effect of
  `revoke_delegation`). Agent key rows link to their node
  (`#/delegations/<id>`).
- Plugins: plugins that share a connection slot (`connection.shared`) get
  one card for the shared fields and the one connection, relayed through
  any member: the Google account card (client id and secret, one Connect
  for Gmail, Calendar and Drive, granted scopes, and missing scopes as
  "reconnect needed" once connected). Member cards keep their own status
  and scopes.
- The GitHub connect panel reads what `plugin-github` reports: the
  installed permissions, App or PAT mode, the repository selection and
  count. Finishing by installation id sends the `state` that
  `connect/start` issued. In PAT mode there is nothing to install, and the
  panel says every restriction is proxy-enforced.
- The enforcement badge follows `policy.enforced_where`: the plugin's live
  report when it is `target`, `mixed` or `proxy`, otherwise "proxy (not
  reported)", never the manifest's claim alone (a test ties the values to
  `plugins.settings.ENFORCEMENT_VALUES`).
- Background timers stop on navigation and sign-out; the header gains a
  Telegram pill.

### Added (phase 5: delegation)
- `delegate`, `list_my_delegations` and `revoke_delegation` over REST
  (`/v1/delegations`) and MCP. A child key is carved out of the caller's
  own authority: the request is narrowed against each live grant's current
  effective view (chain meet, ceiling, every ancestor's role, denies), and
  anything that does not fit is `400 clipped` with `clipped` and `allowed`
  (hidden and denied ids removed). Child keys are named
  `<caller>/<name>`; role, rate and lifetime are at most the caller's
  (`400 exceeds_parent`); depth is capped (`400 depth_exceeded`, and the
  MCP tool is not listed at the cap); each attempt spends the caller's
  rate; a half-made delegation is undone (`409 conflict`).
- `revoke_delegation` reaches descendants only (anything else is 404), and
  an agent can only ever move a grant to `revoked`.
- `GET /v1/admin/keys/tree`: every key as a forest with status, liveness
  and grants; unreachable keys flagged as orphans. `get_my_access` gains
  `parent`, `delegations` and `can_delegate`.
- Delegation limits are operator settings read per call:
  `max_delegation_depth` (default 3) and `max_live_delegations`
  (default 25, 1-200).
- Hypothesis properties 10 and 11 driven through the REST and MCP layers
  (`tests/test_delegation_properties.py`). `docs/delegation.md`.

### Added (phase 5: generated skill doc)
- The agent guide is generated from the manifests, REST first: `GET /skill`
  and `/skill.md` (every enabled plugin; a key is required in public mode),
  `GET /v1/me/skill` (filtered to the calling key, with its current
  capabilities), and the MCP resource `broker://skill` (the same text).
- `aab skill build` renders every vendored manifest into
  `integrations/claude-skill/agent-authority-broker/SKILL.md`; the CI
  skill-drift job rebuilds it and fails on any difference.

### Changed (phase 5: agent requests default to draft)
- A capability an agent asks for (`request_permission` or `delegate`,
  over REST or MCP) without a `mode` is read as `"mode": "draft"`: its
  writes queue for a human unless the agent asks for `"direct"`
  explicitly, so a forgotten field never buys autonomy. Reads stay
  direct. An undraftable write asked for without a mode is `400
  invalid_capabilities`. Owner-authored grants keep `direct` when the mode
  is omitted.

### Added (phase 6: GitHub plugin service)
- `plugins/github` (`aab_plugin_github`): the `github_app` connection
  (install URL with a single-use state nonce; the installation is verified
  with an App JWT before it is stored) mints an installation token per
  call for exactly the addressed repositories and the action's
  `target_permissions`, refuses a token wider than requested, and caches
  it in memory only. A PAT fallback is reported as `proxy` for every
  dimension. Hidden repositories are 404 before any token is minted;
  branch patterns are checked by the plugin. `docs/plugins/github.md`.
- The App id, slug and private key (or the PAT) are console config; the
  plugin reads no GitHub value from env. The key is pasted in the console
  (`private_key_pem`) or named by the `private_key_path` field inside the
  optional read-only `GITHUB_APP_KEY_DIR` bind at `/run/secrets/github`.
- Private key path confinement: a console-set path would be a file-read
  primitive, so the file must resolve (symlinks followed) inside
  `/run/secrets/github`, on configure and on every read. A path elsewhere,
  missing, or not an RSA key is refused with 400 and never echoed; it can
  never point at `/proc/self/environ` (which holds `PLUGIN_TOKEN` and
  `PLUGIN_SECRETS_KEY`). A key in `private_key_pem` wins over the file.
- `plugin-github` receives only the generic `PLUGIN_TOKEN`,
  `PLUGIN_SECRETS_KEY` and `PLUGIN_SECRETS_DIR`. Image
  `python:3.12-slim`, uid 10001, one uvicorn worker, TCP healthcheck. CI
  job for the plugin.

### Added (phase 7: Google plugin service)
- `plugins/google` (`aab_plugin_google`): one container hosting `gmail`,
  `gcal` and `gdrive`, which share one OAuth client and one refresh token.
- `google_oauth` connection: consent for the union of the enabled plugins'
  `target_permissions` (offline, incremental); a 32-byte state nonce stored
  hashed with the redirect URI, 10-minute TTL, consumed by any finish
  attempt; the code exchanged with the plugin's own client secret; the
  refresh token encrypted in the shared `google` slot.
- Each call gets an access token for exactly its scope set, refreshed with
  `scope=<subset>` and cached in memory per scope set. A token with more
  scopes than requested is refused and not cached; a scope the consent did
  not grant is `403 scopes not granted; reconnect`.
- The adapters enforce every narrowing and constraint in the plugin, with
  the broker's `resource_ref` post-filter behind them: Gmail labels (query
  terms and post-filter), contacts, domains, date window, attachments,
  bcc, read state; Calendar calendars (`primary` resolved first),
  attendees, free/busy visibility, time window, private and others'
  events, hidden events; Drive folder subtree by parent-chain walk
  (incomplete chains fail closed), mime types, shared drives, file
  content, download size, external sharing, no shortcut following.
- Flags are named so that `true` is the permissive side (`private_events`,
  `others_events`, `file_content` instead of the plan's `hide_private`,
  `own_events_only`, `metadata_only`), because the algebra drops a flag's
  `true` as top; a lint enforces it. The plan's `hide_keyword` is dropped
  (a deny list); events are hideable by id instead.
- Platform features the lane needed: shared config fields
  (`config_schema[].shared`, stored once in the connection's slot), and an
  OAuth `redirect_uri` computed by the broker and passed in
  `/connect/start` (public mode: from `SITE_DOMAIN`, which the public
  overlay now gives the broker; local mode: the address the console used).
- `plugin-google` receives only the generic `PLUGIN_TOKEN`,
  `PLUGIN_SECRETS_KEY` and `PLUGIN_SECRETS_DIR`. CI job for the plugin.
  `docs/plugins/google.md`.

### Added (phase 8: simulation)
- Approval-volume simulation (`broker/tests/simulation`, `aab simulate`):
  one seeded workload through `engine.perform`, under standing grants and
  under per-action approval. Over 8 hours (seed 7): 20 vs 712 human
  interrupts (2.8%). The default `per_day` budget is 1000.
  `docs/approval-volume.md` also covers the 200-budget run and how
  budgets add up across approved grants.

### Fixed
- OAuth connect could never finish: `GET /oauth/callback/{service}`
  required the owner session, but the SameSite=Strict session cookie is
  not sent on the provider's cross-site redirect. The callback page is now
  served without an owner credential (Cloudflare Access still applies in
  public mode) and holds no data: it strips `code` and `state` from the
  address bar and POSTs them same-origin to the admin-guarded
  `connect/finish` (session cookie and CSRF header). Without a live
  session it asks the owner to log in in another tab and retry, keeping
  the code in memory only.
- Permission views (`list_my_permissions`, `get_permission_status`, and
  the `allowed` list of a `400 clipped`) no longer name an id the owner
  hid or denied after granting it.
- `enforced_where` went stale in the permissive direction: a failed health
  refresh (plugin unreachable) replaced the stored status with an error
  record without `enforcement`, and the broker then fell back to the
  manifest's claim, so a GitHub plugin last seen on a PAT (proxy) was
  reported as target-enforced in `get_my_access` and in signed decision
  rows. A failed refresh now keeps the last reported `enforcement`, and
  `target` is claimed only when the manifest allows it and the plugin's
  last report says `target` or `mixed`; a record with no or an unknown
  value counts as `proxy`. Only a plugin that has never reported keeps the
  manifest's claim.
- The intermittent SQLite failure seen in test runs (the "SQLite
  flake"): fixed. `PRAGMA journal_mode=WAL` is set once at `init()` instead of on every connection (two first connections racing the switch failed one of them at once), and the lifespan now waits for in-flight threadpool work (scheduler tick, Telegram tap) before shutdown, which also closes a production gap where shutdown could abandon a delivery mid-flight.

### Documentation
- `README.md` rewritten for the release: pitch, architecture, quick start,
  plugins, authority model, agent surface, console, deployment,
  development, status and caveats.
- `docs/architecture.md`: system description, deployment topology, data
  flow diagrams with numbered flows, security notes per trust boundary,
  glossary, build status and open points.
- `docs/auth.md`, `docs/grant-algebra.md`, `docs/manifest-schema.md`,
  `docs/plugin-api.md`, `docs/mcp.md`, `docs/configuration.md`,
  `docs/deployment.md` (with a post-`docker compose up` verification
  checklist), `docs/plugins/whatsapp.md`, `docs/plugins/google.md`,
  `docs/plugins/github.md` (with the GitHub plugin), `docs/console.md`,
  `docs/delegation.md`, `docs/approval-volume.md`.
- `docs/platform-thesis.md`: what the second and third plugins cost the
  platform, measured from the lanes' diffs. Engine files touched: zero for
  each.

### Known limitations
- Not yet verified with Docker running: the image builds, and
  plugin-whatsapp reading the sidecar's WAL-mode archive through its
  read-only mount (`docs/deployment.md` lists the checks).
- Google downscoped refresh is not yet verified against the real token
  endpoint; if Google ignores the requested subset, the `scopes` narrowing
  moves to `proxy`.
- Console changes to `mcp_allowed_hosts_extra` take effect at the next
  broker start.
- Budgets belong to grants, so each approved expansion adds its own daily
  budget to a key (approval cards state the budget a request adds).
- WhatsApp goes through whatsmeow, an unofficial client; its session is
  the one credential not encrypted at rest.

### Upgrade notes
- This is a new repository and a new deployment, not an upgrade in place.
  Nothing migrates from WA_GW: not its database, its `wagw_` keys, its
  grants or its private-chat list. Pair WhatsApp again by QR, create
  `aab_` keys with capabilities in the console, re-hide private chats
  under Hidden resources, and install the new skill
  (`integrations/claude-skill/agent-authority-broker`) in place of
  WA_GW's. Tool and endpoint names are namespaced
  (`whatsapp_send_message`, `POST /v1/targets/whatsapp/actions/send_message`);
  there are no aliases for the old ones.
- Secrets are generated, never copied or typed: run
  `python scripts/init_secrets.py` (or let `deploy/push.sh` run it on the
  host). WA_GW's `ADMIN_TOKEN` has no successor in `.env`: create the owner
  account with the one-time `SETUP_TOKEN`, then mint `aab_admin_` tokens in
  the console for the CLI.
- Everything else is configured in the console: the Telegram bot token
  (WA_GW's `TELEGRAM_BOT_TOKEN` env var is gone), GitHub App and Google
  OAuth client credentials, plugin enable and config, and the operator
  settings. `docs/configuration.md` lists what stays in files and why.
