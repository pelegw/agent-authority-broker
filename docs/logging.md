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
| `uvicorn` | `uvicorn`, `uvicorn.error` | Server start and stop, through the same handler and format. uvicorn's own access log is off: see [The access line](#the-access-line). |

The `aab` CLI logs to **stderr**, at WARNING unless `LOG_LEVEL` sets a
different level. Its stdout is its output. `aab simulate` and `aab skill build`
run broker code in-process.

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
  32-hex-character id. The broker rejects ids that start with `sched-` or
  `tg-` and generates one instead. Those prefixes belong to its own
  background jobs, so no caller can make its calls look like the scheduler's.
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
  plugin token. The cost is that image digests become `<hex64>` too.
- Every secret value the job received becomes `<redacted>`, whatever its
  shape. That is `INSTALLER_TOKEN` and the GitHub token its request carried.
  The installer holds that token in memory for that job only and never saves
  it.

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

## The WhatsApp pairing QR

While the sidecar waits for pairing, it prints the pairing QR as a block of
characters in its log, as it always has. This is the pairing path that needs
no console (`docs/plugins/whatsapp.md`). A code is valid for tens of seconds.
It has no value after a device pairs. Also, a person needs Docker access on
the server to read a container's log. The sidecar's log lines record the
event (`qr event=code`, `qr served`), never the code.
