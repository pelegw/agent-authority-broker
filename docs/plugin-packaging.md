# Plugin packaging: external plugins

An **external plugin** lives in its own git repository, not in this one. The
owner installs it into a running gateway from the console (Plugins,
**+ Add plugin**, `github.com/you/aab-plugin-name@v0.1.0`), reviews what it
asks for, and clicks Install; the opt-in installer container builds and
starts it. The finance plugin (`pelegw/aab-plugin-finance`, private) is the
reference package.

Nothing about authority changes. The broker registers a plugin only when the
service offers exactly the manifest the owner approved (the **pin**, stored in
the `plugin_pins` table instead of a vendored file); the engine files are
untouched by any plugin; each plugin keeps its own network, volumes and
token. What this document covers is the plugin author's side of the
contract: the repository layout, the descriptor, the base image, what the
installer renders and guarantees, versioning, install / upgrade / remove,
and how to develop against a gateway checkout. The operator's side (turning
the installer on, backups, the acceptance test) is in `docs/deployment.md`;
the console flow is in `docs/console.md`.

## A plugin repository

```
aab-plugin.yaml                  # the descriptor (below); the installer reads nothing else to decide
Dockerfile                       # FROM ghcr.io/pelegw/aab-plugin-base:<gateway version>
pyproject.toml                   # depends on aab-plugin-runtime by version, never by URL
VERSION                          # the plugin's own version
aab_plugin_<name>/manifest.yaml  # one manifest per hosted plugin id
aab_plugin_<name>/...            # the adapter, exactly as for an in-tree plugin (docs/plugin-api.md)
tests/
```

The adapter is written exactly as an in-tree one: an
`aab_plugin_runtime` adapter class and a `main.py` with a `create_app()`
factory that calls `serve(...)` (see `plugins/github/` and
`docs/plugin-api.md`). The manifest follows `docs/manifest-schema.md` and is
validated by the broker's own strict schema before the owner can pin it.

## The descriptor: `aab-plugin.yaml`

Schema 1, validated by `installer/aab_installer/descriptor.py` (pydantic,
strict types, unknown keys refused, each rule below has a rejecting test in
`installer/tests/test_descriptor.py`). The finance plugin's:

```yaml
schema: 1
service: finance                      # compose service plugin-finance, network net_finance
plugins: [finance]                    # the manifest ids this service hosts
manifests: [aab_plugin_finance/manifest.yaml]   # one per plugin id, same order
runtime: "0.3"                        # the gateway line it was built against (informational)
build: {dockerfile: Dockerfile}       # the context is always the repository root
volumes: {finance_data: /data}        # named volumes only, names start with "finance_"
environment: {FINANCE_DB: /data/finance.db}   # literal values only
env_passthrough: [TZ]                 # host .env keys it may read: TZ, LOG_LEVEL, LOG_FORMAT
```

| Key | Type | Rules |
|---|---|---|
| `schema` | integer | Exactly `1`. |
| `service` | string | `^[a-z][a-z0-9]{1,31}$` (one lowercase word, no `-` or `_`), and not a name the stack already uses: `broker`, `edge`, `caddy`, `installer`, `sidecar`, `internal`, `default`, `whatsapp`, `wa`, `github`, `google`, `plugin`, `plugins`. Every other name derives from it (below). |
| `plugins` | list of strings, 1 to 16 | Each `^[a-z][a-z0-9]*$` (the broker's manifest id rule), unique. Must be exactly the ids the service's `GET /manifests` offers. |
| `manifests` | list of strings, 1 to 16 | One per plugin id, in the same order. Each a relative POSIX path inside the repository (`[A-Za-z0-9_][A-Za-z0-9_.-/]*`, at most 200 characters, no `..`, no empty segment) ending in `.yaml` or `.yml`, unique. |
| `runtime` | string | `0.3` or `0.3.0` style (quote it: `"0.3"`, strict types refuse a YAML float). Informational: the base image pins the runtime. |
| `build` | mapping | Only `dockerfile` (default `Dockerfile`), a relative path like the manifest paths. The build context is always the repository root. |
| `volumes` | mapping, at most 8 | Name to absolute mount path. Names match `^[a-z][a-z0-9_]*$` and start with `<service>_` (a plugin names only its own volumes; service names have no underscore, so the prefix names exactly one service). `<service>_secrets` is the installer's (mounted at `/secrets`). Mount paths are absolute (`/[A-Za-z0-9_.-/]*`, no `.` or `..` segment), not `/`, not `/secrets` or anything below it, and unique. |
| `environment` | mapping, at most 32 | Names `^[A-Z][A-Z0-9_]{0,63}$`, never starting with `PLUGIN_` (the installer sets those) and never `TZ`, `LOG_LEVEL` or `LOG_FORMAT` (use `env_passthrough`). Values are literal strings of at most 1024 characters with no `$` (compose would interpolate it from the `.env` that holds every service's secrets) and no control character. |
| `env_passthrough` | list of strings | A subset of `TZ` (default `UTC`), `LOG_LEVEL` (`INFO`) and `LOG_FORMAT` (`text`), unique: the host `.env` keys the plugin may read, rendered as `${KEY:-default}`. |

The file must be UTF-8 and at most 64 KiB. When the installer reads a package
it also checks, through real files only (a symlink anywhere on a path is
refused, and git checks symlinks out as plain files anyway): the Dockerfile
exists and is at most 1 MiB; each manifest is at most 256 KiB of UTF-8 YAML
whose `id` is the plugin id listed at its position and whose `version` is
`MAJOR.MINOR.PATCH`. The broker then validates each manifest fully
(`plugins/manifest.py`) before the review is shown.

Names derived from `service` (here `finance`):

| What | Name |
|---|---|
| Compose service | `plugin-finance` |
| Its network (shared with the broker only) | `net_finance` |
| Its encrypted secret store | volume `finance_secrets` at `/secrets` |
| Its token, in the host's `.env` | `PLUGIN_TOKEN_FINANCE` (the broker presents it as `X-Plugin-Token`) |
| Its secret-store key, in the host's `.env` | `PLUGIN_SECRETS_KEY_FINANCE` |
| Where the broker finds it | `PLUGIN_URL_FINANCE=http://plugin-finance:8090` |
| Its files on the host | `plugins.d/finance/` (`src/`, `compose.yml`, `install.json`) |

## What the installer renders, and what that guarantees

A plugin repository cannot supply compose YAML. The installer renders
`plugins.d/<service>/compose.yml` from the validated descriptor through a
fixed template (`installer/aab_installer/overlay.py`; its golden file is
`installer/tests/golden/finance.compose.yml`):

```yaml
services:
  plugin-finance:
    build: {context: ./plugins.d/finance/src, dockerfile: Dockerfile}
    restart: unless-stopped
    logging: {driver: json-file, options: {max-size: 10m, max-file: '5'}}
    networks: [net_finance]
    volumes: [finance_secrets:/secrets, finance_data:/data]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    environment:
      PLUGIN_TOKEN: '${PLUGIN_TOKEN_FINANCE:?missing in .env ...}'
      PLUGIN_SECRETS_KEY: '${PLUGIN_SECRETS_KEY_FINANCE:?missing in .env ...}'
      PLUGIN_SECRETS_DIR: /secrets
      FINANCE_DB: /data/finance.db
      TZ: ${TZ:-UTC}
  broker:
    networks: [net_finance]
    environment:
      PLUGIN_URL_FINANCE: http://plugin-finance:8090
      PLUGIN_TOKEN_FINANCE: '${PLUGIN_TOKEN_FINANCE:?missing in .env ...}'
networks: {net_finance: {}}
volumes: {finance_secrets: {}, finance_data: {}}
```

So by construction every external plugin gets the shape the in-tree plugins
have (the invariants `broker/tests/test_config_files.py` enforces for them,
enforced here by the template and `installer/tests/test_overlay.py`):

- exactly one network, its own, shared with the broker only: it cannot reach
  another plugin, the installer or the edge, and nothing but the broker can
  reach it;
- no published port, no bind mount (nothing on the host is reachable), only
  named volumes it declared plus its secret store, all prefixed with its own
  service name;
- only its own token and key, under the runtime's generic names; a `.env`
  missing them makes compose refuse to start it rather than start it open;
- no extra capability, no privilege escalation, the rotated `json-file`
  logging every service uses, `restart: unless-stopped`.

## The base image

`plugins/base/Dockerfile` builds `ghcr.io/pelegw/aab-plugin-base:<version>`
(and `:latest`), published by `.github/workflows/release.yml` when the
gateway is tagged `v<version>`; CI builds it on every push without pushing,
and checks the contract below inside the image. It provides:

- Python 3.12 (`python:3.12-slim`), `WORKDIR /srv`;
- `aab-plugin-runtime` from the gateway's `plugin-runtime/` at that release,
  and `uvicorn`;
- the unprivileged user `aab`, uid/gid 10001 (every image in the stack uses
  it);
- `/secrets` owned by `aab`, mode 0700, declared a `VOLUME`, and
  `PLUGIN_SECRETS_DIR=/secrets`, `PYTHONDONTWRITEBYTECODE=1`,
  `PYTHONUNBUFFERED=1`;
- a TCP liveness check on `:8090` (every plugin API route requires the
  token, which a healthcheck must not hold) and `EXPOSE 8090`;
- `USER aab`, and **no `CMD`**: the plugin names its own app factory.

The same release attaches the runtime wheel (`aab_plugin_runtime-<version>-py3-none-any.whl`)
to its GitHub Release, for development installs without a gateway checkout.

GHCR packages of a private repository are private. On the host, run
`docker login ghcr.io` once with a token that has `read:packages`, and
`docker pull ghcr.io/pelegw/aab-plugin-base:<version>` for each base version
your plugins name: the installer drives the host's Docker daemon but holds
no registry credentials of its own, so its builds use the base image from
the daemon's image store (`docs/deployment.md`). On a development machine,
`docker login ghcr.io` is enough for your own `docker build`. The base image
is the only thing a plugin build takes from the gateway, so a plugin never
needs access to the gateway repository itself.

### Dockerfile template

The finance plugin's Dockerfile, reduced to what every plugin needs:

```dockerfile
FROM ghcr.io/pelegw/aab-plugin-base:0.3.0

# Code is installed as root and stays root-owned: read-only to the process.
# The runtime is already in the base image, so this fetches only the
# plugin's own dependencies.
USER root
COPY VERSION pyproject.toml /tmp/aab-plugin-name/
COPY aab_plugin_name /tmp/aab-plugin-name/aab_plugin_name
RUN pip install --no-cache-dir /tmp/aab-plugin-name \
 && rm -rf /tmp/aab-plugin-name

# A declared data volume: created and owned in the image so a fresh named
# volume mounted here inherits aab ownership. Optional.
RUN mkdir -p /data && chown aab:aab /data && chmod 0700 /data
VOLUME /data

USER aab
EXPOSE 8090
# One worker; --no-access-log: the runtime writes its own access line with
# the broker's request id (docs/logging.md).
CMD ["uvicorn", "--factory", "aab_plugin_name.main:create_app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", "--no-access-log"]
```

The plugin's `pyproject.toml` names the runtime **by version**
(`"aab-plugin-runtime>=0.3.0,<1"`; the finance plugin accepts `>=0.2.0,<1`),
never by URL: pip always re-resolves a direct-URL dependency even when the
runtime is already installed, so a URL would make the image build clone the
gateway (private, so it would fail). For a standalone install outside the
image, offer the URL as an extra:

```toml
[project.optional-dependencies]
runtime = ["aab-plugin-runtime @ git+https://github.com/pelegw/agent-authority-broker@v0.3.0#subdirectory=plugin-runtime"]
```

## Versioning

- Release the plugin by tagging `vMAJOR.MINOR.PATCH`. The installer accepts a
  release tag or a full 40-hex commit, never a branch (a branch can move
  under a reviewed install), and refuses a `v1.2.3` that resolves to a
  branch rather than a tag.
- **Bump the manifest's `version` whenever the manifest changes.** The broker
  pins id and version: a service offering a different version than the pin is
  refused and shown for review (Offered, awaiting review), and a changed
  manifest under the same version is never used, because the broker serves
  its pinned copy, not the plugin's.
- `FROM ghcr.io/pelegw/aab-plugin-base:<x.y.z>` names the gateway release the
  plugin was built and tested against, and `runtime: "x.y"` in the
  descriptor says the same for a reader. The base image and the runtime wheel
  are published for every gateway tag; the runtime stays on the gateway's
  `x.y` line (the release workflow refuses otherwise).

## Install, upgrade, remove

The owner's side is in `docs/console.md`; what happens underneath:

1. **Inspect.** The broker asks the installer to clone the source at the ref
   (an allowlisted source only: `INSTALLER_ALLOWED_SOURCES`), read the
   descriptor and the manifests, and return them with the resolved commit.
   The broker validates every manifest and shows the review: each action
   with its side effect and modes, resources, narrowings, constraints, the
   settings it will ask for and which are secret, the volumes, environment
   and passthrough, and on an upgrade the diff against the current pin.
2. **Install.** The broker inspects again, refuses if the ref no longer
   resolves to the reviewed commit, **pins every manifest** (audited
   `plugin.pin`), and only then asks the installer for the job
   (`plugin.install`). The job fetches the reviewed commit into
   `plugins.d/<service>/src`, makes sure `.env` holds the service's token and
   key (generated by `scripts/init_secrets.py --rotate`), renders the
   overlay, writes `install.json`, runs
   `docker compose ... up -d --build plugin-<service>` and then
   `up -d broker` (recreated with its new environment and network, never
   rebuilt). Any failure rolls the files back, so a broken overlay never
   stays in the compose file set. The broker comes back, discovers the
   service, finds the pin, and the plugin's card appears **disabled**; the
   owner enables it as any other.
3. **Upgrade.** The same, against an installed service from the same source
   (another repository under the same service name is a remove and an
   install, each reviewed): the new manifests are pinned first, then the
   service is rebuilt and the broker recreated. A failed upgrade restores the
   previous checkout and overlay and starts them again; the broker then
   offers the old manifest for review (the pin already moved), and pinning
   it there restores the plugin. A refused upgrade request (the installer
   busy or down) puts the old pins back at once.
4. **Remove.** The installer stops and deletes `plugin-<service>`, deletes
   `plugins.d/<service>/`, recreates the broker without the service and
   removes its network; the broker then unpins every plugin the service
   hosted (agents get 404 at once; the plugin rows stay, disabled). Without
   purge, the service's volumes and its two `.env` secrets are kept (the
   secrets commented out as `#aab-retired# PLUGIN_TOKEN_<SERVICE>=...`), so a
   later install of the same service restores the same key and its data
   still decrypts. With purge, the `<service>_*` volumes and both secrets are
   deleted for good.

One job runs at a time; its state and log lines (never a token) live in
`plugins.d/_installer/jobs/<id>.json` and survive restarts of both the
installer and the broker, which is how the console follows a job through the
broker's own restart.

## Private repositories: the GitHub token in the console

The installer clones anonymously over https unless the owner has stored a
**read-only** GitHub token in the console: Plugins, **+ Add plugin**, the
"GitHub token for private plugin repositories" field (Set, Clear, and a
state badge: not set, set, or re-enter required). Make it:

- fine-grained (preferred): resource owner = the plugins' owner, repository
  access = only the plugin repositories, permissions = Contents: read-only
  (Metadata: read-only comes with it);
- or a classic token with the `repo` scope (it reads every repository the
  account can: prefer fine-grained).

It is not in `.env`. Like the Telegram bot token, it is a third-party
credential, so the broker stores it encrypted under `BROKER_SECRETS_KEY`
(write-only: no route returns it) and the installer holds none of its own.
The broker sends it in the body of each inspect, install and upgrade request
(`git_token`); the installer uses it for that request's clone, or for the
job's single clone at its start, and writes it nowhere: not the job record,
`install.json`, a job log line or a log line. git gets it only through
`GIT_ASKPASS`: a fixed script the installer writes (it holds no secret)
prints the token from the environment of the one clone or fetch that talks
to the remote. The token is never part of a URL or an argument, so no git
error, process listing or job log can carry it; it is offered only when the
source is on `github.com` (and on the allowlist, which is checked first),
and the script answers only `github.com`'s credential prompts (a redirect to
another host gets nothing). Job log lines are masked for its value whatever
its shape, and the redaction backstop of every service masks URL credentials
and the field names `git_token`, `installer_git_token` and
`installer_token`.

Rotate it at GitHub, then paste the new one in the same field (Replace). If
`BROKER_SECRETS_KEY` changes, the field shows "re-enter required" and
inspect, install and upgrade answer 409 until you paste it again or clear
it. Nothing needs a restart.

The plugin's own Docker build does not need it: the base image already holds
the runtime, and the build context is the clone the installer made.

## Developing against a gateway checkout

- **Unit tests** need only the runtime: from the plugin repository,
  `pip install -e "$AAB_SRC/plugin-runtime" -e ".[dev]"` with `AAB_SRC`
  pointing at a gateway checkout (or install the runtime wheel from the
  gateway's release), then `python -m pytest`.
- **Broker-level tests** (pinning, the capability algebra, MCP tools, hidden
  resources against the real engine): install `broker` from the checkout
  (`pip install -e "$AAB_SRC/broker[dev]"`) and register the adapter in
  process with `Registry(vendored_dirs=(<a directory holding <id>/manifest.yaml>,))`,
  the seam the gateway's own tests use (`broker/tests/conftest.py`). The
  finance plugin keeps these in `tests/integration/`, skipped when `AAB_SRC`
  is absent.
- **The image**: `docker login ghcr.io` once, then `docker build .` in the
  plugin repository.
- **The whole install path, locally**: run a gateway checkout with
  `INSTALLER_ENABLED=true`, `INSTALLER_ALLOWED_SOURCES=github.com/<you>/*`
  and `AAB_HOME` set to the checkout's absolute path as the Docker daemon
  sees it (the installer mounts it at the same path on both sides), pull the
  base image into the daemon (as on a server), then
  `docker compose $(scripts/compose-files.sh) up -d --build` and install from
  the console. Tag a test release (or use a full commit) of the plugin.
  The acceptance test in `docs/deployment.md` walks through it.
- **CI for a plugin repository**: the unit tests with the runtime installed
  from the gateway tag (a token that can read the gateway repository if it
  is private), the integration tests with a gateway checkout at that tag, and
  `docker build` after `docker login ghcr.io` (the base image is private
  with the gateway).

## What the installer never does

- Load compose YAML, scripts or configuration from a plugin repository: the
  overlay comes from the template, the `.env` entries from
  `scripts/init_secrets.py`, and the only thing of the plugin's that runs is
  its Dockerfile, inside `docker build`, like any image.
- Clone anything not in `INSTALLER_ALLOWED_SOURCES` (env-only, fail closed:
  empty refuses everything), over anything but https, at anything but a tag
  or a full commit, or install a commit other than the one the owner
  reviewed.
- Decide authority: the broker pins, and a plugin is served only when it
  offers exactly the pinned manifest.
- Keep a git credential: the GitHub token comes with the one request that
  needs it and is used for that clone only (see Private repositories).
- Talk to plugins, or anything on the network other than an allowlisted git
  host (and the Docker daemon's own image pulls).
