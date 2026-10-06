# Agent Authority Broker

Let AI agents work in your WhatsApp, GitHub, Gmail, Calendar and Drive
without giving them any of your credentials.

An agent holds one thing: an `aab_` key, which works against this broker and
nothing else. The broker holds no target credential either. Each target runs
behind its own plugin container, which keeps that target's credential
encrypted in its own volume and, where the target supports it, mints a
short-lived, narrowed token for each call.

On every call, the broker works out what the key can do right now:

```
effective = P(owner) ∩ G(grant chain) ∩ R(role)   minus hidden and denied resources
```

`P` is what you have connected, `G` is the key's grants walked up to the root,
and `R` is the key's role, a ceiling that caps the grants below it. The broker
then lets the call through, turns it into a draft for you to approve (in the
console or on Telegram), or rejects it. Every decision goes into a
hash-chained, signed record before anything runs.

- **Grants only narrow.** An agent can ask for more, and you approve. It can
  hand a sub-agent a narrower child key without asking, because nothing widens.
- **Standing grants, few interrupts.** You approve a bounded capability once
  ("post in these rooms, up to 1000 writes a day"), not every action.
- **Hidden means missing.** A chat, repository, label or folder you hide
  answers 404 to every agent and is absent from every list.
- **Approval is a human act.** Agents use REST or MCP; neither has an
  approve call.

## How it fits together

```
          agents (REST, MCP)                 you (console, Telegram)
                 |                                     |
                 v                                     v
     +---------------------------------------------------------+
     |                         broker                          |
     |   keys, grants, policy, decision record, approvals      |
     +---------------------------------------------------------+
        | net_whatsapp        | net_github        | net_google
        v                     v                   v
  +---------------+    +---------------+    +------------------+
  | plugin-whatsapp|   | plugin-github |    | plugin-google    |
  | + sidecar     |    |               |    | gmail gcal gdrive|
  +---------------+    +---------------+    +------------------+
```

Each plugin service is one container on a private network shared with the
broker only. No plugin can reach another plugin, the edge or the broker's
database, and no plugin port is ever published. The broker sends each plugin
*requirements* (what the key may see, the limits, the permission the call
needs), never a credential. Details and trust boundaries:
[docs/architecture.md](docs/architecture.md).

## Quick start

You need Docker with Compose v2 and Python 3.

```bash
python scripts/init_secrets.py        # writes .env with generated secrets; prints no secret
docker compose up -d --build
curl http://127.0.0.1:8080/v1/health  # {"status":"ok","version":"0.3.0"}
```

1. Open <http://127.0.0.1:8080/admin> and create the owner account with the
   `SETUP_TOKEN` from `.env`.
2. Under Plugins, enable WhatsApp and pair your phone with the QR code.
3. Under Agent keys, create a key: pick the actions, optionally only on
   chosen chats, a mode (`draft` makes writes wait for your approval), a
   budget and an expiry. The key is shown once.
4. Point an agent at it:

   ```bash
   export AAB_KEY=aab_...
   curl -s http://127.0.0.1:8080/v1/me -H "Authorization: Bearer $AAB_KEY"
   curl -s -X POST http://127.0.0.1:8080/v1/targets/whatsapp/actions/list_chats \
     -H "Authorization: Bearer $AAB_KEY" -H "Content-Type: application/json" \
     -d '{"params": {"limit": 5}}'
   ```

   `GET /v1/me/skill` returns the agent's guide, filtered to that key. For
   MCP: `claude mcp add --transport http aab http://127.0.0.1:8080/mcp --header "Authorization: Bearer aab_..."`.

5. Approve the first draft under Requests, or on your phone after linking a
   Telegram bot under Channels.

Gmail, Calendar and Drive need your own Google OAuth client; GitHub needs a
GitHub App. You enter both in the console, never in `.env`
([google.md](docs/plugins/google.md), [github.md](docs/plugins/github.md)).

## Plugins

| Plugin | Container | What agents can do | Doc |
|---|---|---|---|
| WhatsApp | `plugin-whatsapp` + `whatsapp-sidecar` | List and read chats, search, download media, wait for new messages; send a text (draftable, schedulable) | [whatsapp.md](docs/plugins/whatsapp.md) |
| GitHub | `plugin-github` | Read repos, issues, PRs and files; open, comment, close, branch, push a file, open PRs; merge and delete branches (destructive) | [github.md](docs/plugins/github.md) |
| Gmail | `plugin-google` | Search and read threads, labels, attachments; draft, send, label, archive; trash and delete (destructive) | [google.md](docs/plugins/google.md) |
| Calendar | `plugin-google` | List calendars, events, free/busy; create, update, respond; delete (destructive) | [google.md](docs/plugins/google.md) |
| Drive | `plugin-google` | List, search, read, download; create, upload, move, share; trash and delete (destructive) | [google.md](docs/plugins/google.md) |

A plugin is a manifest (actions, resources, limits) plus an adapter in its
own container. The broker derives REST routes, MCP tools, approval cards, the
console's capability editor and the agent guide from the manifest, so a new
plugin touches no engine code.

**External plugins.** A plugin can live in its own repository. In the
console, click **+ Add plugin**, enter `github.com/you/aab-plugin-x` at a
release tag, review what it asks for, and install. The opt-in installer
builds and starts it; you approve the manifest first. Start with
[docs/plugins-guide.md](docs/plugins-guide.md), then
[docs/plugin-packaging.md](docs/plugin-packaging.md).

## Agents

- **REST.** Every action is `POST /v1/targets/{target}/actions/{action}` with
  `{"params": {...}}`, plus optional `as_draft`, `run_at` and `note`. A call
  answers `200` with the result, `202` when it waits for approval or its
  time, `403` when outside the grant, `404` for missing or hidden, `503` when
  not delivered and safe to retry, `502` when the outcome is unknown.
- **MCP.** `/mcp` serves the same key one tool per action the key can reach
  right now, plus `get_my_access`, `request_permission` and `delegate`.
- **The guide.** `GET /v1/me/skill` tells the agent how to address things and
  what the rules are, from the manifests, for that key only.

[docs/mcp.md](docs/mcp.md), [docs/delegation.md](docs/delegation.md),
[docs/grant-algebra.md](docs/grant-algebra.md).

## Documentation

| Read this | For |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Containers, networks, trust boundaries, data flows |
| [docs/deployment.md](docs/deployment.md), [deploy/DEPLOY.md](deploy/DEPLOY.md) | Running it, exposing it behind Cloudflare, backups, the installer |
| [docs/configuration.md](docs/configuration.md) | What lives in `.env` and what you set in the console |
| [docs/console.md](docs/console.md) | Every console view and what it does |
| [docs/auth.md](docs/auth.md) | The owner account, admin and monitor tokens, Cloudflare Access |
| [docs/plugins-guide.md](docs/plugins-guide.md) | How plugins work and how to write one |
| [docs/status.md](docs/status.md) | What is verified, known limits, caveats |
| [docs/README.md](docs/README.md) | The full index |

## Development

```bash
cd broker && python -m venv .venv && .venv/Scripts/pip install -e ".[dev]" -e ../plugin-runtime
cd broker && .venv/Scripts/python -m pytest          # the broker
cd sidecars/whatsapp && go build ./... && go test ./...
```

The plugin runtime, the installer and each plugin service have their own
`pytest` suite under their directory. [CLAUDE.md](CLAUDE.md) holds the
conventions. The `aab` CLI, installed with the broker, drives the admin API
from a terminal; `aab --help` lists its commands.

## License

Copyright (C) 2026 Peleg Wasserman.

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or (at your
option) any later version. See [LICENSE](LICENSE) for the full text. The
AGPL's network clause applies: if you run a modified version of the broker
as a service, you must offer its source to the users of that service.

The plugin runtime (`plugin-runtime/`) and the plugin base image are part of
this program. A plugin that imports `aab_plugin_runtime` is a work based on
it, so it must be distributed under the AGPL as well, unless you hold a
different license from the author. A commercial license for uses the AGPL
does not fit is available from the author.
