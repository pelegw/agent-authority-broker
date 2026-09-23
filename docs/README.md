# docs

Design documents land here as the phases that need them are built:

| File | Added in | What it covers |
|---|---|---|
| `auth.md` | phase 1 | Owner account: setup token, password login + sessions, admin tokens, Cloudflare Access as the outer layer, later 2FA/passkeys. |
| `grant-algebra.md` | phase 2 | Capability statements, the six narrowing forms, `cap_le` / `meet` / `narrow`, chain evaluation, denies outside the lattice. |
| `manifest-schema.md` | phase 3 | The plugin manifest format: resources, narrowings, constraints, actions, skill section. |
| `platform-thesis.md` | phase 7 | What adding Google after GitHub cost: manifest fields, engine files touched (target zero), shared adapter code. |

Deployment lives in `deploy/DEPLOY.md`. The implementation plan is referenced from `CLAUDE.md`.
