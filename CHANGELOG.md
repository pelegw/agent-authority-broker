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
