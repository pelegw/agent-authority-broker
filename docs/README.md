# docs

Design documents land here as the phases that need them are built:

| File | Added in | What it covers |
|---|---|---|
| `architecture.md` | phase 0 | System description, deployment topology, data flow diagrams with trust boundaries (threat-model input), glossary. |
| `auth.md` | phase 1 | Owner account: setup token, password login + sessions, admin tokens, Cloudflare Access as the outer layer, later 2FA/passkeys. |
| `grant-algebra.md` | phase 2 | Capability statements, the six narrowing forms, `cap_le` / `meet` / `narrow`, chain evaluation, denies outside the lattice. |
| `manifest-schema.md` | phase 2 | The plugin manifest format: resources, narrowings, constraints, actions, skill section. |
| `deployment.md` | phase 3 | One container per plugin service: local and public run, networks, the env split per container, what each volume holds, rotation per secret. |
| `configuration.md` | phase 3 | What lives in files and why (bootstrap secrets, fail-closed exposure settings) versus the console; every console setting with its bounds. |
| `plugin-api.md` | phase 3 | The internal plugin API served by `aab-plugin-runtime` (`/manifests`, `/perform`, connect flows, `X-Plugin-Token`, 503/502 contract). |
| `mcp.md` | phase 3 | The MCP surface: per-request tool derivation from manifests and the caller's reach, `<plugin>_<action>` naming, call controls, result encoding, what is deliberately absent. |
| `plugins/whatsapp.md` | phase 4 | The WhatsApp plugin service: actions and visibility, JID rules (sendable vs read-only chats), container env, QR pairing flow, status mapping, the 503/502 table. |
| `console.md` | phase 4 | The owner console: setup/login, views, the capability editor, the nonce CSP and why the page holds no data. |
| `delegation.md` | phase 5 | Agent-minted child keys: how a chain is built (only by `narrow()`), the owner's key tree, revocation propagation, the service-level properties 10 and 11. The generated agent skill doc is `integrations/claude-skill/agent-authority-broker/SKILL.md` (`aab skill build`; CI checks drift). |
| `plugins/github.md` | phase 6 | The GitHub plugin service: creating the GitHub App (permissions, Setup URL, install), per-call installation tokens, what is target- vs proxy-enforced, the PAT fallback's caveats, 503/502. |
| `plugins/google.md` | phase 7 | The Google plugin service (gmail, gcal, gdrive): OAuth client setup and redirect URIs, consent scopes, per-scope-set tokens, what is target- vs proxy-enforced, every narrowing and constraint. |
| `platform-thesis.md` | phase 7 | What adding Google after GitHub cost: manifest fields, engine files touched (target zero), shared adapter code. |
| `approval-volume.md` | phase 8 | Simulated human interrupts under standing grants vs per-action approval (8-hour reference numbers), budget exhaustion, and what the simulation does not model. |

The public deploy runbook (EC2 + Cloudflare) is `deploy/DEPLOY.md`. The implementation plan is referenced from `CLAUDE.md`.
