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
| `platform-thesis.md` | phase 7 | What adding Google after GitHub cost: manifest fields, engine files touched (target zero), shared adapter code. |

The public deploy runbook (EC2 + Cloudflare) is `deploy/DEPLOY.md`. The implementation plan is referenced from `CLAUDE.md`.
