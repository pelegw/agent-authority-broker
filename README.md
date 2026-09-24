# Agent Authority Broker

Let AI agents work in your WhatsApp, GitHub, Gmail, Calendar and Drive
without giving them any of your credentials.

An agent holds one thing: an `aab_` key, which is useful against this broker
and nothing else. The broker holds no target credentials either. Each target
system (WhatsApp, GitHub, Google) runs behind its own plugin container,
which keeps that system's credential
in its own volume, encrypted under its own key (the WhatsApp session, held
by the sidecar, is the one exception; see the caveats), and mints a
short-lived, narrowed token for each call where the target supports one. On
every call the broker works out what the key may do **right now**:

```
effective = P(owner) ∩ G(grant chain) ∩ R(role)   minus hidden and denied resources
```

`P` is what the owner has connected, `G` is the key's grants walked up to
the root, and `R` is the key's role. The broker then allows the call, turns
it into a draft that you approve (in the console or on Telegram), or refuses
it. Every decision is written to a hash-chained, HMAC-signed record, with
the full authority chain that allowed it, before anything happens.

- **Grants only narrow.** An agent can ask for more (you approve) or hand a
  sub-agent a narrower child key (no approval needed, because nothing
  widens). No operation widens authority.
- **Standing grants, few interrupts.** You approve a bounded capability
  once ("post in these rooms, up to 1000 writes a day") instead of every
  action. You are asked again when the agent needs new scope, and for
  drafts.
- **Hidden means missing.** A chat, repo, label or folder you hide answers
  404 to every agent, exactly like one that does not exist, and is left out
  of every list.
- **Agents use REST** (primary) **or MCP**. Neither has an approve call:
  approval is a human act.

It is the successor of WA_GW, a WhatsApp-only gateway, and reuses its
security plumbing (origin lockdown, Cloudflare Access, Telegram approvals,
the Go WhatsApp sidecar). It is a clean break: new repository, `aab_` keys,
namespaced tools and endpoints, and no migration of WA_GW's database.

## Architecture

```
  agents: REST or MCP                        owner: console, Telegram
  Authorization: Bearer aab_...              password session or aab_admin_ token
            │                                           │
            └─────────────────────┬─────────────────────┘
                                  │  public mode only: Cloudflare proxy, WAF,
                                  │  Access on /admin* /auth* /v1/admin* /oauth*,
                                  │  Transform Rule adds X-AAB-Origin
┌─ docker compose ────────────────┼──────────────────────────────────────────────┐
│                                 ▼                                              │
│ edge_net          ┌───────────────────────────┐                                │
│                   │ edge (Caddy, public only) │  origin TLS, Cloudflare mTLS,  │
│                   │ publishes 443             │  origin-secret check           │
│                   └─────────────┬─────────────┘                                │
│                                 ▼          local mode: 127.0.0.1:8080 instead  │
│                   ┌───────────────────────────┐                                │
│                   │          broker           │  REST + MCP, console, keys,    │
│                   │                           │  grants, policy, decisions,    │
│                   │                           │  queue, Telegram cards         │
│                   └─────────────┬─────────────┘  holds no target credential    │
│ broker_net                      │  X-Plugin-Token (one per service)            │
│              ┌──────────────────┼──────────────────────┐                       │
│              ▼                  ▼                      ▼                       │
│   ┌──────────────────┐ ┌────────────────┐ ┌─────────────────────────┐          │
│   │ plugin-whatsapp  │ │ plugin-github  │ │ plugin-google           │          │
│   │ reads the archive│ │ App key,       │ │ gmail, gcal, gdrive     │          │
│   │ read-only        │ │ installation   │ │ OAuth refresh token     │          │
│   └────────┬─────────┘ └────────────────┘ └─────────────────────────┘          │
│ wa_internal│  X-Internal-Token        each plugin: own secret volume, own key  │
│   ┌────────▼─────────┐                                                         │
│   │ whatsapp-sidecar │  Go + whatsmeow; holds the WhatsApp session             │
│   └──────────────────┘                                                         │
└────────────────────────────────────────────────────────────────────────────────┘
```

- **Three networks, one kind of traffic each.** `edge_net` (edge and
  broker), `broker_net` (broker and plugin services), `wa_internal`
  (plugin-whatsapp and the sidecar). The edge cannot reach a plugin; the
  broker cannot reach the sidecar or the message archive. No plugin or
  sidecar port is ever published.
- **The broker sends requirements, not credentials.** With each call it
  sends a `CallScope`: what the key may see, the constraints, and the
  credential the call needs (for example "`gmail.readonly`", or "repo
  `a/b`, `contents:read`"). The plugin mints exactly that. Compromising the
  broker lets an attacker ask plugins to act, but yields no standing
  credential usable elsewhere.
- **Env split.** `scripts/init_secrets.py` writes every secret into one
  `.env`, but compose gives each container only the variables it names. No
  container receives the whole file.

Details, data-flow diagrams and the trust boundaries:
[docs/architecture.md](docs/architecture.md). Operations:
[docs/deployment.md](docs/deployment.md).

## Quick start

Prerequisites: Docker with Compose v2 (Docker Desktop with the WSL2 backend
on Windows), Python 3 for the secrets script (standard library only), and a
phone with WhatsApp for pairing.

```bash
python scripts/init_secrets.py        # writes .env (mode 0600) with 11 generated secrets;
                                      # prints a checklist, never a secret value
docker compose up -d --build
curl http://127.0.0.1:8080/v1/health  # {"status":"ok","version":"0.2.0"}
```

1. **Create the owner account.** Open <http://127.0.0.1:8080/admin>. The
   setup page asks for the `SETUP_TOKEN` from `.env`
   (`grep ^SETUP_TOKEN= .env`), a username and a password (at least 12
   characters). The token is inert once the owner exists. Then log in.
2. **Enable WhatsApp and pair it.** Plugins > WhatsApp > Enable, then
   Pair a device. The console shows the pairing QR (it is also printed in
   `docker compose logs whatsapp-sidecar`). On the phone: WhatsApp >
   Settings > Linked devices > Link a device, and scan. The plugin's health
   goes from "waiting for QR pairing" to connected. The device shows up as
   `AAB` (`DEVICE_NAME` in `.env`, applied at pairing).
3. **Create an agent key.** Agent keys > Create. Pick a role (for a first
   try, `read-draft`: reads act, writes become drafts) and use the
   capability editor to allow WhatsApp actions, optionally only on chosen
   chats, with a mode, a budget and an expiry. The `aab_...` key is shown
   once.
4. **Point an agent at it.** REST is the primary surface:

   ```bash
   export AAB_KEY=aab_...
   curl -s http://127.0.0.1:8080/v1/me -H "Authorization: Bearer $AAB_KEY"
   curl -s -X POST http://127.0.0.1:8080/v1/targets/whatsapp/actions/list_chats \
     -H "Authorization: Bearer $AAB_KEY" -H "Content-Type: application/json" \
     -d '{"params": {"limit": 5}}'
   curl -s -X POST http://127.0.0.1:8080/v1/targets/whatsapp/actions/send_message \
     -H "Authorization: Bearer $AAB_KEY" -H "Content-Type: application/json" \
     -d '{"params": {"to": "972501234567@s.whatsapp.net", "text": "On my way"}, "note": "reply to Alice"}'
   # -> 202 {"status": "pending_approval", "action_id": ...}
   ```

   Hand the agent its guide: `GET /v1/me/skill` is the skill doc filtered
   to that key. Or connect an MCP client instead:

   ```bash
   claude mcp add --transport http aab http://127.0.0.1:8080/mcp \
     --header "Authorization: Bearer aab_..."
   ```

5. **Approve the first draft.** Requests shows it as "Send to Alice: On my
   way", with the key, the note and every parameter. Approve it and the
   broker delivers it, after re-checking that the key is still alive, the
   plugin still enabled and the chat not hidden. Decisions shows the
   decision and outcome rows with their grant chain; Verify chain
   recomputes the HMAC chain.

   Optional, approvals on your phone: create a bot with @BotFather, paste
   its token under Channels > Telegram, start linking there and send
   `/start <code>` to the bot from your private chat within 5 minutes, then
   enable it. Cards arrive with Approve and Reject buttons, and only your
   linked chat and Telegram user are accepted.

Gmail, Calendar and Drive need a Google OAuth client of your own
([docs/plugins/google.md](docs/plugins/google.md)); GitHub needs a GitHub
App ([docs/plugins/github.md](docs/plugins/github.md)). Both are entered in
the console (Plugins: the shared Google account card, and the GitHub
plugin's form), never in `.env`.

## What lives in files vs the console

`.env` holds only what cannot live in the database:

- **Generated bootstrap secrets**: `SETUP_TOKEN`, `BROKER_SECRETS_KEY`,
  `DECISION_SIGNING_KEY`, `ORIGIN_SECRET`, `SIDECAR_TOKEN`, and a
  `PLUGIN_TOKEN_<SERVICE>` and `PLUGIN_SECRETS_KEY_<SERVICE>` for
  `WHATSAPP`, `GITHUB` and `GOOGLE`. They must exist before the database is
  readable or before containers can authenticate each other. Rotate one
  with `python scripts/init_secrets.py --rotate NAME`.
- **Exposure settings that fail closed at boot**: `CF_ACCESS_*`,
  `ALLOW_INSECURE_ADMIN`, the base `MCP_ALLOWED_HOSTS`, `SITE_DOMAIN`. They
  are env-only so that a hijacked console session cannot weaken them.
- **Compose values**: `BROKER_PORT`, `TZ`, `DEVICE_NAME`,
  `GITHUB_APP_KEY_DIR`.

Everything else is configured in the console:

- **The Telegram bot token** (Channels > Telegram), stored in `broker.db`
  encrypted under `BROKER_SECRETS_KEY`, write-only.
- **Every plugin credential**: the GitHub App id, slug and private key (or
  the PAT fallback), and the Google OAuth client id and secret (one shared
  Google account card for Gmail, Calendar and Drive). Secret fields are
  relayed once to the plugin container and encrypted there; the broker never
  stores them.
- **Plugin enable/disable and non-secret config**, and the **operator
  settings** (Settings: delegation limits, session lifetimes, draft TTL,
  scheduling bounds, timeouts, extra MCP hosts), each typed and bounded,
  stored in `broker.db`.

See [docs/configuration.md](docs/configuration.md).

## Plugins

Each plugin is a manifest (actions, resources, narrowings, constraints)
plus an adapter in its own container. Tools, REST routes, approval cards,
the console's capability editor and the skill doc are all generated from
the manifests. Every decision (and `GET /v1/me`) carries `enforced_where`,
which says per dimension whether the **target** enforces a limit (the token
the plugin mints cannot exceed it) or only the broker and the plugin do
(**proxy**).

| Plugin | Container | What agents can do | Target-enforced | Proxy-only | Doc |
|---|---|---|---|---|---|
| WhatsApp (`whatsapp`) | `plugin-whatsapp` + `whatsapp-sidecar` | List and read chats, search messages and contacts, download media, long-poll for new messages; send a text (draftable, schedulable) | Nothing: the linked device is the whole account | Chat selector, hidden chats (filtered in SQL), mode, budget | [whatsapp.md](docs/plugins/whatsapp.md) |
| GitHub (`github`) | `plugin-github` | List repos; read issues, PRs and files; open, comment on and close issues, create branches, push a file, open PRs; merge a PR and delete a branch (destructive) | With a GitHub App: the repo list and the permissions, through a per-call installation token | Branch pattern, hidden repos, mode, budget; with the PAT fallback, everything | [github.md](docs/plugins/github.md) |
| Gmail (`gmail`) | `plugin-google` | Search and read threads, attachments and labels; create drafts, send, label, archive; trash and delete (destructive) | Read vs write, through the OAuth scopes of each call's token | Labels, contacts, recipient domains, date window, attachments, bcc, read state, hidden threads and labels | [google.md](docs/plugins/google.md) |
| Calendar (`gcal`) | `plugin-google` | List calendars and events, free/busy; create, update and respond to events; delete (destructive) | Read vs write, by scope | Calendars, attendees, free/busy-only visibility, time window, private and others' events, hidden events | [google.md](docs/plugins/google.md) |
| Drive (`gdrive`) | `plugin-google` | List, search, read metadata, download; create folders, upload, move, share; trash and delete (destructive) | Read vs write, by scope | Folder subtree (parent-chain walk by id), file types, shared drives, content vs metadata only, download size, external sharing, hidden files | [google.md](docs/plugins/google.md) |

All three plugin services (`plugin-whatsapp`, `plugin-github`,
`plugin-google`) are real. Gmail, Calendar and Drive share one container
because they share one Google account: one OAuth client, one refresh token,
and in the console one Google account card with one Connect. Adding the
second and third plugins touched no engine file
([docs/platform-thesis.md](docs/platform-thesis.md)).

## Authority model

- **Roles** cap a key coarsely: `read-only`, `read-draft` (writes and
  destructive actions only as drafts), `read-act` (destructive actions only
  as drafts), `full`.
- **Capabilities** are allow statements (target, actions, selector,
  constraints, mode `draft < direct`, expiry, budget). A grant is a list of
  them, and nothing else carries authority.
- **Narrowing forms** are shared by every plugin: `list`, `subtree`,
  `pattern`, `range`, `flag`, `level`. Each defines "narrower than" and
  "meet", so a child grant is computed, never trusted.
- **Delegation**: an agent mints a child key whose grants are `narrow()` of
  its own, with role, rate and expiry no wider (depth 3 by default).
  Revoking any link or disabling any key stops everything below it on the
  next call, with no cascade writes.
- **Hidden == 404**: owner-hidden resources and per-key denies sit outside
  the lattice, only grow along a chain, and are subtracted last.
- **Standing grants vs approvals**: a human is interrupted for scope
  expansion (`request_permission`) and for drafts only. The approval-volume
  simulation counts 20 interrupts against 712 for per-action approval over
  an 8-hour workload (2.8%).

The algebra and its property tests: [docs/grant-algebra.md](docs/grant-algebra.md).
Delegation: [docs/delegation.md](docs/delegation.md). The simulation:
[docs/approval-volume.md](docs/approval-volume.md).

## Agent surface

- **REST (primary).** Every action is
  `POST /v1/targets/{target}/actions/{action}` with `{"params": {...}}`,
  plus optional call controls (`as_draft`, `run_at` or `delay_seconds`,
  `note`). `GET /v1/me` is the key's access: capabilities per target,
  `enforced_where`, remaining budgets, expiry, never what is hidden.
  Answers: `200` with the result, `202` `pending_approval` or `scheduled`,
  `403 out_of_grant`, `404` (missing or hidden), `429`, `503` (not
  performed, safe to retry), `502` (outcome unknown, never retried
  automatically). Also `/v1/permissions` (ask for more), `/v1/delegations`,
  `/v1/actions`, and `GET /v1/me/openapi.json` with exactly the routes the
  key can reach.
- **The skill doc.** `GET /skill` is the agent guide for every enabled
  plugin, with the base URL filled in (in public mode it needs a key).
  `GET /v1/me/skill` is the same guide filtered to the calling key.
- **MCP (alternative).** `/mcp`, stateless streamable HTTP, same bearer
  key. The tool list is computed per request: generic tools
  (`get_my_access`, `request_permission`, `delegate`, ...) plus one
  `<plugin>_<action>` tool per action the key can reach right now. The
  resource `broker://skill` is the key's guide. See [docs/mcp.md](docs/mcp.md).
- **Claude skill.** `integrations/claude-skill/agent-authority-broker/SKILL.md`
  is generated from the vendored plugin manifests (`aab skill build`; CI
  fails on drift) with a `{{BASE_URL}}` placeholder. Copy the folder into
  `~/.claude/skills/`.

## Console

`/admin` is one static page: no build step, a nonce CSP, and no data in the
page itself. Views:

| View | What it is for |
|---|---|
| Overview | Pending counts, a status card per plugin, decision-chain verification |
| Requests | Drafts and permission requests to approve or reject (a permission request states the budget it adds) |
| Scheduled | Actions waiting for their time; cancel |
| Decisions | The decision record with key and grant chains, `enforced_where` and outcomes; Verify chain |
| Plugins | Enable and disable, config forms generated from the manifest (secret fields write-only), health, the enforcement badge (`target`, `mixed` or `proxy`, from what the plugin last reported), connect: WhatsApp QR pairing, the GitHub connect panel (App install, installed permissions, App or PAT mode), and one shared Google account card for Gmail, Calendar and Drive (client id and secret, one Connect, granted and missing scopes) |
| Agent keys | Create keys with the capability editor, edit, rotate, disable, revoke grants |
| Delegations | The key tree with delegated children, why a key is not live, orphans; edit, disable, revoke |
| Hidden resources | Hide a chat, repo, label, folder and so on, picked by name and stored by id |
| Channels | Telegram: the bot token (write-only), linking your chat, enable, test message, poll-loop health |
| Settings | Every operator setting with its default, bounds and source; what lives in files and why |
| Account | Password, admin tokens for the CLI, sessions |

Everything the console does goes through the admin API (`/v1/admin/*`),
which the `aab` CLI and scripts can call with an admin token. See
[docs/console.md](docs/console.md).

## Exposing to the internet

By default only the broker is published, on `127.0.0.1`. For agents
elsewhere, run the public overlay behind Cloudflare:

```bash
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d --build
```

The overlay removes the broker's host port, turns on origin lockdown and
adds a Caddy edge on `443`. The origin is locked three ways: the security
group accepts only Cloudflare's ranges, Caddy requires Cloudflare's client
certificate (Authenticated Origin Pulls), and the app rejects requests
without the `X-AAB-Origin` secret a Transform Rule adds. A Cloudflare Access
application covers the admin plane: `/admin*`, `/auth*`, `/v1/admin*` and
`/oauth*` (the OAuth callback page). Agent paths are not behind Access;
agents authenticate with their key. The broker refuses to boot in public
mode without Access. Step-by-step runbook (EC2 and Cloudflare):
[deploy/DEPLOY.md](deploy/DEPLOY.md).

## Development

```bash
# broker: authority core, engine, REST, MCP, console, CLI
cd broker
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]" -e ../plugin-runtime   # .venv/bin/ on Linux/macOS
.venv/Scripts/python -m pytest

# plugin runtime and each plugin service (from the repo root)
pip install -e "plugin-runtime[dev]"                     && (cd plugin-runtime && python -m pytest)
pip install -e plugin-runtime -e "plugins/whatsapp[dev]" && (cd plugins/whatsapp && python -m pytest)
pip install -e plugin-runtime -e "plugins/google[dev]"   && (cd plugins/google && python -m pytest)
pip install -e plugin-runtime -e "plugins/github[dev]"   && (cd plugins/github && python -m pytest)

# Go WhatsApp sidecar
cd sidecars/whatsapp && go build ./... && go test ./...
```

- The broker runs **one** uvicorn worker on purpose: the per-minute limiter
  is in-process and SQLite has one writer.
- Conventions (small modules, a test for every behaviour, additive schema
  changes, manifests are data, secrets only through the stores) are in
  [CLAUDE.md](CLAUDE.md). The version lives only in `VERSION`.
- The skill doc under `integrations/` is generated. After a manifest
  change: `aab skill build --all-plugins --base-url "{{BASE_URL}}" --out integrations/claude-skill/agent-authority-broker/SKILL.md`.

**The `aab` CLI** (installed with the broker package) talks to the admin
API. Set `AAB_URL` and `AAB_ADMIN_TOKEN` (mint the first token under
Account); behind Cloudflare Access also `CF_ACCESS_CLIENT_ID` and
`CF_ACCESS_CLIENT_SECRET` (an Access service token).

```
aab setup --username owner          aab keys create|list|rotate|disable
aab tokens create|list|revoke       aab grants list|approve|reject|revoke
aab sessions list|revoke            aab actions list|approve|reject|cancel
aab password                        aab plugins list|enable|disable|health|config
aab skill build ...                 aab hidden list|add|rm
aab simulate --hours 8 --seed 7     aab decisions list|verify
```

`aab simulate` runs the approval-volume simulation locally, from a source
checkout: one seeded workload through the real engine, under standing
grants and under per-action approval, printing the interrupt table.

## Status and caveats

Version 0.2.0, single owner.

- **Verified under Docker, except with a paired phone.** The five images
  build, the stack runs as uid 10001 with only the broker published, and
  plugin-whatsapp reads the sidecar's WAL-mode archive through its
  read-only mount (exercised with a stand-in writer, before pairing).
  Pairing and reads of a live, paired archive were not part of that run.
  CI validates the compose files (`docker compose config`) but builds no
  image. [docs/deployment.md](docs/deployment.md) lists the checks to run
  after `docker compose up`, with the expected outputs.
- **Google downscoped refresh is not yet verified against the real
  endpoint.** The plugin refuses any token wider than it asked for; if
  Google ignores the requested subset, the manifests' `scopes` narrowing
  moves to `proxy` rather than claim target enforcement.
- **WhatsApp runs through whatsmeow, an unofficial client.** Meta's terms
  do not allow it, and accounts can be banned. The WhatsApp session is the
  one credential not encrypted at rest (whatsmeow's own store in
  `wa_data`); a backup of that volume is the live account.
- **Single owner, no identity provider.** With no token exchange, the
  Google refresh token and the GitHub App key are held long-term, inside
  their plugin containers only. No 2FA or passkeys yet.
- The decision record is tamper-evident, not tamper-proof: anyone with
  `DECISION_SIGNING_KEY` and write access to `broker.db` can rewrite it.
- Budgets belong to grants, so each approved expansion adds its own daily
  budget to a key.
- A console change to the extra MCP hosts takes effect at the next broker
  start.
