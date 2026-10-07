# Architecture

This document describes the Agent Authority Broker as the implementation plan
defines it for v0.2.0. It tells these things:

- What the system is for.
- How the deployment works.
- How data moves through the system.
- Where the trust boundaries are.

It has two readers. The first is a newcomer who needs a mental model. The
second is the person who writes the threat model. For that work, the input
is the numbered flows in section 3 and the boundary notes in section 4.

Everything here describes the **target state** of the plan. The source is
the implementation plan, "Deployment: one container per plugin", decided
2026-09-24, with its "Resolved open points". The target state also includes
the decisions made after that:

- The owner enters third-party credentials in the console
  (`docs/configuration.md`).
- The broker passes the OAuth redirect URI to the plugin (section 1.7).

Some older text in the plan comes before these decisions. Examples are
`PLUGIN_TOKEN_<ID>` keyed by plugin id, OAuth state kept in the broker, and
a `TELEGRAM_BOT_TOKEN` env var. In those cases, this document follows the
decisions. Every phase that the plan builds for 0.2.0 is on `dev`.
[Current build status](#6-current-build-status) lists what is on `dev` and
what still needs verification before the tag.

Contents

1. [System description](#1-system-description)
2. [Deployment topology](#2-deployment-topology)
3. [Data flow diagrams](#3-data-flow-diagrams)
4. [Security notes per trust boundary](#4-security-notes-per-trust-boundary)
5. [Glossary](#5-glossary)
6. [Current build status](#6-current-build-status)
7. [Open points](#7-open-points)

---

## 1. System description

### 1.1 Purpose

AI agents need to work in real systems: a WhatsApp account, GitHub repos, a
Gmail inbox, a calendar, a Drive. The usual method is to give the agent a
credential. That gives the agent all of the credential's authority. It also
leaves no record of which human's authority a given action used.

The broker replaces that pattern:

- **Agents hold no target credentials.** An agent holds only an `aab_` key.
  The key works against the broker and nothing else.
- **The broker decides every call live.** It calculates what the key can do
  *now*. Then it permits the call, drafts it (queues it for a human) or
  denies it.
- **Grants can only narrow.** An agent can ask for more (a human approves).
  An agent can give a sub-agent less (no human needed). But no operation
  exists that widens authority.
- **The broker records every decision** with its full authority chain in a
  hash-chained, HMAC-signed decision record.

The pitch in the brief is accountability infrastructure: access control is the
mechanism that produces the evidence.

### 1.2 The authority formula

```
effective(key, t) = P(owner, t)  ∩  G(grant chain, t)  ∩  R(role)   − denies
```

| Term | Meaning in v0.2.0 |
|---|---|
| **P(owner, t)**, the ceiling | Everything the owner can reach *right now*: for every plugin that is **enabled and connected**, all actions, every selector `"*"`, mode `direct`, no expiry or budget. It is virtual (computed, never stored), so disabling or disconnecting a plugin removes it from every agent in the same instant. |
| **G(grant chain, t)** | The union over the key's active, unexpired grants of the *chain meet*: the grant met with its parent, grandparent, up to the root. If any link in a chain is not active and unexpired, that chain contributes nothing. The stored child row is never trusted on its own; the chain is re-walked on every call. |
| **R(role)** | The key's ceiling. It never grants; it caps every capability below it: `read-only` (writes and destructive actions denied), `read-draft` (writes and destructive actions only as drafts), `read-act` (destructive actions only as drafts), `full` (caps nothing: the capabilities decide; the default for owner-created keys). |
| **denies** | Outside the lattice, subtracted last: owner-level `hidden_resources` and the key's own `api_keys.denies` (for a delegated key: its parent's denies plus its own). A hidden or denied resource answers **404**, exactly like a missing one. |

In the brief, the first term is `P(principal)`. v0.2.0 has a single principal
(the owner) and no identity provider. But every table carries `principal_id`,
and the decision record carries the chain. Thus a later version can add more
principals with no change to the model.

### 1.3 Actors

| Actor | What it is | How it authenticates |
|---|---|---|
| **Owner** | The one human principal. Creates keys and grants, approves drafts and permission requests, enables plugins, connects target accounts. | Console password login (`aab_session` cookie) or an owner-minted `aab_admin_…` token; in public mode also a Cloudflare Access identity. Telegram taps, once the Telegram account is linked. |
| **Agent** | Any AI client (Claude over MCP, a script over REST). | `Authorization: Bearer aab_…` |
| **Delegated sub-agent** | An agent holding a child key minted by another agent via `delegate`. Same surface; its authority is a strict narrowing of its parent's. | `Bearer aab_…` (child key; dies with its parent) |
| **Target systems** | WhatsApp (via the Go sidecar), GitHub, Google (Gmail, Calendar, Drive). | Reached only by plugin containers, with short-lived credentials they mint themselves. |
| **Approval channel** | The console, and optionally a Telegram bot. Shows approval cards; the owner's tap is an admin action. | Telegram bot token (entered by the owner in the console, encrypted under `BROKER_SECRETS_KEY`; used outbound only); the tapping Telegram user must be the linked owner. |

### 1.4 Two planes

The brief divides the system into two planes. The deployment makes that
division physical.

- **Authority plane = the `broker` container.** It holds these parts:
  - The owner account, keys, grants, roles and hidden resources.
  - The policy engine.
  - The decision record.
  - The capacity ledger.
  - The queue of drafts and scheduled actions.
  - The notifier.
  - The REST and MCP surfaces.
  - The console.

  This is the policy decision point.
- **Enforcement plane = one container per plugin service, plus native
  sidecars.** A *plugin service* is a container with one or more plugin ids.
  There are three:
  - `plugin-whatsapp` (plugin `whatsapp`, with the Go `whatsapp-sidecar`
    behind it).
  - `plugin-github` (`github`).
  - `plugin-google` (`gmail`, `gcal` and `gdrive`). They share one
    container because they share one OAuth credential.

  Each plugin service does these things:
  - It runs the `aab-plugin-runtime` package, which serves Python adapters.
  - It holds its own target credentials, encrypted under its own key.
  - It runs its own connect flow (OAuth, App install, QR pairing).
  - It exposes a small internal plugin API to the broker.

  The broker's registry maps plugin id → service.

**What the broker holds:**

- The owner's password hash, and hashes of session cookies, admin tokens and
  agent keys.
- Grants and denies.
- The decision record and its `DECISION_SIGNING_KEY`.
- The Telegram bot token, and the installer's read-only GitHub token for
  private plugin repositories. The owner enters both in the console. The
  broker encrypts them in `plugin_secrets` under `BROKER_SECRETS_KEY`.
- One `PLUGIN_TOKEN_<SERVICE>` per plugin service (`WHATSAPP`, `GITHUB`,
  `GOOGLE`), so it can call the plugin API.

**What the broker does NOT hold:** any target credential. It holds none of
these:

- An OAuth client secret.
- A Google refresh or access token.
- A GitHub App private key or installation token.
- A WhatsApp session.
- `SIDECAR_TOKEN`.
- A `PLUGIN_SECRETS_KEY_<SERVICE>`.

It cannot reach the sidecar or the WhatsApp archive. The broker sends
*requirements* ("a token with `gmail.readonly`", "repos `a/b`,
`contents:read`"), and the plugin mints the credential. During a connect
flow, the broker sends an OAuth authorization code to the plugin one time and
keeps nothing.

Consequence for the threat model: an attacker who controls the broker can
*ask* every plugin to act, because the broker holds every
`PLUGIN_TOKEN_<SERVICE>`. What each plugin's connection can mint sets the
limit. The attacker gets no standing target credentials to use from a
different place.

### 1.5 Lifecycle of one call

An agent calls `POST /v1/targets/gmail/actions/search_threads` (or the MCP tool
`gmail_search_threads`; both go through the same `engine.perform`).

1. **Authenticate.** The edge checks come first (public mode). Then the
   broker does these checks:
   - It finds the `aab_` bearer key by its hash, with the rotation grace.
   - The key must be active: not disabled and not expired.
   - Every ancestor key in the delegation chain must be alive.

   Any failure is 401.
2. **Evaluate** (`policy.evaluate`). The broker does these checks in this
   order:
   - Params that UTF-8 JSON cannot encode (a lone surrogate, NaN, Infinity)
     get a recorded 400 `invalid_params` before evaluation starts.
   - A disabled plugin gets a 404-shaped deny.
   - The action must exist and the params must validate. The manifest alone
     decides this.
   - If the plugin has no connection, the broker calculates the key's
     capabilities as if it had one. It checks them against the raw (trimmed)
     selector value. Only a key that some capability covers gets 503
     `not_connected`. Every other key gets the same 403 that it gets when
     the plugin has a connection. Thus a key without authority cannot learn
     the pairing state of a plugin.
   - Then the broker calculates `effective()` live. A key with no capability
     that reaches the action gets its 403 before the broker asks the plugin
     anything.
   - The broker normalizes the selector parameter through the plugin's
     `/normalize`. If the broker cannot reach the plugin, the result is 503
     `plugin_unavailable`. Again, this applies only to a key that a
     capability covers on the raw value.
   - The first capability that covers target + action + resource wins. A
     hidden or denied resource is then 404.
   - If no capability covers it, the result is deny 403 `out_of_grant`.
   - Capability mode `draft`, the caller's `as_draft=true` or a `run_at`
     gives draft.
   - Otherwise the result is `allow`.

   The broker fills `enforced_where` per bounding dimension. The full order
   is in the docstring of `broker/broker/policy.py`.
3. **Decision record.** The broker appends a `decision` row to `decisions`
   **before any side effect**, also for denies. The row holds the result
   (`allow`, `draft` or `deny`), the grant chain from root to leaf,
   `params_hash`, the reason and `enforced_where`.
4. **Deny** returns the error. **Draft** goes to the action queue (step 1.6).
   The agent gets 202 `pending_approval` or `scheduled`.
5. **Ledger.** For a write that the broker permits, the broker checks the
   per-key per-minute limiter. `capacity_ledger` reserves one charge per
   grant in the chain (`ledger_grants`). An exhausted budget gives 429, with
   the name of the grant.
6. **Plugin `perform`.** The broker's `RemoteAdapter` sends `POST /perform`
   to the plugin container. The request holds the params and a `CallScope`:
   the visibility deny set and optional `allow_only`, the constraints, the
   credential requirements and `request_id`. The plugin mints or reuses a
   narrowly scoped token and calls the target.
7. **Outcome.** The engine filters the result again and removes any row that
   names a hidden resource. Then the result goes back to the agent. The
   broker appends an `outcome` row to `decisions` and commits the ledger
   reservation. A **503** from the plugin means "not delivered, safe to
   retry", and it releases the reservation. A **502** means "outcome
   unknown". It keeps the reservation for 24h, and the broker never retries
   it automatically.

Two REST response rules apply on top of this (`docs/plugin-api.md`):

- The broker serves a binary result as an attachment, with
  `X-Content-Type-Options: nosniff`. The header is
  `Content-Disposition: attachment`, and the file name comes from the most
  specific id of the call.
- The broker holds a long-poll read (`GET …?wait=N`) until something is new.
  The exception is a *bootstrap*: a call without a cursor to a `long_poll`
  action that declares a `cursor`. The broker answers it at once, whatever
  `wait` says. Thus the agent skips nothing that arrives during the wait.

MCP returns binary results as base64 content and never waits.

### 1.6 Lifecycle of one draft

1. **Queue.** `actions.queue.create` is the single choke point. It inserts an
   `actions` row, linked to its `decision_id`. The row has `status=pending`,
   or `scheduled` for an already-permitted call with `run_at`. Then it calls
   `notify.notify_action`.
2. **Notify.** The console's `requests` view shows it, with text that the
   broker makes from the manifest's `summary_template`. If the owner linked
   Telegram, the broker sends the owner a card with approve and reject
   buttons.
3. **Human decision.** The owner approves or rejects in the console (session
   or admin token), or taps in Telegram. Each path makes an `AdminContext`
   (principal, surface `session | token | telegram`). The row records
   `decided_by_principal` (the owner's username) and `decided_via`. The
   status changes only by an atomic `UPDATE … WHERE status=?`. Thus a double
   tap cannot deliver twice.
4. **Deliver with re-check** (`actions/deliver.py`; the scheduler also runs it
   for due `run_at` rows). A human-approved row still needs a live key, an
   enabled plugin and a resource that is not hidden. A row that no human
   approved (`approval_source=automatic`) runs `evaluate` again and needs
   `allow`. The broker *holds* the rows of a disabled plugin and does not
   drop them. An approval of a held action gets 409. Delivery then follows
   steps 5 to 7 of the call lifecycle. Pending drafts expire after their TTL.

### 1.7 Plugin enable and disable, hidden resources, delegation

- **Enable/disable** is a broker-side flag in the `plugins` table. Every
  discovered plugin starts disabled. Enable does three things:
  - It validates the config against the manifest's `config_schema`.
  - It sends secret fields one time to the plugin's `/configure`.
  - It stores `/status` as `last_health`.

  Disable removes that plugin's MCP tools, REST routes and skill section on
  the next request. It also holds the plugin's queued actions. The plugin
  keeps its secrets. A plugin container that is down shows as unhealthy. Its
  tools go away only when the owner disables the plugin.
- **Connect and disconnect** run inside the plugin service. The console calls
  `/v1/admin/plugins/{id}/connect/start`. The broker sends this to the
  plugin's `POST /connect/start {enabled_plugins, redirect_uri?}`. The plugin
  answers `{kind: oauth|install|qr|none, url?, state?}`.

  The OAuth redirect URI is the broker's `GET /oauth/callback/{service}`.
  Only the broker knows how the owner reaches it, so the broker calculates
  it:
  - Public mode: `https://<SITE_DOMAIN>/oauth/callback/<service>`, from the
    broker's own env.
  - Local mode: the loopback address that the console used.

  The broker passes it as `redirect_uri`. The plugin stores it beside the
  `state` nonce and uses it again for the code exchange (decided;
  implemented with phase 7).

  What the plugin returns depends on the connection kind:
  - For Google, the plugin builds the auth URL from its own client id, that
    `redirect_uri` and the scopes. The scopes are the union of its enabled
    manifests' `target_permissions`. The plugin generates and stores the
    `state` nonce itself, in its own secret volume. The nonce is valid for
    10 minutes and for one use. The first `/connect/finish` that presents
    it consumes it.
  - For GitHub, it returns the App install URL (same nonce rules).
  - For WhatsApp, it returns `{kind: qr}` (`{kind: none}` once paired). The
    console shows `GET /v1/admin/plugins/whatsapp/connect/qr.png`. The broker
    proxies it from the plugin's `GET /connect/qr.png`, which the plugin
    proxies from the sidecar.

  The callback page needs no owner credential (Cloudflare Access still
  applies in public mode). The provider's redirect is cross-site, so the
  browser does not send the SameSite=Strict session cookie on it. The page
  holds no data. It removes `code` and `state` from the address bar. It
  POSTs them, same origin, with the session cookie and the CSRF header, to
  the admin-guarded `/v1/admin/plugins/{service}/connect/finish`. Without a
  live session, it asks the owner to log in in another tab and retry. The
  broker sends them one time to the plugin's `POST /connect/finish`
  (GitHub: `installation_id`). The plugin exchanges the code with its own
  client secret. It stores the credential in its own volume.

  GitHub sends the owner back to the one Setup URL registered on the App,
  not to a URL from the request. Sometimes that redirect cannot reach the
  broker, for example in a local-only deployment that did not register its
  loopback callback. Then the owner types the installation id into the
  console's GitHub panel, with the `state` that `connect/start` issued. The
  panel then finishes the flow. For WhatsApp, `/connect/finish` is a no-op
  that makes the broker refresh health.

  `disconnect` sends `POST /disconnect` to the plugin, which wipes the
  credential. WhatsApp answers 409: the plugin mounts the session
  read-only, so the owner unlinks the device on the phone and the sidecar
  pairs again.
- **Hidden resources** (`hidden_resources`, by target, kind and normalized id)
  apply to every key. The label is only for display. Enforcement uses the
  id, so a new name for a chat or repo can never unhide it. A get on a hidden
  resource is 404. The adapter boundary filters hidden resources from every
  list, and the engine filters them again. `get_my_access` never lists them.
- **Delegation.** `delegate(name, capabilities, expires_in_hours, reason)`
  creates two things:
  - A child `api_keys` row. It has `parent_key_id` set. Its role, rate and
    expiry are no wider than the parent's. Its denies are a superset of the
    parent's.
  - One `kind=delegation` grant per parent grant, which `narrow()` makes.

  `max_delegation_depth` (3) caps the depth. No human takes part, because
  nothing widens. `revoke_delegation` disables a descendant and revokes its
  grants. Its own descendants die through the chain walk. The console's
  Delegations view does the same for the owner, in two steps (section 7).
  Scope *expansion* (`request_permission`) is the one human interrupt
  besides drafts. It creates a `pending` `expansion` grant. The broker clips
  that grant to what the parent can give (or the ceiling, for a root key).

### 1.8 Structural guarantees

The shape of the code enforces these properties. They do not depend on a
policy check that a wrong setting can break.

- **No approve tool for agents.** Neither MCP nor REST exposes approval to an
  `aab_` key, and Telegram has no agent-facing approve path. A test asserts
  that `mcp_server.py`'s import graph cannot reach `services/admin.py`.
- **Child grants only through `narrow()`.** The store's child-insert path
  accepts only a `NarrowedCapabilities` value. Its constructor needs a
  module-private sentinel. The store asserts `grant_le(child, parent)` as a
  post-condition. Every capability only permits, and capabilities form a
  meet-semilattice, so "widen" has no representation. Hypothesis property
  tests check that widening is unreachable.
- **Chains re-walked on every call.** A change reaches every descendant with
  no cascade writes. Such changes include a revoked link, a disabled parent
  key and a root grant that the owner makes narrower.
- **Hidden == 404.** A hidden or denied resource is indistinguishable from a
  missing one.
- **503 vs 502.** "Not delivered" and "unknown outcome" are different statuses
  end to end. Thus a retry never silently duplicates a send.
- **The broker pins manifests.** The registry validates every manifest that
  a plugin service returns from `GET /manifests`. It compares each one with
  the vendored copy in `targets/<id>/manifest.yaml`, and the id and version
  must match. Thus a plugin cannot widen its declared lattice at runtime.
- **Decision before side effect.** The decision row exists before the broker
  calls the plugin. Thus the engine cannot do an action that has no record.

---

## 2. Deployment topology

### 2.1 Containers, networks, ports and volumes

The deployment is one Docker Compose project. The base file is local-only.
The public overlay adds the Caddy `edge` and removes the broker's published
port.

```mermaid
flowchart TB
  AGL["Local agent / owner browser<br/>(same host)"]
  subgraph CFZ["Cloudflare (public mode only)"]
    CF["DNS proxy + WAF + Access<br/>Transform Rule adds X-AAB-Origin"]
  end
  AGP["Remote agent / owner browser"]

  subgraph HOST["Docker host (EC2 security group: 443 from Cloudflare IPs only)"]
    subgraph EN["network: edge_net (edge + broker only)"]
      EDGE["edge (Caddy, optional)<br/>publishes 443"]
    end
    BROKER["broker<br/>FastAPI :8080<br/>on edge_net + net_whatsapp,<br/>net_github, net_google<br/>publishes 127.0.0.1:8080 (local mode only)"]
    subgraph NWA["network: net_whatsapp (broker + plugin-whatsapp)"]
      PWA["plugin-whatsapp<br/>aab-plugin-runtime: whatsapp"]
    end
    subgraph NGH["network: net_github (broker + plugin-github)"]
      PGH["plugin-github<br/>aab-plugin-runtime: github"]
    end
    subgraph NGO["network: net_google (broker + plugin-google)"]
      PGO["plugin-google<br/>aab-plugin-runtime: gmail, gcal, gdrive"]
    end
    subgraph WN["network: wa_internal (plugin-whatsapp + sidecar only)"]
      SC["whatsapp-sidecar<br/>Go whatsmeow :8081<br/>never published"]
    end
    subgraph NIN["network: net_installer (broker + aab-installer; opt-in overlay)"]
      INS["aab-installer :8070<br/>never published<br/>Docker socket: root on the host"]
    end
    subgraph NEX["network: net_&lt;service&gt; (broker + one installed plugin)"]
      PEX["plugin-&lt;service&gt;<br/>built from plugins.d/&lt;service&gt;/src"]
    end
    VB[("broker_data<br/>broker.db")]
    VW[("wa_data<br/>messages.db")]
    VSE[("wa_session<br/>session.db")]
    VGH[("github_secrets")]
    VPEM[("GITHUB_APP_KEY_DIR bind<br/>/run/secrets/github (ro)")]
    VGO[("google_secrets")]
    VWS[("whatsapp_secrets")]
    VC[("caddy_data + edge/certs (ro)")]
  end

  EXT["Internet services<br/>Telegram Bot API, Cloudflare JWKS,<br/>GitHub API, Google APIs, WhatsApp servers"]

  AGP -->|HTTPS| CF
  CF -->|"HTTPS :443, AOP mTLS"| EDGE
  EDGE -->|"HTTP broker:8080 (edge_net)"| BROKER
  AGL -->|"HTTP 127.0.0.1:8080"| BROKER
  BROKER -->|"HTTP + X-Plugin-Token (net_whatsapp)"| PWA
  BROKER -->|"HTTP + X-Plugin-Token (net_github)"| PGH
  BROKER -->|"HTTP + X-Plugin-Token (net_google)"| PGO
  BROKER -->|"HTTP + X-Plugin-Token (net_&lt;service&gt;)"| PEX
  BROKER -->|"HTTP + X-Installer-Token (net_installer)"| INS
  INS -->|"docker compose via the socket"| PEX
  PWA -->|"HTTP + X-Internal-Token (wa_internal)"| SC
  BROKER -.- VB
  SC -.-|rw| VW
  SC -.-|rw| VSE
  PWA -.-|ro| VW
  PWA -.- VWS
  PGH -.- VGH
  PGH -.-|ro| VPEM
  PGO -.- VGO
  EDGE -.- VC
  BROKER -->|egress| EXT
  PGH -->|egress| EXT
  PGO -->|egress| EXT
  SC -->|egress| EXT
  INS -->|"git clone: allowlisted sources, https"| EXT
```

Notes:

- **Published ports.** Local mode publishes only
  `127.0.0.1:${BROKER_PORT:-8080}` (the broker, loopback only). Public mode
  publishes only `443` (the edge). The public overlay removes the broker's
  published port (`ports: !reset []`). Compose never publishes a plugin port
  or a sidecar port.
- **Networks.** Every deployment has five networks. Each one carries exactly
  one kind of traffic:
  - `edge_net`: edge ↔ broker.
  - One network per plugin service (`net_whatsapp`, `net_github`,
    `net_google`): the broker ↔ that one service.
  - `wa_internal`: plugin-whatsapp ↔ sidecar.

  The opt-in installer adds more networks:
  - `net_installer`: broker ↔ `aab-installer` only.
  - One `net_<service>` per installed external plugin: broker ↔ that plugin
    only. The installer writes it. The plugin's repository never supplies
    it. On install, the installer connects the running broker to it
    (`docker network connect`). On remove, it disconnects the broker. The
    broker never restarts for either.

  The broker is on every network except `wa_internal`. The only other
  container on two networks is plugin-whatsapp (its own and `wa_internal`).
  This has three results:
  - The edge cannot reach any plugin, so a compromised edge cannot call
    `/perform`.
  - No plugin service can reach another. A compromised plugin-github cannot
    even resolve plugin-google's name, so it cannot open a connection to its
    API.
  - The broker cannot reach the sidecar.

  None of the networks blocks egress. Plugins, the sidecar and the broker
  (Telegram, Cloudflare JWKS) need outbound internet.
- **Volumes.**
  - `broker_data`: broker only.
  - `wa_data`: the archive, `messages.db`. The sidecar mounts it read-write,
    and plugin-whatsapp mounts it read-only. Nobody else mounts it, and the
    broker never does.
  - `wa_session`: whatsmeow's `session.db`, the WhatsApp credential. Only
    the sidecar mounts it, read-write. Thus no other container can read the
    session.
  - One secret volume per plugin service, which only that service mounts,
    at `/secrets`: `whatsapp_secrets`, `github_secrets`, `google_secrets`.
  - `caddy_data` and the `edge/certs` bind mount: edge only.

  Compose puts the project name before each name (`aab_broker_data`, ...).
  The deployment uses named volumes, not bind mounts, because SQLite WAL
  locking is unreliable over Docker Desktop's NTFS sharing.
- **GitHub App key bind.** As an option, `plugin-github` (and nothing else)
  also mounts the directory `${GITHUB_APP_KEY_DIR:-./data/github-app}` of the
  machine, read-only, at `/run/secrets/github`. Then the owner can give the
  App private key as a file, and not paste it in the console. For that, the
  plugin's `private_key_path` field points at `/run/secrets/github/app.pem`.
  The plugin reads only a file that resolves (symlinks followed) inside that
  directory. It checks this on configure and on every read. Thus a console
  session cannot turn the field into a read of any other file. Git ignores
  the directory. Make it readable only by uid 10001.
- **The plugin installer** (opt-in, `docker-compose.installer.yml`, loaded when
  `INSTALLER_ENABLED=true`). `aab-installer` mounts the Docker socket. It is
  the only container that does, so it is root on the machine. It also mounts
  the checkout at `AAB_HOME`, at the same path as on the machine. It shares
  `net_installer` with the broker alone. It publishes nothing and clones only
  allowlisted sources. It owns `plugins.d/`. Each installed plugin builds
  from there, and the `compose.yml` that the installer writes there joins the
  compose file set (`scripts/compose-files.sh`). The broker learns an
  installed plugin's URL and token from the installer (`GET /services`), so
  an install needs no new broker environment. Section 4.1 has the boundary.
- **The New Relic overlay** (opt-in, `docker-compose.newrelic.yml`, loaded
  when `NEWRELIC_ENABLED=true`). It sends every service's log lines and the
  audit record to New Relic Logs. `log-shipper` (Fluent Bit) is the only
  container with `NEW_RELIC_LICENSE_KEY`. It is alone on `net_logs` and
  publishes one port, on the host's loopback, for the Docker log driver.
  `audit-exporter` reads `broker.db` from a read-only `broker_data` mount,
  with no network and no credential. `docs/logging.md` has the details.
  `log-shipper` runs as uid 65534 (`nobody`), with a read-only root file
  system and no capabilities.
- Every image runs as a non-root user (`aab`), with one uvicorn worker in the
  broker. The exception is `aab-installer`, which runs as root on purpose.
  Whoever holds the socket is root on the machine. Thus an unprivileged user
  inside adds no boundary.

### 2.2 Secrets and environment per container (the env split)

`scripts/init_secrets.py` generates these values into `.env` (mode 0600,
never printed):

- The broker-owned values.
- Per plugin service, a `PLUGIN_TOKEN_<SERVICE>` and a
  `PLUGIN_SECRETS_KEY_<SERVICE>`, for `WHATSAPP`, `GITHUB` and `GOOGLE`.

Compose maps each value only to the services that need it. **No service
receives the whole `.env`.** The env key is the *service*, not the plugin
id. Each container has one token and one key, whatever number of plugin ids
it holds.

| Container | Receives | Must never receive |
|---|---|---|
| `broker` | `SETUP_TOKEN`, `BROKER_SECRETS_KEY`, `DECISION_SIGNING_KEY`, `ORIGIN_SECRET` (public overlay only; forced empty in the base file), `CF_ACCESS_ENABLED/TEAM_DOMAIN/AUD/ALLOWED_EMAILS`, `ALLOW_INSECURE_ADMIN`, `MCP_ALLOWED_HOSTS`, `SITE_DOMAIN` (public overlay only: builds the OAuth redirect URI `https://<SITE_DOMAIN>/oauth/callback/<service>`), `PLUGIN_URL_<SERVICE>` and `PLUGIN_TOKEN_<SERVICE>` for `WHATSAPP`, `GITHUB`, `GOOGLE` and each installed external plugin (from its rendered overlay), `INSTALLER_URL` and `INSTALLER_TOKEN` (installer overlay only), `BROKER_DB`, `TZ`, `LOG_LEVEL`, `LOG_FORMAT` | `SIDECAR_TOKEN`, any `PLUGIN_SECRETS_KEY_<SERVICE>`, `INSTALLER_ALLOWED_SOURCES`, `NEW_RELIC_LICENSE_KEY`, the Docker socket, the `wa_data` and `wa_session` volumes |
| `plugin-whatsapp` | `PLUGIN_TOKEN_WHATSAPP`, `PLUGIN_SECRETS_KEY_WHATSAPP`, `SIDECAR_URL` (`http://whatsapp-sidecar:8081`), `SIDECAR_TOKEN`, `MESSAGES_DB` (`/data/messages.db`), `LOG_LEVEL`, `LOG_FORMAT`, `wa_data` (ro) | Other services' tokens/keys, broker secrets (`DECISION_SIGNING_KEY`, `BROKER_SECRETS_KEY`, `SETUP_TOKEN`, `ORIGIN_SECRET`), the `wa_session` volume |
| `whatsapp-sidecar` | `SIDECAR_TOKEN`, `DEVICE_NAME`, `TZ`, `LOG_LEVEL`, `SESSION_DIR` (`/session`), `wa_data` (rw), `wa_session` (rw; the only container that mounts it) | Everything else |
| `plugin-github` | `PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY`, `PLUGIN_SECRETS_DIR` (the runtime's generic names, fed from `PLUGIN_TOKEN_GITHUB` / `PLUGIN_SECRETS_KEY_GITHUB`), `LOG_LEVEL`, `LOG_FORMAT`; the App id, slug and private key are console config, not env (+ the optional read-only `/run/secrets/github` bind holding the PEM, a file alternative to pasting it) | Other services' tokens/keys, broker secrets, `SIDECAR_TOKEN` |
| `plugin-google` | `PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY`, `PLUGIN_SECRETS_DIR` (the runtime's generic names, fed from `PLUGIN_TOKEN_GOOGLE` / `PLUGIN_SECRETS_KEY_GOOGLE`), `LOG_LEVEL`, `LOG_FORMAT`; nothing Google-specific: the OAuth client id and secret are console config, and the broker passes the redirect URI with each connect | Other services' tokens/keys, broker secrets, `SIDECAR_TOKEN`, `SITE_DOMAIN` |
| `plugin-<service>` (each installed external plugin) | `PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY`, `PLUGIN_SECRETS_DIR` (the runtime's generic names, fed from `PLUGIN_TOKEN_<SERVICE>` / `PLUGIN_SECRETS_KEY_<SERVICE>`), the literal `environment` of its descriptor, its allowlisted `env_passthrough` (`TZ`, `LOG_LEVEL`, `LOG_FORMAT`), `<service>_secrets` at `/secrets` and the `<service>_*` volumes it declares | Other services' tokens/keys, broker and installer secrets, `SIDECAR_TOKEN`, any bind mount, any other volume or network (the installer renders its overlay; the repository supplies none) |
| `aab-installer` (installer overlay only) | `INSTALLER_TOKEN`, `INSTALLER_ALLOWED_SOURCES`, `AAB_HOME`, `LOG_LEVEL`, `LOG_FORMAT`; the Docker socket and the checkout at `AAB_HOME` (same path inside), so it can read `.env`: it is root on the host | Any other variable in its environment, a published port, any network but `net_installer` |
| `edge` | `SITE_DOMAIN`, `ORIGIN_SECRET`, origin certificate + key, Cloudflare origin-pull CA | Every other secret |
| `log-shipper` (New Relic overlay only) | `NEW_RELIC_LICENSE_KEY` (the only container that receives it); its configuration `ops/fluent-bit/` (ro) | Every other secret, any volume, any network but `net_logs` |
| `audit-exporter` (New Relic overlay only) | `BROKER_DB`, `AUDIT_EXPORT_STATE`, `AUDIT_EXPORT_INTERVAL`, `AUDIT_EXPORT_HASH_RESOURCES`, `LOG_LEVEL`, `LOG_FORMAT`; `broker_data` (ro) and `audit_export_state` | `NEW_RELIC_LICENSE_KEY`, every broker or plugin secret, any network (`network_mode: none`), write access to `broker_data` |

The table names the `.env` entries that feed each container. Inside a plugin
container, the names are generic. `aab_plugin_runtime.from_env` reads
`PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY` and `PLUGIN_SECRETS_DIR`. Compose maps
the service's own values onto them: `PLUGIN_TOKEN:
${PLUGIN_TOKEN_WHATSAPP}`, `PLUGIN_SECRETS_KEY:
${PLUGIN_SECRETS_KEY_WHATSAPP}`, `PLUGIN_SECRETS_DIR: /secrets`. Thus the
image does not depend on the service it runs as. All three plugin services
(`plugin-whatsapp`, `plugin-github`, `plugin-google`) read these generic
names.

Third-party credentials are not in the env split at all
(`docs/configuration.md`). The owner enters them in the console:

- The broker stores the Telegram bot token and the installer's GitHub token
  in `broker.db` (the `plugin_secrets` table, slot `broker`), encrypted
  under `BROKER_SECRETS_KEY`.
- The broker sends the GitHub token to the installer in the body of each
  inspect, install and upgrade request. The installer keeps none.
- The owner enters the GitHub App id and key, and the Google OAuth client id
  and secret, in each plugin's config form. The broker sends them one time
  to that plugin's `/configure` and never stores them.

Compose itself reads `BROKER_PORT` and `GITHUB_APP_KEY_DIR` (port mapping,
bind source). It passes them into no container. Compose has no `env_file`:
each service lists its variables under `environment:`.
`docs/deployment.md` gives the operational view of the same split (volumes,
rotation per secret).

### 2.3 Component table

| Component | Responsibilities | Credentials it holds | Network reachability |
|---|---|---|---|
| **Cloudflare** (public mode) | Public DNS and TLS, WAF and rate limiting, Access SSO on `/admin*`, `/auth*`, `/v1/admin*`, `/oauth*` (the OAuth callback page); injects `X-AAB-Origin` | Origin secret (in the Transform Rule), Access signing keys | Internet-facing; reaches the edge on 443 |
| **edge** (Caddy, optional) | Terminates origin TLS with the Cloudflare Origin Certificate, requires Cloudflare's client certificate (Authenticated Origin Pulls), rejects requests without `X-AAB-Origin`, reverse-proxies to `broker:8080` | Origin cert + key, `ORIGIN_SECRET` | Inbound 443 from Cloudflare only (security group); outbound only to the broker on `edge_net` |
| **broker** | REST + MCP agent surface, console + admin API, OAuth callback page (`/oauth/callback/{service}`), owner identity, grants and authority algebra, policy engine, decision record, capacity ledger, action queue + scheduler, Telegram notifier, plugin registry (plugin id → service) and `RemoteAdapter`, connect-flow relay, skill doc generation | Password/key/token *hashes*, `DECISION_SIGNING_KEY`, `BROKER_SECRETS_KEY`, `SETUP_TOKEN`, all `PLUGIN_TOKEN_<SERVICE>` (an installed plugin's from its environment after a deploy, or from the installer's `GET /services`, in memory only), the Telegram bot token (console-entered, encrypted); an OAuth authorization code in transit only; **no target credentials** | Inbound from the edge on `edge_net` (public) or loopback (local); outbound to each plugin service on that service's own network (`net_whatsapp`, `net_github`, `net_google`), Telegram, Cloudflare JWKS; cannot reach the sidecar |
| **plugin-whatsapp** | Plugin API for `whatsapp`; archive reads over `messages.db` (opened `mode=ro` with `query_only`) with SQL-level visibility filtering; sends and media via the sidecar; connect = QR relay (`/connect/qr.png`) and status mapped from the sidecar's; disconnect answers 409 (unlink on the phone) | `PLUGIN_TOKEN_WHATSAPP` (read as `PLUGIN_TOKEN`; verifies the broker), `PLUGIN_SECRETS_KEY_WHATSAPP` (read as `PLUGIN_SECRETS_KEY`), `SIDECAR_TOKEN`; a secret store in `whatsapp_secrets` that holds nothing today (the manifest's `config_schema` is empty: sidecar URL, token and archive path are deployment env); read-only access to `wa_data` (the archive only: the session is in `wa_session`, which this container does not mount) | Inbound from the broker on `net_whatsapp`; outbound to the sidecar on `wa_internal`; cannot reach the other plugin services |
| **whatsapp-sidecar** | Speaks the WhatsApp multi-device protocol (whatsmeow), archives every message into `messages.db`, internal API `/health`, `/status`, `/qr`, `/send`, `/media`; no policy | WhatsApp session (`session.db` in `wa_session`, which only this container mounts; the account credential, plaintext: see 4.3), `SIDECAR_TOKEN` | Inbound only from `wa_internal`; outbound to WhatsApp servers |
| **plugin-github** | Plugin API for `github`; `github_app` connection (install URL, installation recorded on `/connect/finish`) mints installation tokens restricted to the requested repos and permissions | App private key (uploaded: encrypted in `github_secrets`; or file: `private_key_path`, confined to the read-only `/run/secrets/github` bind), installation id and connect `state` nonces in `github_secrets`, App id, cached installation tokens (memory, ≤ 50 min), `PLUGIN_TOKEN_GITHUB`, `PLUGIN_SECRETS_KEY_GITHUB` | Inbound from the broker on `net_github`; outbound to `api.github.com`; cannot reach the other plugin services |
| **plugin-google** | Plugin API for `gmail`, `gcal`, `gdrive` (one `GET /manifests` returns all three; shared `aab_plugin_google/client.py`); `google_oauth` connection builds the auth URL, owns the state nonce, exchanges the code, mints per-scope-set access tokens by downscoped refresh | OAuth client id/secret, refresh token and connect `state` nonces with the `redirect_uri` the broker passed (encrypted in `google_secrets`), access tokens (memory only, per scope set), `PLUGIN_TOKEN_GOOGLE`, `PLUGIN_SECRETS_KEY_GOOGLE` | Inbound from the broker on `net_google`; outbound to Google OAuth and API endpoints; cannot reach the other plugin services |
| **aab-installer** (opt-in) | Inspect, install, upgrade and remove external plugins: clone an allowlisted source at a tag or a full commit (over https; for a private `github.com` repository, the GitHub token the broker sends with that request, given to git through `GIT_ASKPASS` for that clone only), validate the descriptor, render the overlay through a fixed template, ensure the service's `.env` secrets via `scripts/init_secrets.py`, run `docker compose` (build and start the plugin) and `docker network connect` / `disconnect` (put the running broker on the plugin's network, or take it off: it never recreates or restarts the broker), roll back on failure; one job at a time, persisted in `plugins.d/_installer/`; answer `GET /services` (each installed service's URL and `PLUGIN_TOKEN_<SERVICE>`, read from `.env` at each request) | `INSTALLER_TOKEN` (no git credential of its own: a GitHub token lives only as long as the request or job it came with); through its mounts, the Docker socket (root on the host) and the checkout including `.env` | Inbound from the broker only, on `net_installer`; outbound to allowlisted git hosts; the Docker daemon through the socket; cannot reach any plugin |
| **plugin-&lt;service&gt;** (each installed external plugin) | Plugin API for the ids its descriptor lists, from its own repository at the pinned commit, on the base image `aab-plugin-base` | `PLUGIN_TOKEN_<SERVICE>`, `PLUGIN_SECRETS_KEY_<SERVICE>`, whatever it stores in `<service>_secrets` | Inbound from the broker on `net_<service>`; cannot reach other plugins, the installer or the edge |
| **Telegram Bot API** | Delivers approval cards to the owner's phone and returns button taps | (external) | Broker calls it outbound; taps are fetched by the broker's poll loop, no inbound webhook |

---

## 3. Data flow diagrams

Conventions:

- Rectangles are processes.
- Cylinders are data stores.
- Rounded nodes outside the machine are external entities.
- Each **subgraph is a trust zone**. Every arrow that leaves a subgraph
  crosses a trust boundary.
- Numbers on arrows refer to the flow tables.

### 3.1 DFD level 0 (context)

```mermaid
flowchart LR
  OW(["Owner"])
  AG(["Agent / delegated sub-agent"])
  subgraph CFZ["Trust zone: Cloudflare"]
    CF["Cloudflare proxy + Access"]
  end
  subgraph SYS["Trust zone: our host"]
    AAB["Agent Authority Broker<br/>(broker + plugin containers + sidecar)"]
  end
  TG(["Telegram"])
  TS(["Target systems<br/>WhatsApp, GitHub, Google"])

  AG -->|"C1 request + aab_ key"| CF
  CF -->|"C2 request + X-AAB-Origin"| AAB
  AAB -->|"C3 result / 202 / error"| AG
  OW -->|"C4 console, Access SSO + session"| CF
  CF -->|"C5 admin request + Access JWT"| AAB
  AAB -->|"C6 approval card"| TG
  TG -->|"C7 card to phone"| OW
  OW -->|"C8 approve / reject tap"| TG
  TG -->|"C9 tap (polled)"| AAB
  AAB -->|"C10 API calls, short-lived tokens"| TS
  TS -->|"C11 data, results"| AAB
  OW -->|"C12 OAuth consent / App install / QR scan"| TS
  AAB -->|"C13 JWKS fetch"| CF
```

In local mode, C1/C2 and C4/C5 become direct loopback calls to
`127.0.0.1:8080`, and Cloudflare is absent.

| # | From → To | Data | Protocol / auth | Credential? |
|---|---|---|---|---|
| C1 | Agent → Cloudflare | Action call, permission request, delegation | HTTPS; `Authorization: Bearer aab_…` | Yes (agent key) |
| C2 | Cloudflare → system | Same request | HTTPS + AOP mTLS; `X-AAB-Origin` header | Yes (origin secret, agent key) |
| C3 | System → Agent | Result rows (filtered), 202 with action id, compact error | HTTPS response | No (delegated child key plaintext once, on `delegate`) |
| C4 | Owner → Cloudflare | Console, login, admin API | HTTPS; Access SSO | Yes (SSO, password at login) |
| C5 | Cloudflare → system | Same | HTTPS; `Cf-Access-Jwt-Assertion`, `aab_session` cookie or `Bearer aab_admin_…`, `X-AAB-Origin` | Yes |
| C6 | System → Telegram | Approval card text (summary, key name, resource label) | HTTPS Bot API; bot token | Yes (bot token) |
| C7 | Telegram → Owner | Card | Telegram app | No |
| C8 | Owner → Telegram | Button tap | Telegram app | No |
| C9 | Telegram → system | Callback query (chat id, user id, action id, verb) | HTTPS long poll, initiated by the broker | Yes (bot token on the request) |
| C10 | System → targets | Target API calls, WhatsApp protocol | HTTPS with downscoped access / installation tokens; WhatsApp multi-device session | Yes |
| C11 | Targets → system | Messages, threads, files, events, results | HTTPS / WhatsApp protocol | Tokens in OAuth responses |
| C12 | Owner → targets | Google consent, GitHub App install, WhatsApp QR scan with the phone; the browser is then redirected back to the broker's `/oauth/callback/{service}` (over C4/C5) carrying an authorization code that the broker relays once to the plugin | Browser / phone | Yes (authorization code; establishes the long-lived credential, held only by the plugin) |
| C13 | System → Cloudflare | Access public keys (`/cdn-cgi/access/certs`) | HTTPS | No |

### 3.2 DFD level 1

```mermaid
flowchart LR
  subgraph Z0["TZ0: Internet (untrusted)"]
    AG(["Agent / sub-agent"])
    OW(["Owner browser"])
  end
  subgraph Z1["TZ1: Cloudflare"]
    CF["Proxy + WAF + Access"]
  end
  subgraph Z2["TZ2: edge container (edge_net)"]
    ED["Caddy<br/>TLS, AOP mTLS,<br/>X-AAB-Origin check"]
  end
  subgraph Z3["TZ3: broker container (authority plane)"]
    P1["P1 Agent surface<br/>REST + MCP, aab_ auth"]
    P2["P2 Admin surface<br/>console, /auth, /v1/admin,<br/>/oauth/callback"]
    P3["P3 Policy engine<br/>evaluate + perform"]
    P4["P4 Decision record"]
    P5["P5 Capacity ledger"]
    P6["P6 Action queue<br/>+ scheduler + deliver"]
    P7["P7 Notifier<br/>Telegram"]
    P8["P8 Plugin API client<br/>registry + RemoteAdapter<br/>+ connect relay"]
    D1[("D1 identity<br/>principals, sessions,<br/>admin_tokens, app_config")]
    D2[("D2 authority<br/>api_keys, grants, plugins,<br/>hidden_resources")]
    D3[("D3 record<br/>decisions, capacity_ledger,<br/>ledger_grants, audit_log")]
    D4[("D4 actions")]
  end
  subgraph Z4["TZ4: plugin service container, one per service (enforcement plane, one network per service)"]
    P9["P9 Plugin runtime<br/>+ adapters"]
    P10["P10 Connection<br/>connect flow + credential minting"]
    D5[("D5 plugin secret store<br/>encrypted, own key;<br/>connect state nonce")]
  end
  subgraph Z5["TZ5: wa_internal, whatsapp-sidecar"]
    P11["P11 Go sidecar"]
    D6[("D6 wa_data<br/>messages.db")]
    D7[("D7 wa_session<br/>session.db")]
  end
  subgraph Z7["TZ7: aab-installer container (opt-in; net_installer; root on the host)"]
    P12["P12 Installer<br/>inspect, jobs,<br/>overlay renderer"]
    DH[("DH host: .env,<br/>plugins.d, Docker daemon")]
  end
  subgraph Z6["TZ6: third-party services"]
    TG(["Telegram Bot API"])
    GH(["Allowlisted git hosts"])
    TK(["Token and consent endpoints<br/>Google OAuth, GitHub App"])
    TA(["Target APIs<br/>GitHub, Google"])
    WS(["WhatsApp servers"])
  end

  AG -->|1| CF
  OW -->|"2, 41"| CF
  CF -->|3| ED
  ED -->|4| P1
  ED -->|5| P2
  P1 <-->|6| D2
  P2 <-->|7| D1
  P2 <-->|8| D2
  P1 -->|9| P3
  P3 <-->|10| D2
  P3 -->|11| P4
  P4 -->|12| D3
  P3 -->|13| P6
  P6 <-->|14| D4
  P6 -->|15| P7
  P7 -->|16| TG
  TG -->|17| P7
  P7 -->|18| P6
  P2 -->|19| P6
  P3 <-->|20| P5
  P5 <-->|21| D3
  P3 <-->|22| P8
  P6 -->|23| P3
  P2 -->|24| P8
  P8 <-->|25| P9
  P9 -->|26| P10
  P10 <-->|27| D5
  P10 <-->|"28, 43"| TK
  P9 <-->|29| TA
  P9 <-->|30| P11
  P11 <-->|31| WS
  P11 -->|32| D6
  P11 -->|32| D7
  P9 -->|33| D6
  P1 -->|34| AG
  OW -->|35| TK
  P2 -->|36| CF
  P2 -->|37| P4
  P8 -->|"38, 42, 44"| P9
  P2 <-->|"45, 48, 51"| P12
  P12 <-->|46| GH
  P2 -->|47| D2
  P12 -->|"49, 50"| DH
  P2 -->|39| OW
  TK -->|40| OW
```

In local mode, flows 4 and 5 go directly from the agent or owner to P1/P2
over loopback (zones TZ1 and TZ2 absent). Flow 41 takes the same path as
flow 2 (then 3 and 5). An arrow drawn with `<-->` is a request/response pair
whose return leg carries meaningful data.

**Call path and approvals (1 to 37)**

| # | From → To | Data carried | Protocol / auth | Credential? |
|---|---|---|---|---|
| 1 | Agent → Cloudflare | REST (`/v1/targets/{t}/actions/{a}`, `/v1/me`, `/v1/permissions…`, `/v1/delegations…`, `/v1/actions…`, `/skill`) or MCP (`/mcp`, stateless streamable HTTP) | HTTPS; `Bearer aab_…` | Yes: agent key |
| 2 | Owner browser → Cloudflare | Console page, `/auth/setup` (setup token, username, password), `/auth/login`, `/v1/admin/*` calls | HTTPS; Access SSO for `/admin*`, `/auth*`, `/v1/admin*` (and `/oauth*`, flow 41) | Yes: SSO session, password, setup token |
| 3 | Cloudflare → edge | Requests 1, 2 and 41 | HTTPS :443; Cloudflare client cert (AOP); `X-AAB-Origin` added by Transform Rule; `Cf-Access-Jwt-Assertion` on admin paths; `CF-Connecting-IP` | Yes: origin secret, Access JWT, plus request credentials |
| 4 | Edge → P1 | Agent requests | HTTP on `edge_net`; `X-AAB-Origin` re-checked by `OriginGuardMiddleware`; `X-Real-IP`; MCP `Host` allowlist (`MCP_ALLOWED_HOSTS` from env plus the console's extras, read at lifespan start) | Yes: origin secret, agent key |
| 5 | Edge → P2 | Owner requests, OAuth callback | HTTP on `edge_net`; `X-AAB-Origin`; Access JWT verified against the team JWKS (aud, iss, exp, optional email allowlist); `aab_session` cookie (HttpOnly, SameSite=Strict, Secure) or `Bearer aab_admin_…`; `X-Requested-With: aab-console` on state changes | Yes |
| 6 | P1 ↔ D2 | Key lookup by sha256 hash (current or previous-in-grace), parent chain, role, rate, denies; `last_used_at/ip` update | In-process SQLite | Hashes only |
| 7 | P2 ↔ D1 | Owner row (scrypt hash + salt), session rows (sha256 of cookie), admin token hashes, `setup_completed`, Telegram link | In-process SQLite | Hashes |
| 8 | P2 ↔ D2 | Key create/rotate/disable, grant create/approve/revoke, hidden resources, plugin enable/config (non-secret) | In-process SQLite; every write carries `AdminContext` | Key plaintext returned once to the owner, stored as hash |
| 9 | P1 → P3 | Auth context, target, action, params, `as_draft`, `run_at` / `delay_seconds` | In-process call (`engine.perform`, same path for REST and MCP) | No |
| 10 | P3 ↔ D2 | Grants for the key and every ancestor grant, plugin enabled/connected, hidden resources, denies | In-process SQLite | No |
| 11 | P3 → P4 | Decision (allow/draft/deny), grant chain root→leaf, `params_hash`, reason, `enforced_where`; later the outcome | In-process | No |
| 12 | P4 → D3 | Append row, `hash = HMAC-SHA256(DECISION_SIGNING_KEY, prev_hash + "\n" + canonical)` under `BEGIN IMMEDIATE` | In-process SQLite | Uses signing key (not stored in DB) |
| 13 | P3 → P6 | Draft or scheduled action: params, resource label, note, `decision_id` | In-process (`actions.queue.create`) | No |
| 14 | P6 ↔ D4 | Insert; atomic status transitions `UPDATE … WHERE status=?`; results | In-process SQLite | No |
| 15 | P6 → P7 | `notify_action` (new draft, new permission request) | In-process | No |
| 16 | P7 → Telegram | Card built from `summary_template` (resource names via the plugin's `/label`); an oversized card is replaced by a button-less pointer to the console | HTTPS Bot API; bot token (console-entered, encrypted at rest under `BROKER_SECRETS_KEY`) | Yes: bot token |
| 17 | Telegram → P7 | Callback query: chat id, user id, action/grant id, verb | HTTPS long poll (`getUpdates`) initiated by the broker; accepted only from the linked owner's chat **and** user id; kill switch | Yes: bot token on the request |
| 18 | P7 → P6 | Approve/reject with `AdminContext(via=telegram)` | In-process (admin services) | No |
| 19 | P2 → P6 | Approve/reject/cancel from the console with `AdminContext(via=session\|token)` | In-process | No |
| 20 | P3 ↔ P5 | Reserve before a write; commit or release after | In-process; in-process per-minute limiter | No |
| 21 | P5 ↔ D3 | `capacity_ledger` row + one `ledger_grants` row per grant in the chain; per-day sums | In-process SQLite | No |
| 22 | P3 ↔ P8 | `normalize` / `resolve` / `label` / `perform(action, params, CallScope)`; results or `AdapterError` 400/404/503/502 | In-process | No |
| 23 | P6 → P3 | Due or approved action re-entering evaluation/delivery (`deliver.py`, scheduler tick) | In-process | No |
| 24 | P2 → P8 | Plugin enable/config (secret fields relayed once), health check, labels for the console, and the connect relays of flows 38 to 44 | In-process | Yes: secret config fields and an authorization code, in transit only |
| 25 | P8 ↔ P9 | `GET /manifests` (every manifest the service hosts), `GET /status`, `POST /configure`, `POST /normalize`, `POST /resolve`, `POST /label` (`{kind, ids[]}` → labels), `POST /perform` (params + `CallScope`: deny/allow_only, constraints, credential requirements, `request_id`); results | HTTP on the service's own network (`net_whatsapp`, `net_github`, `net_google`); `X-Plugin-Token: PLUGIN_TOKEN_<SERVICE>`, constant-time compare | Yes: service token; `/configure` carries secret values |
| 26 | P9 → P10 | Credential requirements (`{"scopes": [...]}` or `{"repos": [...], "permissions": {...}}`) | In-process in the plugin | No |
| 27 | P10 ↔ D5 | Long-lived credential (Google OAuth client secret and refresh token, GitHub App PEM and installation id; plugin-whatsapp stores nothing today) and the connect `state` nonce with its `redirect_uri`; encrypted with `PLUGIN_SECRETS_KEY_<SERVICE>` | Local file/volume | Yes |
| 28 | P10 ↔ token endpoints | Google: refresh with `scope=<subset>` → access token (cached in memory per scope set, never logged or persisted). GitHub: RS256 App JWT (10 min) → installation token for exact repos + permissions (cached ≤ 50 min) | HTTPS | Yes: refresh token / App JWT out, access / installation token back |
| 29 | P9 ↔ target APIs | Target calls and responses; the plugin filters denied/hidden rows before returning | HTTPS; short-lived token | Yes: short-lived token |
| 30 | P9 ↔ P11 | `POST /send`, `GET /media`, `GET /status`, `GET /qr` (backs `/connect/qr.png`); results | HTTP :8081 on `wa_internal`; `X-Internal-Token: SIDECAR_TOKEN`, constant-time compare (`/health` tokenless) | Yes: sidecar token |
| 31 | P11 ↔ WhatsApp | Multi-device protocol: messages in and out, media | WhatsApp E2E protocol; linked-device session from `session.db` | Yes: session keys |
| 32 | P11 → D6, D7 | Writes `session.db` (session credential, plaintext) to D7 and archives every message into `messages.db` (D6) | Local SQLite on `wa_session` (rw, mounted by the sidecar alone) and `wa_data` (rw) | Yes: `session.db` is the account credential |
| 33 | P9 → D6 | Reads `messages.db` for list/read/search with the visibility clause in SQL | Local SQLite on `wa_data` mounted read-only | No (the session is in `wa_session`, which plugin-whatsapp does not mount) |
| 34 | P1 → Agent | Result, 202 `pending_approval` / `scheduled`, or `{"error","code","hint"}` with 400/401/403/404/429/502/503. REST binary results are attachments with `X-Content-Type-Options: nosniff`; a long-poll bootstrap (no `cursor`) is answered at once whatever `?wait=` says | HTTP(S) response | Only `delegate` returns a child key plaintext, once |
| 35 | Owner browser → consent endpoints | Google consent screen (scopes = union of the enabled Google manifests' `target_permissions`) or GitHub App install page, opened from the URL of flow 39 | HTTPS in the browser | Yes: owner's Google / GitHub login |
| 36 | P2 → Cloudflare | JWKS fetch for Access JWT verification (cached, refreshed on unknown `kid`) | HTTPS | No |
| 37 | P2 → P4 | Admin decisions that belong in the record (approvals as `actor_principal`/`actor_via` on outcome rows); `GET /v1/admin/decisions/verify` re-computes the chain | In-process | No |

**Connect flows (38 to 44)**

The connect flow for every connection kind lives in the plugin service. The
broker only passes data through. The sequence for Google is 24 → 38 → 39 →
35 → 40 → 41 → 24 → 42 → 43 → 27. GitHub is the same, with an install page
and `installation_id`. WhatsApp starts with 24 → 38 (`{kind: qr}`). Then the
console polls `GET /v1/admin/plugins/whatsapp/connect/qr.png`
(24 → 38 → 30 → 39) while the owner scans with the phone (C12). Last comes
24 → 42 (`/connect/finish`, a no-op that refreshes health).

| # | From → To | Data carried | Protocol / auth | Credential? |
|---|---|---|---|---|
| 38 | P8 → P9 | `POST /connect/start {enabled_plugins, redirect_uri?}` → `{kind: oauth\|install\|qr\|none, url?, state?}`. `redirect_uri` is the broker's own `/oauth/callback/{service}`, computed by the broker. For Google the plugin builds the auth URL from its own client id, that `redirect_uri` and the union of its enabled manifests' `target_permissions`, and generates and stores the `state` nonce with the `redirect_uri` (27); for GitHub it returns the App install URL; for WhatsApp `{kind: qr}` (`none` once paired). Also `GET /connect/qr.png` (WhatsApp, proxied from the sidecar via 30) | HTTP on the service's own network; `X-Plugin-Token` | No (the URL carries only the public client id, the redirect URI and `state`) |
| 39 | P2 → Owner browser | The auth / install URL to open, or the QR PNG served at `/v1/admin/plugins/whatsapp/connect/qr.png` | HTTPS response to an authenticated admin request | QR PNG is a pairing secret while valid |
| 40 | Consent endpoint → Owner browser | Redirect to the broker's `GET /oauth/callback/{service}` with `code` and `state` (GitHub: `installation_id`) | HTTPS 302 | Yes: authorization code |
| 41 | Owner browser → P2 | `GET /oauth/callback/{service}` renders the data-free callback page (no owner credential: the cross-site redirect carries no SameSite=Strict cookie), which strips the query and POSTs `code` + `state` same-origin to the admin-guarded `connect/finish`; travels via Cloudflare and the edge like flow 2 | HTTPS; Access JWT in public mode on both; the POST carries the owner session cookie and the CSRF header | Yes: authorization code (never logged: the access line has the path only, `docs/logging.md`) |
| 42 | P8 → P9 | `POST /connect/finish {code?, state?, installation_id?}`: the code is relayed once and not stored by the broker (WhatsApp: no body, a no-op that refreshes health) | HTTP on the service's own network; `X-Plugin-Token` | Yes: authorization code |
| 43 | P10 ↔ token endpoints | Plugin checks `state`, exchanges the code with its own client id + secret for a refresh token (Google), or verifies the installation with an App JWT (GitHub); result stored encrypted in D5 (27) | HTTPS | Yes: client secret / App JWT out, refresh token back |
| 44 | P8 → P9 | `POST /disconnect`: the plugin wipes its stored credential (WhatsApp: 409, unlink the device on the phone) | HTTP on the service's own network; `X-Plugin-Token` | No |

**Install flows (45 to 51, opt-in installer)**

These flows install external plugins (`docs/plugin-packaging.md`). The
installer (P12) is a separate container, and it is root on the machine. The
broker decides authority (the pin, 47) before the installer does anything
lasting. The sequence for an install is
2 → 5 → 45 → 46 → (review) → 45 → 46 → 47 → 48 → 49 → 50. The broker
keeps running throughout: flow 50 connects it to the plugin's network. The
console and the broker itself poll the job over 51. When the job ends, the
broker reads the installed services over 51 (`GET /services`). It discovers
the plugin over 25 and checks it against the pin. A later deploy recreates
the broker once with the same network and values from the overlay.

| # | From → To | Data carried | Protocol / auth | Credential? |
|---|---|---|---|---|
| 45 | P2 → P12 | `POST /inspect {source, ref, git_token?}` → descriptor, manifest texts, resolved commit, the install record if any; the broker validates every manifest and builds the review | HTTP on `net_installer`; `X-Installer-Token: INSTALLER_TOKEN`, constant-time compare; `GET /health` the only tokenless route | Yes: installer token; the owner's GitHub token in the body when one is stored (decrypted from `plugin_secrets` for this request; never logged or returned) |
| 46 | P12 → git host | `git clone --depth 1 --branch <tag>` or fetch by commit, into a temporary directory (inspect) or `plugins.d/<service>/src` (a job); no system or global git config, `GIT_ALLOW_PROTOCOL=https`, `core.symlinks=false`, hooks off | HTTPS; anonymous, or the broker's GitHub token (sent with that inspect, install or upgrade request) answered by the `GIT_ASKPASS` script to `github.com`'s prompts only | Yes when the owner stored one: read-only git token, for this clone only (never in a URL, an argument, a file or a log) |
| 47 | P2 → D2 | The owner's pin of every manifest (`plugin_pins`: manifest text, version, source, ref, commit, who, when), audited `plugin.pin`; restored exactly if the installer refuses 48 | In-process SQLite; `AdminContext` | No |
| 48 | P2 → P12 | `POST /install {source, ref, commit, git_token?}`, `POST /upgrade {service, source, ref, commit, git_token?}`, `POST /remove {service, purge}` → 202 job (audited `plugin.install` / `.upgrade` / `.remove`); after a remove, the broker unpins what the service hosted | HTTP on `net_installer`; `X-Installer-Token` | Yes: installer token; the GitHub token in install and upgrade bodies when one is stored (the job keeps it in memory for its one clone, masked in its lines, never saved) |
| 49 | P12 → host `.env` | The service's `PLUGIN_TOKEN_<SERVICE>` / `PLUGIN_SECRETS_KEY_<SERVICE>` generated by `scripts/init_secrets.py --rotate` (never read back), retired or purged on remove | Local file through the checkout mount (0600, owner kept) | Yes: generated secrets (written, never returned) |
| 50 | P12 → Docker daemon | `docker compose --project-directory <AAB_HOME> <the file set> up -d --build plugin-<service>`, `ps -q broker`, `rm -s -f`; `docker network connect` / `disconnect aab_net_<service> <broker container>` (install / remove: the running broker joins or leaves, never recreated), `network rm`, `volume rm` (purge) | The Docker socket (root on the host) | No (the overlay maps `.env` values by name) |
| 51 | P2 → P12 | `GET /jobs/{id}` (state, log lines: redacted, 64-hex runs, `INSTALLER_TOKEN`, every plugin token in `.env` and the job's own GitHub token masked; the broker relays only the job's known fields), polled by the console every 2 s and by the broker's sync loop for the jobs it asked for; `GET /installed`; `GET /services` → each installed service's `{service, url, token}` (at boot, when a job ends, and every 60 s; validated strictly, kept in the registry's memory, never logged or written) | HTTP on `net_installer`; `X-Installer-Token` | Yes on `GET /services`: each installed service's `PLUGIN_TOKEN_<SERVICE>` (never its secrets key) |

---

## 4. Security notes per trust boundary

### 4.1 What authenticates each crossing

| Boundary | Flows | What authenticates the crossing | Notes |
|---|---|---|---|
| **Internet ↔ Cloudflare** | 1, 2, 41 | Agents: nothing at this layer beyond TLS (their `aab_` key is checked by the broker). Owner: Cloudflare Access SSO on `/admin*`, `/auth*`, `/v1/admin*`, `/oauth*`. WAF rate limiting (recommended). | Agent paths are deliberately not behind Access; agents authenticate with their key. |
| **Cloudflare ↔ edge** | 3 | Security group allows 443 only from Cloudflare IP ranges; Authenticated Origin Pulls (Cloudflare client cert); `X-AAB-Origin` origin secret. | The default origin-pull CA is Cloudflare's global CA, so AOP proves "some Cloudflare account". The origin secret is the real per-deployment lock until a custom AOP cert is pinned. |
| **Edge ↔ broker** | 4, 5 | Network `edge_net` (edge and broker only). `OriginGuardMiddleware` re-checks `X-AAB-Origin` (constant-time; only `/health` and `/v1/health` exempt) and only then trusts `CF-Connecting-IP`. Admin: Access JWT verified at the origin **and** an owner credential (session cookie or `aab_admin_` token), plus `X-Requested-With: aab-console` on state changes. Agents: `Bearer aab_…`. `/mcp` checks `Host` against `MCP_ALLOWED_HOSTS` plus the console's `mcp_allowed_hosts_extra` (additive: the console can add hosts, never remove the file's), read when the app lifespan starts. | The edge has no route to any plugin, so a compromised edge cannot call `/perform`. Boot fails closed if public mode is on without Cloudflare Access (unless `ALLOW_INSECURE_ADMIN=true`). OpenAPI/docs UIs are off in public mode. Local mode: the loopback bind is the boundary and the origin secret is off. |
| **Agent key → authority** | 6, 9, 10 | `aab_` key hashed with sha256; previous hash accepted during the rotation grace (`key_rotation_grace_seconds`, 24h); key and every ancestor must be enabled and unexpired; per-key `rate_per_min`. | A key holds no authority itself; everything comes from grants re-walked per call. |
| **Owner identity** | 7, 8, 19 | Password (`hashlib.scrypt`, per-user salt), 5 failed logins/min/IP; sessions 12h idle / 7d absolute; admin tokens `aab_admin_<48hex>` stored hashed, revocable, optional expiry; one-time `SETUP_TOKEN` inert after setup. | Every human action carries an `AdminContext` and is recorded under the owner's username. |
| **Broker ↔ plugin services** | 24, 25, 38, 42, 44 | One shared token per service, `PLUGIN_TOKEN_<SERVICE>` (`WHATSAPP`, `GITHUB`, `GOOGLE`), in `X-Plugin-Token`, constant-time compared; one network per service (`net_whatsapp`, `net_github`, `net_google`: the broker and that service only, so no plugin service can reach another); every manifest from `GET /manifests` pinned (id + version) against the broker's vendored copy. | Plain HTTP inside the host. The plugin trusts the broker's `CallScope`: a compromised broker can request any credential the connection can mint, but cannot read stored credentials. The authorization code crosses here once (42). |
| **Broker ↔ installer (installer == host root)** | 45, 48, 51 (and 46, 49, 50 behind it) | Bounded by network, token, allowlist, rendered overlays, pinned commits, askpass credentials: `net_installer` holds the broker and the installer only; every call but `/health` needs `INSTALLER_TOKEN` (constant-time; an empty token refuses to boot); `INSTALLER_ALLOWED_SOURCES` is env-only and fail closed; refs are a release tag or a full commit, and a job installs only the commit the owner reviewed; overlays are rendered from a strictly validated descriptor through a fixed template (one network, no ports, no binds, own token and key only); the git credential is the broker's (a console setting, encrypted under `BROKER_SECRETS_KEY`), sent per request, and reaches git only through `GIT_ASKPASS`, for `github.com` only; the installer stores none. `GET /services` hands the broker each installed service's URL and `PLUGIN_TOKEN_<SERVICE>` (never a secrets key) over the same token-guarded channel; the broker validates the whole answer (no name the stack itself uses, the URL exactly `http://plugin-<service>:8090`, a well-formed token), keeps the previous set on anything else, and holds the tokens in memory only. | The installer holds the Docker socket, so whoever controls it is root on the host: it is opt-in, reachable from nothing but the broker, and a hijacked console session can trigger only reviewed installs from allowlisted sources, never widen the allowlist or supply compose YAML. It touches the broker container with two commands only, `docker network connect` and `disconnect` for an installed plugin's network (found through `compose ps`, so never another project's container); it already holds the socket, so this adds no power. Its list can name only installed plugin services: it can never redirect an in-tree service or point the broker at another host. It decides no authority: the broker pins before any job, and a plugin is served only if it offers exactly the pinned manifest. Nothing from a plugin repository runs on the host; its Dockerfile runs inside `docker build`. |
| **Plugin ↔ sidecar** | 30, 33 | `SIDECAR_TOKEN` in `X-Internal-Token`, constant-time compared; network `wa_internal` which the broker is not on. | The sidecar has no policy at all; everything it is asked to do it does. The shared `wa_data` volume is a second crossing: sidecar rw, plugin-whatsapp ro, nobody else; it holds only the archive. The plaintext `session.db` is in `wa_session`, which only the sidecar mounts (4.3). |
| **Broker ↔ Telegram** | 16, 17 | Outbound HTTPS with the bot token; taps accepted only when both the chat id and the user id match the linked owner; kill switch. | No inbound webhook port. A Telegram tap is an owner action (`via=telegram`); agents have no path to it. Card content (summary, resource labels) leaves the host. |
| **Owner ↔ consent, callback relay** | 35, 39 to 43 | The plugin generates, stores and checks the `state` nonce (10-minute TTL, single use); the callback page needs no owner credential (Access in public mode) and holds no data, and the POST it makes is admin-guarded (session and CSRF header, plus Access in public mode); the relay to `/connect/finish` uses the service token. The redirect URI is the broker's own callback, computed by the broker (public mode: from `SITE_DOMAIN` in its env, not from console config) and handed to the plugin in `/connect/start`. | The broker never sees a client secret or a refresh token; an intercepted code is useless without the plugin's client secret. The long-lived credential is created and kept only inside the plugin service. |
| **Plugin ↔ target APIs** | 28, 29, 31, 43 | Google: short-lived access tokens minted by downscoped refresh. GitHub: installation tokens restricted to `repositories` + `permissions`. WhatsApp: the linked-device session. | See 4.2 for what each token actually restricts. |

### 4.2 Enforced by target vs proxy-only

Every decision records `enforced_where` per bounding dimension:

- `target` means that the restriction is inside the credential that the
  target sees. Thus a bug in our narrowing code cannot exceed it.
- `proxy` means that only our code (broker engine or plugin adapter)
  enforces it. The target itself does not stop the wider call.

The broker always enforces `mode`, budgets, hidden resources and per-key
denies. `get_my_access` reports the same per-dimension answer to the agent.

The broker claims `target` only when two conditions are true:

- The manifest permits it.
- The plugin's last `/status` (stored as `last_health`) reports
  `enforcement: target` or `mixed`.

A record with no `enforcement`, or an unknown one, counts as `proxy`. A
failed health refresh keeps the last value that the plugin reported. Thus an
outage can never turn a PAT-backed GitHub plugin into a target-enforced one
(`policy.connection_is_proxy_only`). Only a plugin that has never reported
anything keeps the manifest's claim. The console's enforcement badge
follows the same rule.

| Plugin | Target-enforced | Proxy-only | Caveats |
|---|---|---|---|
| **WhatsApp** (`sidecar_qr`) | Nothing | Everything: `chat` list, `mode`, budget, hidden chats (SQL visibility clause in the archive and post-filtering) | The linked-device session is the whole account; WhatsApp has no scoped credentials. The sidecar holds no policy. |
| **GitHub** (`github_app`) | `repo` list (installation token `repositories`), `permissions` derived from the allowed actions' `target_permissions` (e.g. `contents:read`) | `branch` pattern, `mode`, budget, hidden repos | An installation token can never exceed the App's installed permission set, and it is only as narrow as the requested tuple; the console shows the installed set. **PAT fallback** makes every dimension `proxy`. Hidden-repo filtering of lists can leave page-size gaps. |
| **Google** (`google_oauth`: gmail, gcal, gdrive) | Read-only vs read-write, via scopes (`gmail.readonly` vs `gmail.modify`+`gmail.send`; `calendar.readonly` vs `calendar.events`; `drive.readonly` vs `drive`) | Gmail `label` (query terms and post-filter), `contact`, `domain`, `date_window_days`, `attachments`, `bcc`, `mark_read`; Calendar `calendar`, `attendee`, `visibility`, `time_window_days`, `private_events`, `others_events`, hidden events; Drive `folder` subtree (parent-chain walk by id), `mime`, `shared_drives`, `file_content`, `max_download_mb`, `external_sharing`; `mode`, budget, hidden | Downscoped refresh (`scope=<subset>` on the refresh request) must be verified once against the real endpoint; if a token comes back with full scopes the manifest flips to `proxy`. Credential Access Boundaries exist only for Cloud Storage, so nothing finer than scopes is target-enforced. The refresh token is persisted in the plugin (see 4.3). `drive.file` is not a substitute for folder narrowing. Enabling a further Google plugin later needs a reconnect for the new scopes. |

### 4.3 Other properties worth modelling

- **Deviation from brief §6.1 ("exchange at call time, hold nothing
  persistent").** With no identity provider, there is no token-exchange
  source of truth. Thus the system must hold long-lived credentials: the
  Google refresh token and the GitHub App private key. Only the owning plugin
  service holds them, encrypted under that service's own
  `PLUGIN_SECRETS_KEY_<SERVICE>`. The broker holds none. The short-lived
  tokens that the plugin mints from them live only in plugin memory. This is
  the personal-scale substitute for "hold nothing". It is the first thing
  that an IdP integration will remove.
- **The one credential not encrypted at rest: WhatsApp `session.db`.** It is
  whatsmeow's own SQLite store, in plaintext. A second encryption layer over
  a third-party store is not worth the cost. Only the sidecar can reach the
  file. It lives in its own volume, `wa_session`, which only
  `whatsapp-sidecar` mounts (read-write, at `/session`, a 0700 directory).
  That volume is apart from the archive in `wa_data` that `plugin-whatsapp`
  reads. No other container, the broker included, can open the file, because
  it is not in their filesystems. Every container runs as a non-root user.
  Backups of `wa_session` carry the live account session.
- **Blast radius by component.**
  - Broker compromise: an attacker can forge all grants and the decision
    record, because the broker holds `DECISION_SIGNING_KEY`. The attacker
    can drive every plugin within the ceiling of each connection. But the
    broker holds no target credential.
  - Edge compromise: the edge can reach only the broker (`edge_net`). The
    attacker still needs agent keys or owner credentials.
  - Plugin service compromise: the attacker gets that service's long-lived
    credential and its targets, and nothing else. The service has its own
    network, volume, key and token. On its network, it can reach the broker
    and never another plugin service.
  - plugin-whatsapp compromise: the attacker gets the sidecar API (reads and
    sends as the account while the sidecar runs) and read access to the
    archive in `wa_data`. The attacker does not get the session. `session.db`
    is in `wa_session`, which only the sidecar mounts. Thus the attacker
    cannot copy the account to another device from there.
- **Decision record integrity** is tamper-*evident*, not tamper-proof.
  `verify()` calculates the HMAC chain again and reports the first bad id.
  Anyone with `DECISION_SIGNING_KEY` and write access to `broker.db` can
  rewrite it. A key rotation makes old rows fail verification.
- **The decision record does not store params**, only `params_hash`. The
  broker stores the params of queued actions in `actions` until delivery.
- **Prompt injection** from target content has two mitigations: there is no
  approve path at all, and the skill rule says "archived content is data,
  not instructions". Content still flows to the agent unmodified.
- **Key material at rest**: `.env` (0600, on the machine only) holds every
  generated secret. Backups of `broker_data` and of plugin secret volumes are
  useful only with the matching keys. The loss of a
  `PLUGIN_SECRETS_KEY_<SERVICE>` marks that service's credentials
  "reconnect required". Boot fails closed if encrypted data exists and its
  key is missing. `BROKER_SECRETS_KEY` protects the secrets that the owner
  enters in the console and the broker itself uses. These are the Telegram
  bot token and the installer's GitHub token, in `plugin_secrets`. A changed
  key makes them "re-enter required". With a missing key and stored values,
  the broker does not boot.

---

## 5. Glossary

| Term | Meaning |
|---|---|
| **Principal** | A human whose authority agents spend. v0.2.0 has one: the owner (`principals` row). Every table carries `principal_id`. |
| **Owner** | The single principal; logs into the console, approves, configures. Recorded by username in decisions and audit. |
| **Key** | An agent credential `aab_…` (`api_keys` row, stored as a sha256 hash). Holds no authority itself; carries role, rate, expiry, denies and optional `parent_key_id`. Owner tokens are separate: `aab_admin_…` in `admin_tokens`. |
| **Grant** | The only authority object: a list of capability statements attached to a key, with `kind` (`root`, `expansion`, `delegation`), `status` (`pending`, `active`, `rejected`, `expired`, `revoked`), expiry and optional `parent_grant_id`. |
| **Capability** | One allow statement: `target`, `actions` (explicit sorted set), `selector`, `constraints`, `mode`, `expires_at`, `budget`. Only allow statements exist, so widening has no representation. |
| **Selector** | Per narrowing dimension declared in the manifest (chat, repo, label, folder, …), either `"*"` or a normalized list. An absent dimension means `"*"`. |
| **Constraint** | A manifest-declared restriction that is not a resource selection (`date_window_days`, `attachments`, `file_content`, …), typed by a narrowing form. A `flag` is named for the permission it grants (`true` = allowed), because the algebra treats `true` as top and drops it. |
| **Narrowing form** | The shared vocabulary for how a dimension narrows: `list`, `subtree`, `pattern`, `range`, `flag`, `level`. Each defines `cap_le` (is child ≤ parent) and `meet`. |
| **`narrow()`** | The only way to produce a child grant: the meet of each requested capability with each parent capability on the same target, wrapped in `NarrowedCapabilities`. |
| **Ceiling (P)** | The owner's live authority: every action of every enabled and connected plugin, unrestricted. Virtual, never stored. |
| **Role (R)** | The key's ceiling (console: "Ceiling (role)"): never grants, caps every capability below it. `read-only`, `read-draft`, `read-act`, `full` (the owner default: the capabilities decide). |
| **Effective** | `P ∩ G ∩ R` minus denies. The broker calculates it on every call and walks every grant chain again. |
| **Delegation** | An agent minting a child key whose grants are `narrow()` of its own, with role, rate and expiry no wider and denies no smaller; depth ≤ 3; no human needed. Revoking a key kills its whole subtree. |
| **Scope expansion** | `request_permission`: an agent asks for more; clipped to what its parent (or the ceiling) could give; a human approves. |
| **Hidden resource** | An owner-level deny (`hidden_resources`) by target, kind and id. Answers 404 and is filtered from every list for every key. |
| **Deny set** | Hidden resources plus the key's `api_keys.denies`; for a delegated key the union along the chain. Lives outside the lattice and is subtracted after it; only grows along a chain. |
| **Action** | (1) A manifest operation, canonical id `target.action` (`whatsapp.send_message`; MCP tool `whatsapp_send_message`) with a side effect `read`, `write` or `destructive`. (2) A queued row in `actions`: a draft awaiting a human or a scheduled call. |
| **Draft** | An action queued for a human decision instead of executed, because of the capability's mode, the role, or the agent's `as_draft`. |
| **Decision record** | The `decisions` table: append-only, one `decision` row before any side effect and one `outcome` row after, each HMAC-chained to the previous row with `DECISION_SIGNING_KEY`. Verified by `GET /v1/admin/decisions/verify` or `aab decisions verify`. |
| **`enforced_where`** | Per bounding dimension, whether the target (`target`) or only our code (`proxy`) enforces it. |
| **Ledger** | `capacity_ledger` + `ledger_grants`: budget accounting; one charge per grant in the chain, so a parent's `per_day` bounds its whole subtree. Distinct from `audit_log`, which is ops-only and never load-bearing. |
| **Manifest** | A plugin's declarative YAML (`targets/<id>/manifest.yaml`): connection kind, config schema, resources, narrowings, constraints, actions with `target_permissions` and `summary_template`, skill text. The broker makes the tools, routes, cards, console editor and skill doc from it. |
| **Adapter** | The code that does the actions on a target for one plugin (`configure`, `status`, `normalize`, `resolve`, `label`, `perform`). Broker side: `InProcessAdapter` (tests, `echo`) or `RemoteAdapter` (HTTP to a plugin service). |
| **Connection** | The part of a plugin that runs the connect flow, owns the long-lived credential and mints short-lived ones: `sidecar_qr`, `github_app`, `google_oauth`. Lives in the plugin service; adapters never see long-lived secrets and the broker never sees them at all. |
| **CallScope** | What the broker sends with each `perform`: visibility (deny set, optional `allow_only`), constraints, credential requirements, `request_id`. |
| **Plugin service** | One container with one or more plugin ids (`plugin-google` holds `gmail`, `gcal`, `gdrive`). Env, token and secrets key are per service (`PLUGIN_URL_/PLUGIN_TOKEN_/PLUGIN_SECRETS_KEY_<SERVICE>` in `.env` and on the broker; inside the plugin container compose maps them to the generic `PLUGIN_TOKEN`, `PLUGIN_SECRETS_KEY`, `PLUGIN_SECRETS_DIR`); the registry maps plugin id → service. |
| **Plugin runtime** | The `aab-plugin-runtime` package that runs adapters in a plugin service and serves the internal plugin API (`GET /manifests`, `GET /status`, `POST /configure`, `/normalize`, `/resolve`, `/label`, `/perform`, `/connect/start`, `GET /connect/qr.png`, `POST /connect/finish`, `/disconnect`) behind `X-Plugin-Token`, mapping adapter exceptions to 404/503/502. |
| **Sidecar** | A native process a plugin needs to reach a target that has no API; today the Go WhatsApp sidecar (whatsmeow). It holds the target session and no policy, and is reachable only from its plugin service. |
| **AdminContext** | The identity of a human action: principal, username, surface (`session`, `token`, `telegram`), session or token id. |
| **Public mode** | Deployment with `ORIGIN_SECRET` set: Cloudflare + edge in front, origin lockdown on, Cloudflare Access required on the admin plane. |

---

## 6. Current build status

This is the state of `dev` at `e0c24f0` (2026-09-24). Every phase is on
`dev`. Before the 0.2.0 tag, only verification remains (the table after this
one).

| Phase | On `dev` | Where |
|---|---|---|
| 0. Skeleton | Settings with the `validate_exposure` boot interlock; the full schema with additive `_MIGRATIONS`; origin guard, Access JWT verification, audit, compact errors; `GET /health`; `init_secrets.py`; compose with the public overlay and the Caddy edge; EC2 deploy scripts; CI; the Go sidecar copied from WA_GW; non-root images | `broker/broker/config.py`, `db.py`, `origin.py`, `cf_access.py`, `scripts/`, `deploy/`, `edge/`, `sidecars/whatsapp/` |
| 1. Identity | scrypt owner and one-time `SETUP_TOKEN` setup; session cookies with idle and absolute expiry; `aab_admin_` tokens; `require_admin` → `AdminContext`; CSRF header on cookie writes; `aab` CLI | `identity/`, `deps.py`, `routers/auth.py`, `routers/admin.py`, `docs/auth.md` |
| 2. Authority core | Manifest validation and strict params models; capabilities over the six narrowing forms; `narrow()` as the only producer of `NarrowedCapabilities`; roles, ceiling, denies, live `effective`; the grants store; `aab_` keys with the parent chain; Hypothesis property suite | `plugins/manifest.py`, `authority/`, `auth.py`, `docs/grant-algebra.md`, `docs/manifest-schema.md` |
| 3. Infrastructure | Five services (plus the `edge` overlay) on `edge_net` / `broker_net` / `wa_internal`; the per-service env split; one secret volume per plugin service; 11 generated secrets | `docker-compose.yml`, `docker-compose.public.yml`, `docs/deployment.md` |
| 3. Engine core | The plugin runtime; `InProcessAdapter` and `RemoteAdapter`; the registry with manifest pinning; `policy.evaluate`; the hash-chained decision record with `verify`; the capacity ledger; `engine.perform`; the action queue, delivery with re-check and the scheduler; hidden resources; agent REST (targets, long poll, resolve, actions, `/v1/me`, permissions, per-key OpenAPI); admin REST (plugins with the connect relay and the OAuth callback page, keys and grants, actions, hidden resources, decisions) | `plugin-runtime/`, `broker/broker/`, `docs/plugin-api.md` |
| 3. MCP | Stateless streamable HTTP, per-request tool list, the `BrokerApp` splitter, generic tools, REST/MCP parity tests | `mcp_server.py`, `mcp_tools.py`, `mcp_generic.py`, `docs/mcp.md` |
| 3. Telegram and settings | Broker-side secret store (`crypto.py`, `plugin_secrets` slot `broker`); Telegram cards, linking and taps, with the inbound half unreachable from agent surfaces; console-managed operator settings (`runtime_settings`, `/v1/admin/settings`); third-party credentials removed from `.env` and compose ("files hold less") | `notify/`, `crypto.py`, `runtime_settings.py`, `docs/configuration.md` |
| 4. WhatsApp plugin service | `plugin-whatsapp`: JID normalization, the read-only archive, the sidecar client, the `sidecar_qr` connection, the adapter; then the long-poll bootstrap, a recorded 400 for unencodable params, `nosniff` attachment headers and read-only chats | `plugins/whatsapp/`, `docs/plugins/whatsapp.md` |
| 4. Owner console | Pass 1: setup and login, Overview, Requests, Scheduled, Decisions, Plugins (config forms from `config_schema`, write-only secret fields, connection panels), Agent keys with the capability editor, Hidden resources, Account; the nonce CSP and no HTML-string sinks. Pass 2: Channels (Telegram, write-only bot token), Settings (every operator setting, the env-only list), Delegations (the key tree), the shared Google account card, the GitHub connect panel, and the enforcement badge that fails closed to proxy | `templates/console.html`, `routers/console.py`, `plugins/manifest_view.py`, `docs/console.md` |
| 5. Delegation and skill doc | `delegate`, `list_my_delegations`, `revoke_delegation` over REST and MCP; `GET /v1/admin/keys/tree`; delegation limits as operator settings; the generated skill doc (`GET /skill`, `GET /v1/me/skill`, `broker://skill`, `aab skill build`) with the CI drift job; agent requests without a mode ask for draft | `services/delegation.py`, `routers/delegations.py`, `mcp_generic.py`, `skill/`, `integrations/`, `docs/delegation.md` |
| 6. GitHub plugin service | `plugin-github`: the `github_app` connection (install, installation verified with an App JWT), per-call installation tokens for exactly the addressed repositories and the action's permissions, the PAT fallback reported as proxy, `private_key_path` confined to `/run/secrets/github`; `enforced_where` fails closed to proxy when the plugin's status does not say `target` or `mixed` (section 4.2) | `plugins/github/`, `policy.py`, `plugins/settings.py`, `docs/plugins/github.md` |
| 7. Google plugin service | `plugin-google`: `gmail`, `gcal` and `gdrive` over one `google_oauth` connection, per-scope-set access tokens by downscoped refresh, every narrowing and constraint in the plugin; shared config fields and the broker-computed `redirect_uri`; the OAuth callback page served without the owner credential | `plugins/google/`, `routers/oauth.py`, `docs/plugins/google.md`, `docs/platform-thesis.md` |
| 8. Simulation and release docs | Approval-volume simulation (standing grants vs per-action approval), `aab simulate`; the README, CHANGELOG and operator docs for 0.2.0 | `broker/tests/simulation/`, `docs/approval-volume.md`, `README.md`, `CHANGELOG.md`, `deploy/DEPLOY.md` |

Unreleased (0.3.0, external plugins):

- Pins in the database, with offers awaiting review (`plugins/pins.py`, the
  registry).
- The opt-in installer (`installer/`, `docker-compose.installer.yml`,
  `scripts/compose-files.sh`).
- The install API and the console's + Add plugin
  (`services/plugin_install.py`, `routers/admin_install.py`).
- The plugin base image and the release workflow (`plugins/base/`,
  `.github/workflows/release.yml`).
- `docs/plugin-packaging.md`.

The tests for these parts run without Docker. The acceptance test in
`docs/deployment.md` is still to run. It builds the images, does a real
install through the socket under a polling console. Since then, no install,
upgrade or remove restarts the broker (`docs/plugin-packaging.md`).

What remained before the 0.2.0 tag:

| Item | Target | Status |
|---|---|---|
| No CI job builds an image (the `compose` job runs `docker compose config` only) | `docker compose build` and a local run verified (`docs/deployment.md` > Verify after `docker compose up`) | Verified after the tag: all five images build and the checklist passes; CI still builds no image |
| `plugin-whatsapp` reads the sidecar's WAL-mode `messages.db` from a read-only mount (`mode=ro`, `query_only`, through the sidecar's `-shm` file); no test covers that across two containers | Verified in a real compose run, including the 503 while the sidecar is stopped | Verified after the tag with a stand-in writer (no paired phone). The 503 needed a sidecar fix: it never closed the archive on SIGTERM |
| Google downscoped refresh (`scope=<subset>` on the refresh request) is exercised only against the tests' fake token endpoint | Verified once against Google's real endpoint; if Google ignores the subset, the manifests' `scopes` narrowing moves to `proxy` (section 4.2) | Unverified |
| An intermittent SQLite failure seen in test runs (the "SQLite flake") | Fixed, with a regression test | Fixed (CHANGELOG 0.2.0) |

---

## 7. Open points

Earlier versions of this document listed these open points. Each one now
has an answer:

- The local-mode OAuth redirect. The broker calculates the redirect URI and
  passes it in `/connect/start` (section 1.7).
- Where the Telegram bot token lives. The owner enters it in the console.
  The broker stores it encrypted in `plugin_secrets` under
  `BROKER_SECRETS_KEY` (`docs/configuration.md`).
- The GitHub App Setup URL in local mode. Register the loopback callback on
  the App. Or finish in the console's GitHub panel with the installation id
  and the issued `state` (section 1.7).
- The budget that a permission request adds. The console now shows it.

These points are still open:

1. **Budgets add up across approved grants** (`docs/approval-volume.md`).
   Each approved expansion grant carries its own `per_day`. Thus a key's
   daily write capacity grows with every approval. The console's permission
   requests say so ("adds N/day"), and the Telegram card shows the budget
   too. But nothing caps the total. A key-level daily ceiling is the
   remaining option.
2. **MCP `Host` extras apply at the next start.** The broker stores and
   shows a console edit of `mcp_allowed_hosts_extra` at once. But the MCP
   transport reads the `Host` allowlist when the app lifespan starts. Thus
   the change needs a broker restart. `docs/configuration.md` documents
   this, and the Settings view says so. If the broker rebuilds the transport
   security when the setting changes, the exception goes away.
3. **No owner-side atomic revoke for delegations.** The console's
   Delegations view emulates `revoke_delegation` in two steps:
   1. It disables the key, which stops it and its whole subtree at once.
   2. It revokes the active and pending grants of the key one by one. It
      skips any grant that a different path already decided (409).

   An owner-side admin route that does both in one transaction can replace
   that emulation.
