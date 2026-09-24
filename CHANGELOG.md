# Changelog

All notable changes to this project are documented here. The version number
lives only in `VERSION`.

## [0.2.0] - unreleased

First release as the Agent Authority Broker, the successor of WA_GW 0.1.0.
Clean break: new repo, new names (`aab_` keys, namespaced tools and endpoints),
no migration from WA_GW's database.

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
- The manifest's `config_schema` is empty (sidecar URL, token and archive
  path are deployment env); the broker's vendored copy must stay
  byte-identical.
- Image: `python:3.12-slim`, uid 10001, one uvicorn worker, TCP
  healthcheck. The container reads the generic `PLUGIN_TOKEN`,
  `PLUGIN_SECRETS_KEY` and `PLUGIN_SECRETS_DIR`; an empty `PLUGIN_TOKEN`
  or `SIDECAR_TOKEN` refuses to boot. CI job for the plugin. `docs/plugins/whatsapp.md`.

### Added (phase 8: simulation)
- Approval-volume simulation (`broker/tests/simulation`, `aab simulate`):
  one seeded workload through `engine.perform`, under standing grants and
  under per-action approval. Over 8 hours (seed 7): 20 vs 712 human
  interrupts (2.8%). The default `per_day` budget is 1000.
  `docs/approval-volume.md` also covers the 200-budget run and how
  budgets add up across approved grants.

### Documentation
- `docs/architecture.md`: system description, deployment topology, data
  flow diagrams with numbered flows, security notes per trust boundary,
  glossary, build status and open points.
- `docs/auth.md`, `docs/grant-algebra.md`, `docs/manifest-schema.md`,
  `docs/plugin-api.md`, `docs/mcp.md`, `docs/configuration.md`,
  `docs/deployment.md`, `docs/plugins/whatsapp.md`,
  `docs/approval-volume.md`.
