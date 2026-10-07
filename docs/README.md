# docs

This directory holds the design and operator documents. The table gives the phase that added each one:

| File | Added in | What it covers |
|---|---|---|
| `architecture.md` | phase 0 | System description, deployment topology, data flow diagrams with trust boundaries (threat-model input), what the target enforces and what only the proxy enforces, glossary, build status, open points. |
| `auth.md` | phase 1 | Owner account: setup token, password login and sessions, admin tokens, CSRF, Cloudflare Access as the outer layer (`/admin*`, `/auth*`, `/v1/admin*`, `/oauth*`), the OAuth callback page, later 2FA and passkeys. |
| `grant-algebra.md` | phase 2 | Capability statements, the six narrowing forms, `cap_le` / `meet` / `narrow`, chain evaluation, denies outside the lattice. |
| `manifest-schema.md` | phase 2 | The plugin manifest format: resources, narrowings, constraints, actions, skill section. |
| `deployment.md` | phase 3 | One container per plugin service: local and public run, networks, the checks to run after `docker compose up`, the env split per container, the GitHub App key file, what each volume holds, rotation per secret. |
| `configuration.md` | phase 3 | What lives in files and why (bootstrap secrets, fail-closed exposure settings), and what lives in the console; every console setting with its bounds. |
| `plugin-api.md` | phase 3 | The internal plugin API that `aab-plugin-runtime` serves (`/manifests`, `/perform`, connect flows, `X-Plugin-Token`, 503/502 contract). |
| `mcp.md` | phase 3 | The MCP surface: the tools the broker makes per request from the manifests and the caller's reach, `<plugin>_<action>` names, call controls, result encoding, what is absent on purpose. |
| `plugins/whatsapp.md` | phase 4 | The WhatsApp plugin service: actions and visibility, JID rules (sendable and read-only chats), container env, QR pairing flow, status mapping, the 503/502 table. |
| `console.md` | phase 4 | The owner console (passes 1 and 2): setup and login, every view (Channels for Telegram, Settings and Delegations included), the capability editor, the shared Google account card, the connection panels and what they read from a plugin's status, the nonce CSP, why the page holds no data. |
| `delegation.md` | phase 5 | Agent-minted child keys: how `narrow()`, and nothing else, builds a chain, the owner's key tree, revocation propagation, the service-level properties 10 and 11. The generated agent skill doc is `integrations/claude-skill/agent-authority-broker/SKILL.md` (`aab skill build`; CI checks drift). |
| `plugins/github.md` | phase 6 | The GitHub plugin service: how to create the GitHub App (permissions, Setup URL, install), the private key (pasted, or read from a file inside `/run/secrets/github`), per-call installation tokens, what the target enforces and what only the proxy enforces, the caveats of the PAT fallback, 503/502. |
| `plugins/google.md` | phase 7 | The Google plugin service (gmail, gcal, gdrive): OAuth client setup and redirect URIs, consent scopes, one token per scope set, what the target enforces and what only the proxy enforces, every narrowing and constraint. |
| `platform-thesis.md` | phase 7 | What the second and third plugins cost the platform, measured from the diffs of the lanes: engine files touched (zero for each), files touched outside each plugin, code and manifest sizes, the schema features Google needed, what did not generalize, the flag-polarity rule. |
| `logging.md` | after 0.2.0 | What each service logs and the line format (text or JSON), request ids across broker, plugin and sidecar and their link to the decision record, the access line without query strings, the never-logged list and the redaction backstop, `LOG_LEVEL` / `LOG_FORMAT`, rotation, how to read and ship logs, and the opt-in New Relic overlay that ships the logs and the audit record (with NRQL examples). |
| `approval-volume.md` | phase 8 | Simulated human interrupts under standing grants and under per-action approval (8-hour reference numbers), budget exhaustion, what the simulation does not model. |
| `plugin-packaging.md` | 0.3.0 | External plugins: a plugin as its own repository, the `aab-plugin.yaml` descriptor (every field and rule), the base image `ghcr.io/pelegw/aab-plugin-base` and a Dockerfile template, the overlay the installer writes and the limits it enforces, versioning, install / upgrade / remove below the console, private repositories (the read-only GitHub token, a console setting), work against a gateway checkout. |
| `plugins-guide.md` | 0.3.0 | The plugin guide for a reader with no prior knowledge, in simplified technical English with diagrams: what a plugin is, how one call flows, the manifest and plugin API contracts, the adapter, the connection, the runtime, the package and descriptor, the install flow, a complete worked plugin with tests, the security rules for plugin authors. |
| `status.md` | 0.3.0 | What is verified and what is not, the operating limits and caveats (moved out of the README). |

The public deploy runbook (EC2 and Cloudflare) is `deploy/DEPLOY.md`. `CLAUDE.md` gives the path of the implementation plan.
