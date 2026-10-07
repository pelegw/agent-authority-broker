# Logging

Every service writes one line per event to its own stdout. Docker keeps those
lines and rotates them, and `docker compose logs` reads them. Logs are the
**operational** trail: what the broker and the plugins did, how long it took,
and what failed. The **accountability** trail does not change. It is the
hash-chained decision record and the audit table. The decision record holds
every decision and outcome, with its authority chain. The audit table holds
every human action. The two trails meet on the **request id**. Each log line
carries the id of the request or background job it belongs to. The decision
rows of that request carry the same id in `request_id`.

## Who logs what

| Service | Logger names | Lines |
|---|---|---|
| `broker` | `broker.*` | One access line per HTTP request (`broker.access`); one line per decision and per outcome (`broker.engine`); the owner's actions (login, tokens, keys, grants, approvals, plugin configuration, settings); agent refusals; action lifecycle (created, approved, delivered, deferred, failed, held); delegations; plugin discovery and health changes; plugin pins and installs (package inspected, install / upgrade / remove requested or refused, installer unreachable or refusing, with source, ref, commit, service, plugin ids and job id; the installer's GitHub token stored or cleared, by name only); Telegram; the scheduler; boot. |
| `plugin-whatsapp`, `plugin-github`, `plugin-google` | `aab_plugin_runtime.*`, `aab_plugin_<name>.*` | One access line per plugin API call (`aab_plugin_runtime.access`); one line per `/perform` (action, status, duration); configure (field names), connect, disconnect, the pairing QR served; secret-store writes (slot and field names); per plugin: each sidecar call (WhatsApp), each minted credential by scope (GitHub installation tokens, Google access tokens: cached or fresh), the target API's refusals by status class. |
| `aab-installer` (installer overlay) | `aab_installer.*` | One access line per API call from the broker (`aab_installer.access`, actor `broker`); refused calls (bad or missing `X-Installer-Token`, with the path and whether the header was present); each package inspected (source, ref, commit, service, plugin ids); each job queued, done or failed (job id, kind, service, source, ref, duration, the exception class); the ready line (version, `AAB_HOME`, the allowed sources, `git_auth=per_request`: the installer holds no GitHub token, the broker sends one with each request that needs it). The job's own log, shown in the console, is separate: see [The installer's job logs](#the-installers-job-logs). |
| `whatsapp-sidecar` | Go `log` | One request line per API call (method, path, status, duration, request id); send results (message id only); QR events; connection state changes; whatsmeow's own lines. |
| `edge` (public overlay) | Caddy | Caddy's own log, unchanged. |
| `audit-exporter` (New Relic overlay) | `broker.audit_export` | On stdout, one JSON object per new row of the audit record: this is the export itself. On stderr, one line per run (`audit export done hash_resources=<true|false> decisions=<n> audit_log=<n>`), or the reason a run failed. See [Shipping logs and the audit record to New Relic](#shipping-logs-and-the-audit-record-to-new-relic). |
| `log-shipper` (New Relic overlay) | Fluent Bit | Its own start, connection and retry lines. They stay on this server, in a rotated `json-file` log. |
| `uvicorn` | `uvicorn`, `uvicorn.error` | Server start and stop, through the same handler and format. uvicorn's own access log is off: see [The access line](#the-access-line). |

The `aab` CLI logs to **stderr**, at WARNING unless `LOG_LEVEL` sets a
different level. Its stdout is its output. `aab simulate`, `aab skill build`
and `aab audit export` run broker code in-process. The lines of
`aab audit export` carry the service name `audit-exporter`.

## Format

Text (the default, `LOG_FORMAT=text`):

```
2026-09-24T20:29:08.839Z INFO broker.access [broker smoke-run-1] request method=GET path=/v1/targets status=200 duration_ms=42 actor=key:smoke-agent ip=172.19.0.1
2026-09-24T20:29:08.879Z INFO broker.engine [broker d7234be1ab5f458a93995513a27fadac] decision decision=deny reason=target_unavailable status=404 target=whatsapp action=list_chats key=smoke-agent resource=- chain=0 row=1 approved_by=-
2026-09-24T20:31:35.601Z INFO aab_plugin_whatsapp.sidecar [plugin-whatsapp smoke-health-1] sidecar call method=GET path=/status status=200 error=- duration_ms=20
```

`<UTC timestamp, ms>Z <LEVEL> <logger> [<service> <request id>] <message>`.
The service is `broker`, `plugin-<service>` or `installer`. The request id is
`-` for lines outside any request, for example boot or the scheduler's idle
ticks.

The message is a fixed text followed by `key=value` pairs (logfmt). These
rules apply to a value:

- A value is bare when it has no space, quote, `=`, backslash or control character.
- Any other value goes in double quotes, with those characters escaped.
- A list becomes one comma-joined value.
- An empty value is `-`.
- The formatter cuts a value longer than 200 characters.

Thus a value can never end the line, fake a second pair or fake a second line.
Examples of such values are a key name an agent chose and a resource id.

JSON (`LOG_FORMAT=json`) gives one object per line with the same fields, for a
log collector:

```json
{"ts": "2026-09-24T20:29:08.839Z", "level": "INFO", "logger": "broker.access", "service": "broker", "request_id": "smoke-run-1", "message": "request method=GET path=/v1/targets status=200 duration_ms=42 actor=key:smoke-agent ip=172.19.0.1"}
```

The object also has `exc` (the traceback) when there is one. The JSON is
ASCII-escaped, so a line is always one line.

The sidecar writes Go's standard format with UTC microseconds
(`2026/09/24 20:31:35.601156 request method=GET path=/status status=200
duration_ms=0 request_id=smoke-health-1`). It uses the same key=value style.
It has no JSON mode.

## Request ids and the decision record

- **Every HTTP request** gets an id. It is the caller's `X-Request-Id` when
  that is well formed (`[A-Za-z0-9._-]{1,128}`), else a fresh
  32-hex-character id. The broker rejects ids that start with `sched-`,
  `tg-` or `inst-` and generates one instead. Those prefixes belong to its
  own background jobs, so no caller can make its calls look like the
  scheduler's.
  The `X-Request-Id` response header echoes the id, so an agent can quote it.
- **The decision record uses it**: `engine.new_request_id()` returns the
  current request's id. So the `request_id` of a decision row is the id on
  the log lines of that request. It is a correlation id, not a unique key. An
  agent that sends the same `X-Request-Id` twice gets two sets of rows with it.
- **It crosses every hop**: the broker sends it to the plugin service on
  every plugin call. That is `/perform`, and also `/normalize`, `/resolve`,
  `/status` and the rest. The plugin runtime runs the call under it.
  plugin-whatsapp forwards it to the sidecar, which puts it on its request
  line. Thus one agent call is one id in the broker, the plugin, the sidecar
  and the decision record.
- **Background work** runs under ids of its own:
  - Each scheduler tick runs under a `sched-<hex>` id.
  - Each delivery in a tick runs under a fresh `sched-<hex>` id. The
    per-delivery line names its tick.
  - Each Telegram update runs under a `tg-<hex>` id. A tap's approval, the
    delivery it triggers and the decision rows all carry it.
  - Each tick of the installer sync runs under an `inst-<hex>` id. It sends
    the id to the installer, so its `GET /services` and `GET /jobs` lines
    carry it too.
  - A console approval runs under the id of the console request.

To follow one call:

```bash
docker compose logs --no-log-prefix | grep smoke-run-1
```

## The access line

`request_log.py` writes one line per request, after the response. The broker
and the plugin runtime use the same middleware:

```
request method=POST path=/v1/targets/echo/actions/list_items status=200 duration_ms=12 actor=key:reader ip=172.19.0.1
```

- `path` is the raw path **without the query string, always**. The OAuth
  callback's query carries the authorization code and the state nonce. An
  agent's GET query carries its params. The middleware never reads the query
  string at all.
- `actor` is the caller that authenticated:
  - `key:<name>`: an agent key.
  - `monitor:<token name>`: a monitor token on `/health`.
  - `owner:<username>`: a session or admin token.
  - `plugin:<service>`, in a plugin service: the broker, which holds the
    token of that service.
  - `-`: none of these.
- `ip` is the client address the origin guard trusts. That is
  `CF-Connecting-IP` behind Cloudflare, else the socket peer.
- 5xx lines are WARNING. The middleware writes no line for health probes
  (`/health`, `/v1/health`) or for the owner's monitoring summary
  (`/v1/admin/health`).

uvicorn's own access log is **off**, structurally. uvicorn writes its access
line only when the `uvicorn.access` logger has a handler path. That is all
that `--no-access-log` changes. The logging setup gives that logger no handler
path, so uvicorn never writes the line, with or without the flag. Its line
would duplicate our line and carry the query string. The images also pass
`--no-access-log`.

## Never logged, and the backstop

Log lines never carry these values:

- Params or results of an action.
- Message bodies.
- Notes.
- Resource labels. Log lines carry ids, not names.
- Cookie values.
- `Authorization` headers.
- Passwords.
- The setup token.
- Agent keys.
- Admin tokens.
- Plugin tokens.
- The sidecar token.
- The Telegram bot token or a link code.
- OAuth codes.
- Client secrets.
- Refresh or access tokens.
- Installation tokens.
- Private keys.
- Fernet keys.
- `INSTALLER_TOKEN`.
- The installer's GitHub token.

When the name of a value is useful, the log line carries the name, never the
value: `secret_fields=client_secret`, `secrets_set=setup_token`,
`installer github token stored`. When exception text can quote input, the log
line carries only the exception's class.

The tests prove it. `tests/targets/test_secrets_in_logs.py` sets every logger
to DEBUG and drives these flows:

- Setup and login.
- Admin tokens.
- Key create, rotate and use.
- Delegation.
- The Telegram token.
- The installer's GitHub token: stored, then sent with an inspect.
- Plugin configure.
- A Google connect with minted tokens.
- A GitHub App install with minted tokens.

The test fails if any secret value appears in any record or in the handler's
output. The plugins' own suites do the same for their credentials.

A **redaction backstop** sits on the handler (`RedactSecrets`, the pattern
table `SECRET_PATTERNS` in `logging_setup.py`). It replaces each of these
shapes with `<redacted>`, in the message, the traceback and the stack:

- An agent key (`aab_` + 48 hex).
- An admin token (`aab_admin_` + 48 hex).
- A `Bearer <token>`.
- A Telegram bot token.
- A Google access or refresh token or client secret.
- A GitHub token (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`).
- A Fernet key.
- A PEM block.
- A session cookie.
- Credentials inside a URL (`https://user:token@host`, `https://token@host`).
- The value after a secret-bearing field name (`password=`,
  `client_secret=`, `X-Plugin-Token:`, `X-Installer-Token:`, `git_token=`,
  `installer_git_token=`, `installer_token=`, ...).

The backstop edits a copy of the record, so a test's capture still sees what
the code logged. It is a backstop, not the mechanism. It cannot tell a
64-hex-digit secret from a SHA-256 by its shape. Those secrets are the plugin
tokens, the sidecar token and the signing key. The code keeps them out of the
log because it never logs them.

The setup and the backstop are one module, `logging_setup.py`, with
`request_log.py` for the access line. The repository keeps **three
byte-identical copies** of them: `broker/broker/`,
`plugin-runtime/aab_plugin_runtime/` and `installer/aab_installer/`. The three
packages must not depend on each other. `broker/tests/test_logging_setup.py`
fails if the copies differ, so a new pattern goes into all three copies.

Some third-party loggers stay at WARNING whatever `LOG_LEVEL` says. At lower
levels, they log secrets or content:

- `httpx` logs request URLs with their query strings: a Gmail search, the bot
  token in the Telegram API path.
- `mcp` logs protocol payloads: tool arguments.
- Any third-party logger that logs form fields gets the same limit.

## The installer's job logs

An install, upgrade or remove is a job, and its log is for the owner. The
console's job panel shows it. The installer keeps it in
`plugins.d/_installer/jobs/<id>.json` (root-owned, 0700 directory) across
restarts of the installer and the broker. The log holds these items:

- The steps.
- The commands that ran. Their arguments carry no secret: they are the compose
  file list and service names.
- The exit codes of those commands.
- The last 15 non-empty lines of the output of each command.

Every line goes through the same redaction backstop. Then two more
replacements apply:

- Every run of 64 hex digits becomes `<hex64>`. That is the shape of every
  plugin token. The cost is that image digests and the broker's container
  id (in `docker network connect`) become `<hex64>` too.
- Every secret value the job received becomes `<redacted>`, whatever its
  shape. That is `INSTALLER_TOKEN`, every `PLUGIN_TOKEN_<SERVICE>` in `.env`
  when the job was asked for, and the GitHub token its request carried.
  The installer holds that token in memory for that job only and never saves
  it.

The installer's `GET /services` answer carries each installed service's
`PLUGIN_TOKEN_<SERVICE>`. No log line on either side carries it: the
installer's access line has the path only, and the broker logs service
names only. A sync problem (an installer that is down, or an answer the
broker refuses) is logged once, with its reason, not on every tick.

A line has at most 500 characters, and a job at most 300 lines. The log keeps
the first 20 and the last 280 lines. git's own output never gets into the log.
A failed clone reports only the step and git's exit code.

## Levels

Every Python service reads `LOG_LEVEL` and `LOG_FORMAT` once, at start:

- `LOG_LEVEL` is `DEBUG`, `INFO`, `WARNING` or `ERROR`. The service also
  accepts `WARN` and lower case. The default is `INFO`.
- `LOG_FORMAT` is `text` or `json`.

When the service does not recognise a value, it logs a WARNING and falls back
to `INFO` or `text`. The sidecar also reads `LOG_LEVEL`. It sets the level of
whatsmeow, and `WARNING` or `ERROR` drop its per-request lines.

- **INFO**: every line this document describes. Idle background work is
  silent: a scheduler tick logs only when something was due.
- **WARNING**: rejections and failures worth a look:
  - Agent and admin authentication failures, with the reason class
    (`missing`, `malformed`, `admin_token`, `unknown_key`, `chain:expired`,
    ...).
  - Rate-limited logins.
  - Budget rejections, which name the exhausted grant.
  - 5xx outcomes.
  - A plugin service that the broker cannot reach.
  - A failed health check.
  - Telegram poll errors (class, status and backoff).
  - Requests that did not come through the edge.
- **ERROR**: a broken decision chain on verify, and unexpected failures.
- **DEBUG**: adds one line per broker-to-plugin call.

These are environment settings, not console settings (`docs/configuration.md`).
They are process-level, and each service reads them before it opens any
database. The plugin containers have no console. Set them in `.env`.
`scripts/init_secrets.py` writes the defaults. Then restart with
`docker compose up -d`.

## Rotation

`docker-compose.yml` and the public overlay give every service the same
logging block through an `x-logging` anchor:

```yaml
x-logging: &logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "5"
```

Each container keeps at most five 10 MB files, about 50 MB in all.
Docker deletes the oldest file. Change the anchor to keep more or less.
Container logs are not a backup. Keep the decision record and the audit table,
which are in `broker.db`.

The New Relic overlay changes the driver of every service but the shipper to
`fluentd`. Docker's dual logging then keeps the local copy. Its defaults are
five files of 20 MB per container, compressed.

## Reading

```bash
docker compose logs -f --since 10m broker            # follow one service
docker compose logs --since 1h --no-log-prefix | grep <request-id>
docker compose logs --no-log-prefix broker | grep -E ' (WARNING|ERROR) '
# with LOG_FORMAT=json:
docker compose logs --no-log-prefix broker | jq -c 'select(.level != "INFO")'
```

In public mode add `-f docker-compose.yml -f docker-compose.public.yml`
(deploy/DEPLOY.md > Operations > Logs).

## Shipping to a collector

Keep the services writing to stdout, and change the Docker **log driver**
instead. Replace the `x-logging` anchor with a different driver and its
options, for example `driver: local` (compressed local files), `journald`,
`syslog`, `fluentd`, `gelf` or `awslogs`. Set `LOG_FORMAT=json`. Then the
collector receives one JSON object per line, with `ts`, `level`, `logger`,
`service`, `request_id` and `message` as fields. `docker compose logs` reads
`json-file`, `local` and `journald` directly. With other drivers, Docker's
dual logging keeps a local copy for it (Docker 20.10 and later).

For New Relic, do not edit the anchors. The opt-in overlay in the next
section does all of it, the audit record included.

## Shipping logs and the audit record to New Relic

The New Relic overlay sends two streams off this server to New Relic Logs:

- **The operational logs**: every line of every service, as above.
- **The audit record**: every row of the `decisions` and `audit_log` tables
  of `broker.db`, one log event per row.

Then an agent elsewhere can query both with NRQL. The overlay is off by
default. Nothing leaves the server until the owner turns it on.

### What the overlay adds

`docker-compose.newrelic.yml` adds two services and changes one setting of
every service:

- **`log-shipper`** runs Fluent Bit (`fluent/fluent-bit`, a pinned version).
  Its configuration is in `ops/fluent-bit/`. It is the only container that
  receives `NEW_RELIC_LICENSE_KEY`. It is alone on the network `net_logs`. It
  mounts its configuration read-only and nothing else.
- **`audit-exporter`** runs `aab audit export --loop` from the broker image.
  It mounts `broker_data` read-only and has no network at all. It holds no
  New Relic credential. Its stdout is the export.
- **Every service** logs through Docker's `fluentd` driver to
  `127.0.0.1:24224`, the shipper's only published port. The options are
  `fluentd-async: "true"` and `tag: aab.{{.Name}}`.

The shipper's own log stays local, in a rotated `json-file` log. Sent
through itself, its errors about New Relic would loop back into New Relic.

A compose override cannot name a service that no loaded file defines. Thus
three services get their override from a file of their own:

| Service | Override file | Loaded when |
|---|---|---|
| `edge` | `ops/newrelic/public.yml` | `SITE_DOMAIN` is set |
| `aab-installer` | `ops/newrelic/installer.yml` | `INSTALLER_ENABLED=true` |
| `plugin-<service>` (each installed plugin) | `plugins.d/<service>/newrelic.yml` | the installer wrote it |

`scripts/compose-files.sh` adds these files after all other files, and only
together with the file that defines the service. The installer writes
`newrelic.yml` on every install and upgrade, whatever the setting.

**A plugin installed before the installer wrote `newrelic.yml`** has no such
file. Its logs stay local until its next upgrade. To ship them now, write
the file once for each such service. The command uses the installer's own
template, so a later upgrade writes the same bytes:

```bash
C="docker compose $(scripts/compose-files.sh)"
$C exec aab-installer python -m aab_installer.render_newrelic <service>
C="docker compose $(scripts/compose-files.sh)"
$C up -d plugin-<service>
```

- Set `C` again after the command. Only then does the file set name the new
  file.
- To write the file for every installed plugin, use `--all` in place of
  `<service>`. Then run `$C up -d`.
- The command writes `plugins.d/<service>/newrelic.yml` and nothing else. It
  refuses a service that is not installed.
- The `exec` line needs the installer to run (`INSTALLER_ENABLED=true`). If
  it is off, use this line in its place:
  `docker compose -f docker-compose.yml -f docker-compose.installer.yml run --rm --no-deps aab-installer python -m aab_installer.render_newrelic <service>`.

### Enabling it

1. Get the ingest license key. In New Relic, open your user menu, then
   API keys. Copy the key of type INGEST - LICENSE.
2. Set these lines in `.env`:

   ```
   NEWRELIC_ENABLED=true
   NEW_RELIC_REGION=US
   NEW_RELIC_LICENSE_KEY=<the key you copied>
   LOG_FORMAT=json
   ```

   Use `NEW_RELIC_REGION=EU` for an account in the EU data center. Write it
   in capitals.
3. Optional: set `AUDIT_EXPORT_HASH_RESOURCES=true` (see
   [What leaves the server](#what-leaves-the-server)). Set
   `AUDIT_EXPORT_INTERVAL` to export more often than once an hour.
4. Apply it:

   ```bash
   C="docker compose $(scripts/compose-files.sh)"
   $C up -d --build
   $C logs log-shipper
   ```

`NEWRELIC_ENABLED` must be exactly `true`, as `INSTALLER_ENABLED` must.
`LOG_FORMAT=json` is not required. But with it, New Relic receives `level`,
`logger`, `service`, `request_id` and `message` as attributes. A text line
arrives as one `message` only.

To turn shipping off, set `NEWRELIC_ENABLED=false`. Then run
`$C up -d --remove-orphans`, with `C` set again. Compose recreates every
service with its local `json-file` log and removes the two New Relic
containers.

### The license key

The key is the one third-party credential in `.env`
(`docs/configuration.md`). Fluent Bit reads it from its environment when it
starts. No broker code holds it, so the console cannot take it.

- Compose hands it to `log-shipper` alone. A test enforces this.
- The overlay requires it (`${NEW_RELIC_LICENSE_KEY:?...}`). With the overlay
  on and no key, compose refuses to start anything.
- `scripts/init_secrets.py` writes it empty and never generates or rotates
  it.
- Nothing logs it. The shipper's configuration names it as
  `${NEW_RELIC_LICENSE_KEY}` only.
- An ingest key can only send data. It cannot read anything back.

To rotate it, create a new ingest key in New Relic and put it in `.env`.
Then run `$C up -d log-shipper` and delete the old key in New Relic.

### Region

`NEW_RELIC_REGION` picks the shipper's configuration file:

| Value | File | Endpoint |
|---|---|---|
| `US` (the default) | `ops/fluent-bit/region-US.yaml` | `https://log-api.newrelic.com/log/v1` |
| `EU` | `ops/fluent-bit/region-EU.yaml` | `https://log-api.eu.newrelic.com/log/v1` |

Any other value names no file, and Fluent Bit does not start.
`$C logs log-shipper` then shows `could not open configuration file`. The two
files differ only in the endpoint, and a test keeps them so. Fluent Bit's New
Relic output is named `nrlogs`.

### What leaves the server

- Every log line of every service, after the five steps of
  `ops/fluent-bit/pipeline.yaml` (below). The lines carry no secret, params,
  message text, note or label ([Never logged](#never-logged-and-the-backstop)).
  But they do carry identifiers: resource ids (a WhatsApp chat id holds a
  phone number), key names, usernames and client IPs.
- Every row of `decisions` and `audit_log`, with every column, except the
  typed text in `audit_log.detail` (below). A decision row holds
  `params_hash`, never the params. `audit_log.detail` can quote
  identifiers, for example a hidden resource, a Telegram chat id or the
  resources of a capability.

Typed text in `audit_log.detail` never leaves. The audit export always
replaces the value of these keys, at any depth and in any letter case, with
`redacted:sha256:<first 16 hex digits>`:

- `reason`. Today `hidden.add` records the owner's reason for hiding a
  resource under it. The key decides, not the action, so the reason codes of
  `action.denied` and `plugin.refused` are replaced too.
- `note`, `label`, `message` and `text`. No action records them today. They
  are reserved, so a future action that records typed text under one of
  them cannot ship it by mistake.

A `detail` that is not a JSON object is replaced whole. The other keys
stay as stored: ids, counts, flags, the names of config fields, scopes. An
empty or null value stays empty or null. `AUDIT_EXPORT_HASH_RESOURCES`
does not change any of this. Two notes:

- Equal texts give equal markers, so they can still be counted. The hash
  has no key: somebody who guesses a short text can confirm the guess.
- Key names and usernames are identifiers, not typed text in this sense.
  They leave in clear, in `decisions.key_name`, `audit_log.actor`, `detail`
  and the log lines. Do not put private words in a key name.

With `AUDIT_EXPORT_HASH_RESOURCES=true`, the audit export also replaces
these values with `sha256:<first 16 hex digits>`:

- The `resource` column of both tables.
- Every other string inside `audit_log.detail`. Keys and numbers stay.

Equal values still give equal hashes, so counts and groups still work. Two
limits apply:

- The hash has no key. Somebody who guesses a short identifier, such as a
  phone number, can confirm the guess.
- The option changes the audit export only. The service log lines still
  carry `resource=` in clear. Keep shipping off if no identifier may leave.

### What never leaves the server

- The WhatsApp pairing QR. The sidecar prints it to stdout for an operator
  on this server (`sidecars/whatsapp/internal/wa/client.go`), and the QR can
  link a phone to this broker. The sidecar's log lines and any crash output
  go to stderr. The shipper keeps everything from stderr. From stdout it
  keeps whatsmeow's log lines only (`20:31:35.601 [WhatsApp INFO] ...`).
  The QR block, the banners and anything else on stdout stay in
  `docker compose logs`.
- The pairing code in a log line. At `LOG_LEVEL=DEBUG`, whatsmeow logs the
  code itself on its `QRChannel` logger. The shipper drops every line of
  that logger, and every sidecar line that carries a pairing code.
- The shipper's own log.
- Any table but `decisions` and `audit_log`. The exporter never reads
  `actions` (params, notes, labels) or `plugin_secrets`.
- Any secret or token. They are not in the logs and not in those two tables.

Keep `LOG_LEVEL=INFO` while shipping. `DEBUG` adds lines meant for local
diagnosis.

The sidecar's crash output does leave the server, on purpose: a Go panic
writes its message and its stack to stderr, and the shipper keeps them.

### The shipper's pipeline

`ops/fluent-bit/pipeline.yaml` changes each record in five steps:

1. Docker splits a line longer than 16 KB into parts. The `multiline` filter
   joins them, so a large audit row still parses as one JSON object.
2. The first `grep` filter keeps a `whatsapp-sidecar` record only if it
   came from stderr, or if it is a whatsmeow log line on stdout (above).
3. The second `grep` filter drops every `whatsapp-sidecar` record of
   whatsmeow's `QRChannel` logger, and every record that carries a pairing
   code.
4. The `parser` filter turns each JSON line into attributes. A text line
   does not parse and passes unchanged.
5. A parsed service line has its own `message`, so the `modify` filter drops
   the raw copy. An audit row has no `message`, so its raw line stays as the
   log message.

Every event also carries `container_name` (for example `/aab-broker-1`) and
`source` (`stdout` or `stderr`).

### The audit export

`aab audit export` (module `broker/broker/audit_export.py`) prints each row
it did not print before as one JSON line:

```json
{"service":"audit","table":"decisions","id":42,"request_id":"5f0c...","kind":"decision","ts":1790000000,"principal_id":"...","key_id":3,"key_name":"bot","grant_chain":"[\"...\"]","target":"whatsapp","action":"send_message","resource":"...","params_hash":"...","decision":"deny","reason":"out_of_grant","enforced_where":"{}","outcome":null,"actor_principal":null,"actor_via":null,"prev_hash":"...","hash":"...","signed":1}
```

- `service` is always `audit`. `table` is `decisions` or `audit_log`. The
  other keys are the columns of the row, with their stored values. The one
  exception is the typed text in `audit_log.detail`
  ([What leaves the server](#what-leaves-the-server)).
- The JSON columns (`grant_chain`, `enforced_where`, `detail`) stay JSON
  text, as stored. The hash chain covers their parsed values
  (`broker/broker/decisions.py`), so parse them to recompute a hash.
- `ts` is the time of the decision, in unix seconds. The event's own
  `timestamp` is the time of the export, up to one interval later.

How it reads `broker.db`:

- The volume is mounted read-only, and the connection is `mode=ro` with
  `query_only`. The exporter cannot change the record it copies.
- SQLite can read a WAL database from a read-only mount only while
  `broker.db-wal` and `broker.db-shm` exist. The exporter cannot create them.
  Thus the broker keeps one idle connection open while it runs
  (`db.hold_open()`), and the two files exist as long as it runs.
- While the broker is stopped, a run fails with `broker.db could not be
  read (unable to open database file); is the broker running?` and exports
  nothing. The next run continues where the last good one ended.

Where it resumes:

- A cursor file in the volume `audit_export_state` holds, per table, the
  last exported id and a digest of that row's identity columns. These are
  `id` and `hash` for `decisions`, and `id`, `ts`, `actor` and `action` for
  `audit_log`. The cursor cannot live in `app_config`, because the exporter
  cannot write `broker.db`.
- An upgrade that adds a column (`db._MIGRATIONS`) does not change the
  digest. The export continues after the cursor. The new column appears in
  the next rows.
- A cursor from the first release holds a digest of every column. The
  exporter still accepts it for an unchanged row, and rewrites it in the
  new form.
- The exporter prints a batch of up to 500 rows, then moves the cursor. A
  crash between the two prints that batch again. Thus a row can arrive
  twice, but never not at all. `table`, `id` and `hash` identify a repeat.
- If the row under the cursor is gone, or an identity column changed,
  `broker.db` was replaced or restored. The exporter logs a warning and
  exports that table again from the start.
- A missing, empty or malformed `broker.db` exports nothing and moves
  nothing.

To send everything again, for example after an outage that lost lines, run
the exporter once with `--reset`:

```bash
$C stop audit-exporter
$C run --rm audit-exporter aab audit export --reset
$C start audit-exporter
```

With `--loop`, `--reset` stays in force until a run succeeds. A first run
that fails, for example because the broker is not up yet, does not use it.

Without Docker, the same command reads any copy of `broker.db`:
`aab audit export --db broker.db --state cursor.json`.

Its settings:

- `aab audit export` reads `AUDIT_EXPORT_STATE`, `AUDIT_EXPORT_INTERVAL`
  (default 3600, at least 1) and `AUDIT_EXPORT_HASH_RESOURCES` (default
  `false`) as settings (`broker/broker/config.py`).
- The flags `--state`, `--interval` and `--hash-resources` override them.
- A value it cannot read stops it before it reads anything. It names the
  variable, never the value.
- On `docker stop` it exits within about a second, or when the current run
  ends. Its signal handler only sets a flag, and the loop looks at the flag
  every second.

### Outages and delivery

- **The shipper is down or not started yet.** Every service starts and runs:
  `fluentd-async` connects in the background. `docker compose logs` keeps
  working through Docker's dual logging.
- **New Relic is unreachable.** The shipper retries without end. Records
  wait in its memory, up to 32 MB.
- **Lines are lost when a buffer is full.** That happens when the shipper is
  full, when the Docker driver's buffer is full, or when the daemon or the
  shipper restarts. The lost lines are still in `docker compose logs`.
  A lost audit row shows as a gap (the NRQL below finds it). Send the rows
  again with `--reset`.

New Relic is a copy for queries, not the record. The authoritative record is
`broker.db`, with its hash chain (`aab decisions verify`).

**Trust.** Docker's `fluentd` driver has no authentication, so the shipper's
port has none either. Any process on this host can send it lines, with any
fields. No container can: the port is on the host's loopback, and no
container shares `net_logs`. Thus an event in New Relic is only as
trustworthy as this host. Before an agent relies on an audit event, it walks
the chain (below). A forged decision row shows up there as a conflict: two
rows with one id, or a `prev_hash` that matches no row. Only
`aab decisions verify`, which holds `DECISION_SIGNING_KEY`, tells which row
is real.

### Retention

New Relic keeps log events for a limited time. At the time of writing, the
free tier keeps them for 30 days and ingests 100 GB per month at no cost.
Check the current values on your account's data management page. An event
older than the retention is gone from New Relic. Back up `broker.db` for
anything older (`deploy/DEPLOY.md` > Operations > Backups).

### Querying with NRQL

An agent queries through New Relic's NerdGraph API with a New Relic user key.
That key reads data. It is a different key from the ingest key, and it never
goes into this server's `.env`. All examples run on the `Log` event type.

Denies per agent key, last day:

```sql
SELECT count(*) FROM Log
WHERE service = 'audit' AND `table` = 'decisions' AND decision = 'deny'
FACET key_name, reason SINCE 1 day ago
```

Outcomes that failed with a 5xx (503 is "not delivered", 502 is "outcome
unknown"):

```sql
SELECT count(*) FROM Log
WHERE service = 'audit' AND `table` = 'decisions' AND kind = 'outcome'
  AND (outcome IN ('unavailable', 'unknown') OR outcome LIKE 'error:5%')
FACET target, action, outcome SINCE 1 day ago
```

HTTP 5xx answers in the access lines (needs `LOG_FORMAT=json`):

```sql
SELECT count(*) FROM Log
WHERE logger LIKE '%.access' AND message LIKE '% status=5%'
FACET service SINCE 1 hour ago
```

Failed owner logins, by client IP:

```sql
SELECT count(*) FROM Log
WHERE service = 'audit' AND `table` = 'audit_log'
  AND action IN ('auth.login_failed', 'auth.setup_failed')
FACET capture(detail, r'.*"ip": "(?P<ip>[^"]*)".*') SINCE 1 day ago
```

Installer and pin events, newest first:

```sql
SELECT ts, actor, action, resource, result, detail FROM Log
WHERE service = 'audit' AND `table` = 'audit_log'
  AND action IN ('plugin.pin', 'plugin.unpin', 'plugin.refused', 'plugin.install',
                 'plugin.upgrade', 'plugin.remove', 'installer.git_token.set',
                 'installer.git_token.clear')
SINCE 7 days ago LIMIT 100
```

The installer's own job lines: `SELECT message FROM Log WHERE service =
'installer' SINCE 7 days ago`.

A gap in the decision chain. First a quick check:

```sql
SELECT min(id), max(id), count(*) FROM Log
WHERE service = 'audit' AND `table` = 'decisions' SINCE 7 days ago
```

If `max - min + 1` is greater than `count`, ids are missing. A repeated batch
adds rows, so it can hide a gap. The full check walks the chain:

```sql
SELECT id, prev_hash, hash FROM Log
WHERE service = 'audit' AND `table` = 'decisions' SINCE 7 days ago LIMIT MAX
```

Sort the result by `id` and drop repeats of the same `id` and `hash`. Then
each `prev_hash` must equal the `hash` of the row before it. The first row of
the chain has a `prev_hash` of 64 zeros. A query returns at most 5000 rows,
so walk a long chain in id ranges (`AND id > 5000`, ...).

## The WhatsApp pairing QR

While the sidecar waits for pairing, it prints the pairing QR as a block of
characters in its log, as it always has. This is the pairing path that needs
no console (`docs/plugins/whatsapp.md`). A code is valid for tens of seconds.
It has no value after a device pairs. Also, a person needs Docker access on
the server to read a container's log. The sidecar's log lines record the
event (`qr event=code`, `qr served`), never the code. The QR goes to
stdout and the log lines to stderr. Thus the New Relic shipper keeps the log
lines and drops the QR
([What never leaves the server](#what-never-leaves-the-server)).
