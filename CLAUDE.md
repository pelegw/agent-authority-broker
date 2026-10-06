# CLAUDE.md

Guidance for anyone (human or AI) working in this repository.

## What this is

The Agent Authority Broker stands between AI agents and the systems they work
in (WhatsApp, GitHub, Gmail, Calendar, Drive). Agents hold no credentials, and
neither does the broker: each target's credential lives only inside that
plugin's own container (see `docs/architecture.md`). Each
agent key's effective permission is `P(owner) ∩ G(grant chain) ∩ R(role)`,
evaluated live on every call; grants can only ever narrow; every decision is
recorded with its full authority chain. It is the successor of WA_GW (a
WhatsApp-only gateway) and reuses its security plumbing.

- Implementation plan (source of truth for design and phases):
  `C:\Users\Peleg\.claude\plans\eager-sparking-pudding.md`
- Python package `broker` in `broker/broker/`; admin CLI `aab` in `broker/cli/`.
- Go WhatsApp sidecar in `sidecars/whatsapp/` (its own Docker build and `go test`).
- External plugins live in their own repositories and are installed by the
  opt-in `aab-installer` (`installer/`, `docker-compose.installer.yml`); the
  plugin author's contract is `docs/plugin-packaging.md`, the base image
  `plugins/base/Dockerfile`.
- Agent keys are `aab_...`; owner admin tokens are `aab_admin_...`; monitor
  tokens (`/health` and `/v1/health` only) are `aab_monitor_...`.

## Running tests

```bash
cd broker && python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # once (bin/ on Linux)
cd broker && python -m pytest
cd installer && python -m pytest          # with the broker venv: ../broker/.venv/Scripts/python.exe
cd plugin-runtime && python -m pytest
cd sidecars/whatsapp && go build ./... && go test ./...
```

All must be green before any commit. CI (`.github/workflows/ci.yml`) runs the
same plus a skill-drift job (`aab skill build` + `git diff --exit-code integrations/`).

## Conventions

**Code shape**
- Small, focused modules. Every module starts with a docstring saying what it
  is and why it exists; comments explain *why*, not *what*, for a human reader.
- A test for every behaviour (pytest + `fastapi.testclient`). Bug fixes come
  with a regression test. Shared fixtures live in `broker/tests/conftest.py`;
  `env` (fresh temp DB + clean settings) is the root fixture.
- Configuration via `pydantic-settings` in `broker/broker/config.py`,
  env-first, cached by `get_settings()` (tests call `get_settings.cache_clear()`).
  Only broker-wide settings go there; per-plugin config is data in the DB.
- Storage is raw `sqlite3` (`broker/broker/db.py`). Schema changes are additive
  only: new tables in `SCHEMA`, new columns in `_MIGRATIONS`. Never rename or
  drop a column.
- Run exactly one uvicorn worker (in-process rate limiter, single SQLite writer).
- Logging (`docs/logging.md`): `log = logging.getLogger(__name__)`, a fixed
  message plus `kv(...)` for the variable part; never params, results,
  message text, labels, notes, secrets or tokens (names of secret fields are
  fine). `tests/targets/test_secrets_in_logs.py` sweeps every secret flow at
  DEBUG. `logging_setup.py` and `request_log.py` exist three times (broker,
  plugin runtime, installer), byte-identical by test: change all three.
- MCP uses the official `mcp` SDK (low-level `Server` +
  `StreamableHTTPSessionManager`), never the third-party `fastmcp` package.
- The console is a single-file vanilla-JS page (`templates/console.html`) with a
  hash router. No frontend build step.
- The version lives only in `VERSION` at the repo root. `broker/__init__.py`
  reads it; hatch, FastAPI, the CLI and the Docker image all derive from that.

**Authority rules (never bend these)**
- The grant is the only authority object. Child grants are created only from
  the output of `narrow()` (a `NarrowedCapabilities` value); nothing else may
  insert a child grant.
- Hidden == 404. A hidden or denied resource is indistinguishable from a
  missing one, and is filtered out of every list.
- Manifests are data. Tools, REST routes, Telegram cards, the console editor and
  the skill doc are derived from `targets/*/manifest.yaml`. Adding a plugin
  must not touch engine files.
- Adapter errors: **503** = not delivered, safe to retry (reservation released);
  **502** = outcome unknown, never auto-retried.
- Telegram never gains an agent-facing approve path. No approve tool over MCP
  either; a test asserts `services/admin.py` is unreachable from `mcp_server.py`.
- A plugin is served only against an owner-approved manifest: the vendored
  file for an in-tree plugin (the tree always wins), else the owner's pin in
  `plugin_pins` (`plugins/pins.py`, written only by an audited owner action).
  The install flow pins before it asks the installer for anything.
- The installer renders overlays from the descriptor; it never loads compose
  or scripts from a plugin repo. Its bounds (`INSTALLER_ENABLED`,
  `INSTALLER_ALLOWED_SOURCES`, `INSTALLER_TOKEN`, `INSTALLER_GIT_TOKEN`,
  `AAB_HOME`) are file-only; nothing in the console can widen them.
- Every human action carries an `AdminContext` (principal, surface: session |
  token | telegram) and is recorded under the owner's username, never "admin".

**Secrets**
- Broker-owned secrets are generated by `scripts/init_secrets.py`, never typed
  by hand. `.env.example` is that script's `--example` output (a test enforces it).
  The one third-party credential in `.env` is the optional, read-only
  `INSTALLER_GIT_TOKEN`: it reaches the installer container alone, and git
  only through `GIT_ASKPASS`, never in a URL or a log.
- Target credentials never enter the broker. Each plugin service keeps its
  own, encrypted in its own secret volume under `PLUGIN_SECRETS_KEY_<SERVICE>`
  through the plugin runtime's secret store; the broker relays a secret config
  field once to the plugin's `/configure` and never stores it. Tokens and
  secrets are never logged, printed, or returned after creation.
- One container per plugin service; the broker reaches each plugin over
  that plugin's own network (`net_whatsapp`, `net_github`, `net_google`, and
  `net_<service>` for each installed external plugin) with a per-service
  `X-Plugin-Token`, so no plugin can reach another; the edge sits on
  `edge_net` and can never reach a plugin; the WhatsApp sidecar is reachable
  only from its plugin over `wa_internal`; `aab-installer` (root on the host:
  it holds the Docker socket, mounted nowhere else) shares `net_installer`
  with the broker alone, and no plugin is ever on it.
- `plugins.d/` belongs to the installer: the checkout, the rendered
  `compose.yml` and `install.json` of each installed service, and its job
  state under `plugins.d/_installer/`. It is git-ignored, `deploy/push.sh`
  never syncs or deletes it, and nothing else writes there. The compose file
  set is never hand-listed: `scripts/compose-files.sh` prints it
  (`docker compose $(scripts/compose-files.sh) ...`).
- The WhatsApp session (`session.db`, plaintext) lives in the `wa_session`
  volume, which only the sidecar mounts; `plugin-whatsapp` mounts only the
  archive (`wa_data`, read-only). Never mount `wa_session` anywhere else.

**Generated files**
- The agent skill doc and everything under `integrations/` are generated
  (`aab skill build`), never hand-edited. CI fails on drift.

## Git

Develop on `dev`; merge to `main` only when asked. Commit messages end with the
session's attribution trailers.

## Phase status

- [x] 0. Skeleton: repo, VERSION, config, db schema, security ports, compose, CI, secrets script
- [x] 1. Identity: owner setup/login/sessions, admin tokens, `require_admin` -> `AdminContext`
- [x] 2. Authority core: capabilities, grants, `narrow()`, roles, `aab_` keys, hypothesis suite
- [x] 3. Engine: plugin runtime + registry, policy, decisions, ledger, actions, REST, MCP (parity-tested), Telegram approvals, console-managed settings, per-plugin containers + env split
- [x] 4. WhatsApp plugin service + admin console (setup/login, plugins with connect panels and the shared Google account card, keys with the capability editor, requests, scheduled, hidden, decisions, delegations tree, channels, settings, account)
- [x] 5. Delegation + generated skill doc (CI drift job is real) + draft-mode default for agent requests
- [x] 6. GitHub plugin service (App installation tokens per call; PAT fallback reported as proxy)
- [x] 7. Google plugin service (Gmail, Calendar, Drive over one OAuth connection) + `docs/platform-thesis.md`
- [x] 8. Simulation, release docs, SQLite flake fix, tag v0.2.0 (all Python suites green; Go sidecar unchanged since phase 0). Still to verify with Docker running: image builds, the read-only wa_data WAL mount, Google downscoped refresh against the real endpoint.
