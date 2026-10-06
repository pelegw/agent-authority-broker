# Logging

Every service writes one line per event to its own stdout; Docker keeps those
lines, rotated, and `docker compose logs` reads them. Logs are the
**operational** trail: what the broker and the plugins did, how long it took,
what failed. The **accountability** trail is unchanged: the hash-chained
decision record (every decision and outcome, with its authority chain) and the
audit table (every human action). The two meet on the **request id**: each log
line carries the id of the request or background job it belongs to, and the
decision rows that request produced carry the same id in `request_id`.

## Who logs what

| Service | Logger names | Lines |
|---|---|---|
| `broker` | `broker.*` | One access line per HTTP request (`broker.access`); one line per decision and per outcome (`broker.engine`); the owner's actions (login, tokens, keys, grants, approvals, plugin configuration, settings); agent refusals; action lifecycle (created, approved, delivered, deferred, failed, held); delegations; plugin discovery and health changes; Telegram; the scheduler; boot. |
| `plugin-whatsapp`, `plugin-github`, `plugin-google` | `aab_plugin_runtime.*`, `aab_plugin_<name>.*` | One access line per plugin API call (`aab_plugin_runtime.access`); one line per `/perform` (action, status, duration); configure (field names), connect, disconnect, the pairing QR served; secret-store writes (slot and field names); per plugin: each sidecar call (WhatsApp), each minted credential by scope (GitHub installation tokens, Google access tokens: cached or fresh), the target API's refusals by status class. |
| `whatsapp-sidecar` | Go `log` | One request line per API call (method, path, status, duration, request id); send results (message id only); QR events; connection state changes; whatsmeow's own lines. |
| `edge` (public overlay) | Caddy | Caddy's own log, unchanged. |
| `uvicorn` | `uvicorn`, `uvicorn.error` | Server start and stop, through the same handler and format. uvicorn's own access log is off: see [The access line](#the-access-line). |

The `aab` CLI logs to **stderr** (at WARNING unless `LOG_LEVEL` says
otherwise): its stdout is its output, and `aab simulate` / `aab skill build`
run broker code in-process.

## Format

Text (the default, `LOG_FORMAT=text`):

```
2026-09-24T20:29:08.839Z INFO broker.access [broker smoke-run-1] request method=GET path=/v1/targets status=200 duration_ms=42 actor=key:smoke-agent ip=172.19.0.1
2026-09-24T20:29:08.879Z INFO broker.engine [broker d7234be1ab5f458a93995513a27fadac] decision decision=deny reason=target_unavailable status=404 target=whatsapp action=list_chats key=smoke-agent resource=- chain=0 row=1 approved_by=-
2026-09-24T20:31:35.601Z INFO aab_plugin_whatsapp.sidecar [plugin-whatsapp smoke-health-1] sidecar call method=GET path=/status status=200 error=- duration_ms=20
```

`<UTC timestamp, ms>Z <LEVEL> <logger> [<service> <request id>] <message>`.
The service is `broker` or `plugin-<service>`; the request id is `-` for
lines outside any request (boot, the scheduler's idle ticks).

The message is a fixed text followed by `key=value` pairs (logfmt): a value is
bare when it has no space, quote, `=`, backslash or control character, and
otherwise double-quoted with those escaped; lists are comma-joined, an empty
value is `-`, a value longer than 200 characters is cut. So a value (a key
name an agent chose, a resource id) can never end the line, fake a second
pair or a second line.

JSON (`LOG_FORMAT=json`): one object per line with the same fields, for a log
collector:

```json
{"ts": "2026-09-24T20:29:08.839Z", "level": "INFO", "logger": "broker.access", "service": "broker", "request_id": "smoke-run-1", "message": "request method=GET path=/v1/targets status=200 duration_ms=42 actor=key:smoke-agent ip=172.19.0.1"}
```

plus `exc` (the traceback) when there is one. JSON is ASCII-escaped, so a
line is always one line.

The sidecar writes Go's standard format with UTC microseconds
(`2026/09/24 20:31:35.601156 request method=GET path=/status status=200
duration_ms=0 request_id=smoke-health-1`), the same key=value style, and has
no JSON mode.

## Request ids and the decision record

- **Every HTTP request** gets an id: the caller's `X-Request-Id` when it is
  well formed (`[A-Za-z0-9._-]{1,128}`), else a fresh 32-hex-character id.
  The broker refuses ids that start with `sched-` or `tg-` (those are its own
  background jobs, so no caller can make its calls look like the
  scheduler's) and generates one instead. The id is echoed in the
  `X-Request-Id` response header, so an agent can quote it.
- **The decision record uses it**: `engine.new_request_id()` returns the
  current request's id, so a decision row's `request_id` is the id on that
  request's log lines. (It is a correlation id, not a unique key: an agent
  that sends the same `X-Request-Id` twice gets two sets of rows with it.)
- **It crosses every hop**: the broker sends it to the plugin service on
  every plugin call (`/perform`, and also `/normalize`, `/resolve`, `/status`
  and the rest), the plugin runtime runs the call under it, and
  plugin-whatsapp forwards it to the sidecar, which puts it on its request
  line. One agent call is therefore one id in the broker, the plugin, the
  sidecar and the decision record.
- **Background work** runs under ids of its own: each scheduler tick under a
  `sched-<hex>` id and each delivery in it under a fresh `sched-<hex>` (the
  per-delivery line names its tick); each Telegram update under a
  `tg-<hex>` id (a tap's approval, the delivery it triggers and the decision
  rows all carry it). A console approval runs under the console request's
  id.

To follow one call:

```bash
docker compose logs --no-log-prefix | grep smoke-run-1
```

## The access line

One line per request, after the response, from `request_log.py` (the same
middleware in the broker and the plugin runtime):

```
request method=POST path=/v1/targets/echo/actions/list_items status=200 duration_ms=12 actor=key:reader ip=172.19.0.1
```

- `path` is the raw path **without the query string, always**. The OAuth
  callback's query carries the authorization code and the state nonce, and
  an agent's GET query carries its params; the middleware never reads the
  query string at all.
- `actor` is who was authenticated: `key:<name>` (an agent key),
  `monitor:<token name>` (a monitor token on `/health`),
  `owner:<username>` (a session or admin token), `plugin:<service>` in a
  plugin service (the broker, holding that service's token), or `-`.
- `ip` is the client address the origin guard trusts (`CF-Connecting-IP`
  behind Cloudflare, else the socket peer).
- 5xx lines are WARNING; health probes (`/health`, `/v1/health`) and the
  owner's monitoring summary (`/v1/admin/health`) are not logged.

uvicorn's own access log is **off**, structurally: uvicorn writes its access
line only when the `uvicorn.access` logger has a handler path (that is all
`--no-access-log` changes), and the logging setup leaves it none, so uvicorn
never writes one, with or without the flag. Its line would repeat ours and
carry the query string. The images also pass `--no-access-log`.

## Never logged, and the backstop

Log lines never carry: params or results of an action, message bodies,
notes, resource labels (ids are logged, names are not), cookie values,
`Authorization` headers, passwords, the setup token, agent keys, admin
tokens, plugin tokens, the sidecar token, the Telegram bot token or a link
code, OAuth codes, client secrets, refresh or access tokens, installation
tokens, private keys, Fernet keys. Where a value's name is useful it is
logged by name (`secret_fields=client_secret`, `secrets_set=setup_token`),
never with the value. Exception text is logged as the exception's class
where it could quote input.

The tests prove it: `tests/targets/test_secrets_in_logs.py` drives setup,
login, admin tokens, key create/rotate/use, delegation, the Telegram token,
plugin configure, a Google connect with minted tokens and a GitHub App
install with minted tokens, with every logger at DEBUG, and fails if any
secret value appears in any record or in the handler's output. The plugins'
own suites do the same for their credentials.

A **redaction backstop** sits on the handler (`RedactSecrets`, the pattern
table `SECRET_PATTERNS` in `logging_setup.py`): anything shaped like an agent
key (`aab_` + 48 hex), an admin token (`aab_admin_` + 48 hex), a
`Bearer <token>`, a Telegram bot token, a Google access or refresh token or
client secret, a GitHub token (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`,
`github_pat_`), a Fernet key, a PEM block, a session cookie, or the value
after a secret-bearing field name (`password=`, `client_secret=`,
`X-Plugin-Token:`, ...) becomes `<redacted>`, in the message, the traceback
and the stack. It edits a copy of the record, so a test's capture still sees
what the code logged. It is a backstop, not the mechanism: the 64-hex-digit
secrets (plugin tokens, the sidecar token, the signing key) cannot be told
apart from a SHA-256 by shape, so they are kept out by never being logged.

Third-party loggers that would log request URLs with their query strings
(`httpx`: a Gmail search, the bot token in the Telegram API path), protocol
payloads (`mcp`: tool arguments) or form fields are held at WARNING whatever
`LOG_LEVEL` says.

## Levels

`LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR`; `WARN` and lower case
accepted; default `INFO`) and `LOG_FORMAT` (`text` or `json`) are read once
at start by every Python service; a value not recognised logs a WARNING and
falls back to `INFO` / `text`. The sidecar reads `LOG_LEVEL` too: it sets
whatsmeow's level, and `WARNING` or `ERROR` drop its per-request lines.

- **INFO**: every line described above. Idle background work is silent (a
  scheduler tick logs only when something was due).
- **WARNING**: refusals and failures worth a look: agent and admin
  authentication failures (with the reason class: `missing`, `malformed`,
  `admin_token`, `unknown_key`, `chain:expired`, ...), rate-limited logins,
  budget refusals (naming the exhausted grant), 5xx outcomes, a plugin
  service not reachable, a health check that failed, Telegram poll errors
  (class, status and backoff), requests that did not come through the edge.
- **ERROR**: a broken decision chain on verify, unexpected failures.
- **DEBUG**: adds one line per broker-to-plugin call.

These are environment settings, not console settings (`docs/configuration.md`):
they are process-level, read before any database is open, and the plugin
containers have no console. Set them in `.env` (`scripts/init_secrets.py`
writes the defaults) and restart: `docker compose up -d`.

## Rotation

`docker-compose.yml` (and the public overlay) give every service the same
logging block through an `x-logging` anchor:

```yaml
x-logging: &logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "5"
```

At most five 10 MB files per container, about 50 MB each; the oldest is
dropped. Change the anchor to keep more or less. Container logs are not a
backup: the decision record and the audit table (in `broker.db`) are what
you keep.

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

Keep the services writing to stdout and change the Docker **log driver**
instead: replace the `x-logging` anchor with, for example, `driver: local`
(compressed local files), `journald`, `syslog`, `fluentd`, `gelf` or
`awslogs`, with that driver's options. Set `LOG_FORMAT=json` so the collector
receives one JSON object per line with `ts`, `level`, `logger`, `service`,
`request_id` and `message` as fields. `docker compose logs` reads `json-file`,
`local` and `journald` directly; with other drivers Docker's dual logging
keeps a local copy for it (Docker 20.10 and later).

## The WhatsApp pairing QR

While waiting to be paired, the sidecar prints the pairing QR as a block of
characters in its log, as it always has (the pairing path that needs no
console, `docs/plugins/whatsapp.md`). A code is valid for tens of seconds and
worthless once a device is paired, and reading a container's log already
takes Docker access on the host. Its log lines record the event
(`qr event=code`, `qr served`), never the code.
