# Plugin packaging: external plugins

An **external plugin** lives in its own git repository, not in this one. The
owner installs it into a running broker from the console. The owner opens
Plugins, clicks **+ Add plugin** and enters the source, for example
`github.com/you/aab-plugin-name@v0.1.0`. The owner reviews what the plugin
asks for and clicks Install. Then the opt-in installer container builds and
starts it. The finance plugin (`pelegw/aab-plugin-finance`, private) is the
reference package.

Nothing about authority changes:

- The broker registers a plugin only when the plugin service offers exactly
  the manifest that the owner approved.
- That approved copy is the **pin**. The broker stores it in the
  `plugin_pins` table, not in a vendored file.
- No plugin changes an engine file.
- Each plugin keeps its own network, volumes and token.

This document covers the side of the contract that the plugin author owns:

- The repository layout.
- The descriptor.
- The base image.
- What the installer writes, and what that makes sure of.
- Versioning.
- Install, upgrade and remove.
- How to develop against a gateway checkout.

The operator's side is in `docs/deployment.md`. It covers how to turn on the
installer, backups and the acceptance test. The console flow is in
`docs/console.md`.

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

Write the adapter exactly as for an in-tree plugin. It is an
`aab_plugin_runtime` adapter class and a `main.py` with a `create_app()`
factory that calls `serve(...)`. See `plugins/github/` and
`docs/plugin-api.md`. The manifest follows `docs/manifest-schema.md`. The
broker validates it with its own strict schema before the owner can pin it.

## The descriptor: `aab-plugin.yaml`

The descriptor uses schema 1. `installer/aab_installer/descriptor.py`
validates it with pydantic and strict types, and rejects unknown keys. Each
rule below has a rejecting test in `installer/tests/test_descriptor.py`. This
is the descriptor of the finance plugin:

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
| `service` | string | `^[a-z][a-z0-9]{1,31}$` (one lowercase word, no `-` or `_`), and not a name the stack already uses: `broker`, `edge`, `caddy`, `installer`, `sidecar`, `internal`, `default`, `whatsapp`, `wa`, `github`, `google`, `plugin`, `plugins`. Every other name comes from it (below). |
| `plugins` | list of strings, 1 to 16 | Each `^[a-z][a-z0-9]*$` (the broker's manifest id rule), unique. Must be exactly the ids the service's `GET /manifests` offers. |
| `manifests` | list of strings, 1 to 16 | One per plugin id, in the same order. Each a relative POSIX path inside the repository (`[A-Za-z0-9_][A-Za-z0-9_.-/]*`, at most 200 characters, no `..`, no empty segment) ending in `.yaml` or `.yml`, unique. |
| `runtime` | string | `0.3` or `0.3.0` style (quote it: `"0.3"`, strict types reject a YAML float). Informational: the base image pins the runtime. |
| `build` | mapping | Only `dockerfile` (default `Dockerfile`), a relative path like the manifest paths. The build context is always the repository root. |
| `volumes` | mapping, at most 8 | Name to absolute mount path. Names match `^[a-z][a-z0-9_]*$` and start with `<service>_` (a plugin names only its own volumes; service names have no underscore, so the prefix names exactly one service). `<service>_secrets` is the installer's (mounted at `/secrets`). Mount paths are absolute (`/[A-Za-z0-9_.-/]*`, no `.` or `..` segment), not `/`, not `/secrets` or anything below it, and unique. |
| `environment` | mapping, at most 32 | Names `^[A-Z][A-Z0-9_]{0,63}$`, never starting with `PLUGIN_` (the installer sets those) and never `TZ`, `LOG_LEVEL` or `LOG_FORMAT` (use `env_passthrough`). Values are literal strings of at most 1024 characters with no `$` (compose would interpolate it from the `.env` that holds every service's secrets) and no control character. |
| `env_passthrough` | list of strings | A subset of `TZ` (default `UTC`), `LOG_LEVEL` (`INFO`) and `LOG_FORMAT` (`text`), unique: the host `.env` keys that the plugin can read, written as `${KEY:-default}`. |

The file must be UTF-8 and at most 64 KiB. When the installer reads a
package, it also checks these items:

- The Dockerfile exists and is at most 1 MiB.
- Each manifest is at most 256 KiB of UTF-8 YAML.
- The `id` of each manifest is the plugin id at the same position in
  `plugins`.
- The `version` of each manifest is `MAJOR.MINOR.PATCH`.

The installer checks real files only. It rejects a symlink anywhere on a
path. Git checks out symlinks as plain files anyway. Then the broker
validates each manifest fully (`plugins/manifest.py`) before it shows the
review.

These names come from `service` (here `finance`):

| What | Name |
|---|---|
| Compose service | `plugin-finance` |
| Its network (shared with the broker only) | `net_finance` |
| Its encrypted secret store | volume `finance_secrets` at `/secrets` |
| Its token, in the host's `.env` | `PLUGIN_TOKEN_FINANCE` (the broker presents it as `X-Plugin-Token`) |
| Its secret-store key, in the host's `.env` | `PLUGIN_SECRETS_KEY_FINANCE` |
| Where the broker finds it | `PLUGIN_URL_FINANCE=http://plugin-finance:8090` |
| Its files on the host | `plugins.d/finance/` (`src/`, `compose.yml`, `newrelic.yml`, `install.json`) |

## What the installer renders, and what that guarantees

A plugin repository cannot supply compose YAML. The installer writes
`plugins.d/<service>/compose.yml` from the validated descriptor. It uses a
fixed template, `installer/aab_installer/overlay.py`. The golden file of the
template is `installer/tests/golden/finance.compose.yml`. This is the overlay
for the finance plugin:

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

Thus every external plugin gets the same shape as the in-tree plugins, by
construction. For the in-tree plugins, `broker/tests/test_config_files.py`
enforces these invariants. For external plugins, the template and
`installer/tests/test_overlay.py` enforce them:

- Exactly one network, its own, shared with the broker only. The plugin
  cannot reach another plugin, the installer or the edge. Only the broker
  can reach the plugin.
- No published port and no bind mount, so the plugin reaches no file
  outside its volumes.
- Only the named volumes that it declared, plus its secret store. The names
  of all of them start with its own service name.
- Only its own token and secret-store key, under the generic names of the
  runtime. If `.env` does not hold them, compose does not start the plugin
  service. It never starts it open.
- No extra Linux capability and no privilege escalation.
- The rotated `json-file` logging that every service uses, and
  `restart: unless-stopped`.

Beside the overlay, the installer writes `plugins.d/<service>/newrelic.yml`.
It changes one thing: the plugin's logging goes to the New Relic log shipper,
like the logging of every other service. `scripts/compose-files.sh` loads it
only when `NEWRELIC_ENABLED=true` (`docs/logging.md`). The installer builds
it from the service name alone. Its golden file is
`installer/tests/golden/finance.newrelic.yml`.

## The base image

`plugins/base/Dockerfile` builds `ghcr.io/pelegw/aab-plugin-base:<version>`
and `:latest`. `.github/workflows/release.yml` publishes the image when the
gateway gets the tag `v<version>`. CI builds the image on every push, but
does not push it. CI also checks the contract below inside the image. The
image gives:

- Python 3.12 (`python:3.12-slim`) and `WORKDIR /srv`.
- `aab-plugin-runtime` from the `plugin-runtime/` directory of the gateway
  at that release, and `uvicorn`.
- The unprivileged user `aab`, uid/gid 10001. Every image in the stack uses
  it.
- `/secrets`, owned by `aab`, mode 0700, declared as a `VOLUME`.
- `PLUGIN_SECRETS_DIR=/secrets`, `PYTHONDONTWRITEBYTECODE=1` and
  `PYTHONUNBUFFERED=1`.
- A TCP liveness check on `:8090`, and `EXPOSE 8090`. Every plugin API route
  requires the token, and a healthcheck must not hold it.
- `USER aab`, and **no `CMD`**. The plugin names its own app factory.

The same release attaches the runtime wheel
(`aab_plugin_runtime-<version>-py3-none-any.whl`) to its GitHub Release. Use
it for development installs without a gateway checkout.

GHCR packages of a private repository are private. On the server that runs
the broker, run `docker login ghcr.io` one time with a token that has
`read:packages`. Then run `docker pull ghcr.io/pelegw/aab-plugin-base:<version>`
for each base version that your plugins name. The installer controls the
Docker daemon of the server, but it holds no registry credentials of its own.
Thus its builds use the base image from the image store of the daemon
(`docs/deployment.md`). On a development machine, `docker login ghcr.io` is
sufficient for your own `docker build`. A plugin build takes only the base
image from the gateway. Thus a plugin never needs access to the gateway
repository itself.

### Dockerfile template

This is the Dockerfile of the finance plugin, reduced to what every plugin
needs:

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

The `pyproject.toml` of the plugin names the runtime **by version**, never by
URL. Use `"aab-plugin-runtime>=0.3.0,<1"`. The finance plugin accepts
`>=0.2.0,<1`. Pip always resolves a direct-URL dependency again, even when the
image already holds the runtime. Thus a URL makes the image build clone the
gateway. The gateway is private, so that clone fails. For a standalone
install outside the image, give the URL as an extra:

```toml
[project.optional-dependencies]
runtime = ["aab-plugin-runtime @ git+https://github.com/pelegw/agent-authority-broker@v0.3.0#subdirectory=plugin-runtime"]
```

## Versioning

- Release the plugin with a `vMAJOR.MINOR.PATCH` tag. The installer accepts a
  release tag or a full 40-hex commit. It never accepts a branch, because a
  branch can move under a reviewed install. It also rejects a `v1.2.3` that
  resolves to a branch and not to a tag.
- **Bump the manifest's `version` whenever the manifest changes.** The broker
  pins id and version. If a plugin service offers a different version than
  the pin, the broker rejects it and shows it for review (Offered, awaiting
  review). The broker never uses a changed manifest under the same version,
  because it serves its pinned copy, not the copy of the plugin.
- `FROM ghcr.io/pelegw/aab-plugin-base:<x.y.z>` names the gateway release
  that you built and tested the plugin against. `runtime: "x.y"` in the
  descriptor says the same for a reader. The release workflow publishes the
  base image and the runtime wheel for every gateway tag. The runtime stays
  on the `x.y` line of the gateway. The release workflow rejects any other
  runtime version.

## Install, upgrade, remove

The owner's side is in `docs/console.md`. This is what the broker and the
installer do:

1. **Inspect.** The broker asks the installer to clone the source at the
   ref. The source must be on the allowlist, `INSTALLER_ALLOWED_SOURCES`. The
   installer reads the descriptor and the manifests. It returns them with the
   resolved commit. The broker validates every manifest and shows the
   review. The review shows:
   - Each action, with its side effect and modes.
   - The resources, narrowings and constraints.
   - The settings that the plugin will ask for, and which of them are secret.
   - The volumes, the environment and the passthrough.
   - On an upgrade, the diff against the current pin.
2. **Install.** The broker inspects again. It rejects the install if the ref
   no longer resolves to the reviewed commit. It **pins every manifest**
   (audited `plugin.pin`). Only then does it ask the installer for the job
   (`plugin.install`). The job does these steps:
   - It fetches the reviewed commit into `plugins.d/<service>/src`.
   - It makes sure that `.env` holds the token and the secret-store key of
     the plugin service. `scripts/init_secrets.py --rotate` generates them.
   - It writes the overlay, its New Relic logging override and `install.json`.
   - It runs `docker compose ... up -d --build plugin-<service>`.
   - It runs `up -d broker`. This recreates the broker with its new
     environment and network, but never rebuilds it.

   If a step fails, the job rolls the files back. Thus a broken overlay never
   stays in the set of compose files. The broker comes back, discovers the
   plugin service and finds the pin. The card of the plugin appears
   **disabled**. The owner enables it as any other plugin.
3. **Upgrade.** The same steps, for an installed plugin service from the
   same source. Another repository under the same service name is a remove
   and then an install, each with its own review. The broker pins the new
   manifests first. Then the installer rebuilds the plugin service and
   recreates the broker. If an upgrade fails, the installer restores the
   previous checkout and overlay and starts them again. The broker then
   offers the old manifest for review, because the pin already moved. Pin it
   there to restore the plugin. If the installer rejects the upgrade request
   because it is busy or down, the broker puts the old pins back at once.
4. **Remove.** The installer stops and deletes `plugin-<service>`. It
   deletes `plugins.d/<service>/`. It recreates the broker without the
   plugin service and removes its network. The broker then unpins every
   plugin of that plugin service. Agents get 404 at once. The plugin rows
   stay, disabled. Without purge, the installer keeps the volumes and the
   two `.env` secrets of the plugin service. It comments out the secrets as
   `#aab-retired# PLUGIN_TOKEN_<SERVICE>=...`. Thus a later install of the
   same service restores the same secret-store key, and its data still
   decrypts. With purge, the installer deletes the `<service>_*` volumes and
   both secrets permanently.

One job runs at a time. The state and the log lines of a job live in
`plugins.d/_installer/jobs/<id>.json`. The log lines never hold a token. The
file survives restarts of both the installer and the broker. Thus the
console can follow a job through the restart of the broker.

## Private repositories: the GitHub token in the console

The installer clones anonymously over https, unless the owner stored a
**read-only** GitHub token in the console. The field is in Plugins,
**+ Add plugin**: "GitHub token for private plugin repositories". It has Set
and Clear, and a state badge: not set, set, or re-enter required. Make the
token in one of these two ways:

- Fine-grained (preferred). Resource owner = the owner of the plugins.
  Repository access = only the plugin repositories. Permissions = Contents:
  read-only. Metadata: read-only comes with it.
- A classic token with the `repo` scope. It reads every repository that the
  account can read, so prefer fine-grained.

The token is not in `.env`. Like the Telegram bot token, it is a third-party
credential. Thus the broker stores it encrypted under `BROKER_SECRETS_KEY`.
It is write-only: no route returns it. The installer holds no copy of its
own. The broker sends the token in the body of each inspect, install and
upgrade request (`git_token`). The installer uses it for the clone of that
request, or for the single clone at the start of the job. The installer
writes it nowhere:

- Not in the job record.
- Not in `install.json`.
- Not in a job log line.
- Not in a log line.

Git gets the token only through `GIT_ASKPASS`. The installer writes a fixed
script for this, and the script holds no secret. The script prints the token
from the environment of the one clone or fetch that connects to the remote.
The token is never part of a URL or an argument. Thus no git error, process
listing or job log can carry it. The installer gives the token only when the
source is on `github.com`. It checks the allowlist first. The script answers
only the credential prompts of `github.com`, so a redirect to another server
gets nothing. The installer masks the value of the token in job log lines,
whatever its shape. The redaction backstop of every service masks URL
credentials and the field names `git_token`, `installer_git_token` and
`installer_token`.

Rotate the token at GitHub. Then paste the new token in the same field
(Replace). If `BROKER_SECRETS_KEY` changes, the field shows "re-enter
required". Until you paste the token again or clear it, inspect, install and
upgrade answer 409. No change needs a restart.

The Docker build of the plugin does not need the token. The base image
already holds the runtime, and the build context is the clone that the
installer made.

## Developing against a gateway checkout

- **Unit tests** need only the runtime. Set `AAB_SRC` to a gateway checkout.
  From the plugin repository, run
  `pip install -e "$AAB_SRC/plugin-runtime" -e ".[dev]"`. As an alternative,
  install the runtime wheel from the gateway release. Then run
  `python -m pytest`.
- **Broker-level tests** cover pinning, the capability algebra, MCP tools
  and hidden resources against the real engine. Install `broker` from the
  checkout with `pip install -e "$AAB_SRC/broker[dev]"`. Register the adapter
  in process with
  `Registry(vendored_dirs=(<a directory holding <id>/manifest.yaml>,))`. The
  tests of the gateway use the same seam (`broker/tests/conftest.py`). The
  finance plugin keeps these tests in `tests/integration/`. They skip when
  `AAB_SRC` is absent.
- **The image**: run `docker login ghcr.io` one time. Then run
  `docker build .` in the plugin repository.
- **The whole install path, locally**: do these steps.
  1. In a gateway checkout, set `INSTALLER_ENABLED=true` and
     `INSTALLER_ALLOWED_SOURCES=github.com/<you>/*`.
  2. Set `AAB_HOME` to the absolute path of the checkout, as the Docker
     daemon sees it. The installer mounts it at the same path on both sides.
  3. Pull the base image into the daemon, as on a server.
  4. Run `docker compose $(scripts/compose-files.sh) up -d --build`.
  5. Tag a test release of the plugin, or use a full commit.
  6. Install the plugin from the console.

  The acceptance test in `docs/deployment.md` goes through all of it.
- **CI for a plugin repository**:
  - Run the unit tests with the runtime installed from the gateway tag. If
    the gateway repository is private, this needs a token that can read it.
  - Run the integration tests with a gateway checkout at that tag.
  - Run `docker build` after `docker login ghcr.io`. The base image is
    private, as the gateway is.

## What the installer never does

- Load compose YAML, scripts or configuration from a plugin repository. The
  overlay comes from the template, and the `.env` entries come from
  `scripts/init_secrets.py`. The only part of the plugin that runs is its
  Dockerfile, inside `docker build`, like any image.
- Clone a source that is not in `INSTALLER_ALLOWED_SOURCES`. This setting is
  env-only and fails closed: an empty list rejects all sources.
- Clone over a protocol other than https, or at a ref other than a tag or a
  full commit.
- Install a commit other than the one that the owner reviewed.
- Decide authority. The broker pins, and it serves a plugin only when the
  plugin offers exactly the pinned manifest.
- Keep a git credential. The GitHub token comes with the one request that
  needs it. The installer uses it for that clone only (see Private
  repositories).
- Connect to plugins, or to anything on the network other than an
  allowlisted git server and the image pulls of the Docker daemon.
