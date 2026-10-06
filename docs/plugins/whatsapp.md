# WhatsApp plugin (`plugin-whatsapp`)

The WhatsApp plugin gives agents read and send access to one WhatsApp
account, inside the limits of the broker's grants. It is its own container.
The broker reaches it over the internal plugin API (`docs/plugin-api.md`).
The broker never touches WhatsApp, the Go sidecar or the message archive
itself.

```
broker ──(net_whatsapp, X-Plugin-Token)──> plugin-whatsapp ──(wa_internal, X-Internal-Token)──> whatsapp-sidecar ──> WhatsApp
                                                 │
                                                 └── reads /data/messages.db (wa_data, mounted read-only)
```

- **Package:** `plugins/whatsapp/aab_plugin_whatsapp/`. `aab_plugin_runtime`
  serves it. It is a port of WA_GW's `wastore.py`, `sidecar.py`,
  `policy.normalize_jid` and the read half of `services.py`.
- **Manifest:** `plugins/whatsapp/aab_plugin_whatsapp/manifest.yaml` is the
  source of truth. The broker keeps a vendored copy at
  `broker/broker/targets/whatsapp/manifest.yaml`. The copy must stay
  byte-identical: `broker/tests/targets/test_whatsapp.py` fails on any drift.
  The broker pins id and version and then uses its own copy. Thus the
  container cannot widen the lattice at runtime.
- **Enforcement: proxy only.** The linked device has full access to the
  account, and WhatsApp has no scoped credentials. Only the broker and this
  plugin restrict an agent. `enforced_where` reports `proxy` for every
  dimension.

## What it does

| Action | Kind | What happens in the plugin |
|---|---|---|
| `list_chats` | read | Recent chats from the archive, most recent first (`query` matches the name or JID). |
| `get_chat` | read | One chat's metadata. Hidden or missing: `404`. |
| `read_messages` | read | One chat's messages, newest first. Page backwards with `before` (a timestamp) and `before_id`, which makes the cursor exact within one second. Hidden or missing chat: `404`. |
| `search_messages` | read | Substring search across the archive, or inside one `chat` (a hidden or missing chat is then a `404`). |
| `check_new_messages` | read, long-poll | Incoming messages after `cursor`, oldest first: `{"cursor", "items"}`. Called with no cursor, it returns the current top of the archive and no backlog, so the agent starts "from now"; the broker answers that bootstrap at once even if `?wait=` was given. Messages older than 5 minutes are never counted as new: history sync re-inserts old messages as new rows. |
| `search_contacts` | read | Address-book search by name or phone fragment. It returns JIDs. |
| `get_media` | read, binary | Downloads a message's media through the sidecar. The plugin checks the chat's visibility before calling the sidecar. The sender chose the media type, so active types (HTML, SVG, XML, scripts) come back as `application/octet-stream`; over REST the broker always serves it as a `nosniff` attachment named after the message id. |
| `send_message` | write | Sends a text message through the sidecar to a person or a group. The plugin rejects read-only chats (status, broadcast lists, channels) with `400`. It can be drafted and scheduled; the draft routing and the queue live in the broker. |

List results are `{"items": [...]}`. Every chat and message row carries
`"resource_ref": {"kind": "chat", "id": <jid>}`. Contacts use
`"kind": "contact"`. Thus the post-filter of the broker drops any row outside
the scope of the call, even if this plugin has a bug. A send result carries
no `resource_ref`, on purpose. After the plugin sends a message, a
post-filter `404` would tell the agent that the message did not go out.

### Visibility (the CallScope)

Every `/perform` call carries `scope.visibility.chat = {deny, allow_only}`.
`deny` is the hidden chats of the owner plus the denies of the key.
`allow_only` is the chat selector of the capability. The plugin applies both
**inside the SQL**, as WA_GW did. Thus pagination stays honest and counts
never leak.

- **Hidden == missing.** A hidden chat gives exactly the same `404` as a
  chat that does not exist. This applies to a get, a read, a chat-scoped
  search and a media download. A hidden chat never appears in a list, a
  search or the new-messages feed.
- **Media never leaves the sidecar** for a hidden chat. The check runs
  first, and the plugin does not call the sidecar.
- **Sends** go through a check against the scope by id, not against the
  archive. Thus a first message to a new chat works. A hidden or
  out-of-scope recipient is a `404`, and the plugin never calls the sidecar.
- **`allow_only: []` means no chats at all.** WA_GW treated an empty per-key
  allowlist as "unrestricted". In a CallScope, `null` means unrestricted and
  `[]` means nothing. Treating `[]` as unrestricted would fail open.
- A scope that the plugin cannot read, for example a string where a list
  belongs, returns `400`. The plugin never reads it as "no restriction".
- **Contacts stay visible when the owner hides a chat.** This is WA_GW's
  decision, kept on purpose: contacts are how an agent turns a name into a
  JID. The `contact` denies of a key still apply.

### JIDs

`jid.py` has two paths, because not every chat accepts messages:

- **`normalize_recipient`** is the send path, and `normalize` for a
  `contact`. It is WA_GW's `normalize_jid`, unchanged. It mirrors the
  `ParseRecipient` of the sidecar, and a test runs the vectors of the Go
  test against it. It accepts `…@s.whatsapp.net`, `…@g.us`, `…@lid` or an
  international phone number. It strips device (`:N`) and agent (`.N`)
  suffixes.
- **`normalize_jid`** is `normalize` for a `chat`, and every read. It
  accepts all of that, plus the chats that the archive holds but the sidecar
  cannot send to:
  - Status updates (`status@broadcast`).
  - Broadcast lists (`<digits>@broadcast`).
  - Channels (`<digits>@newsletter`).

  The owner can hide them, and agents can read them. `send_message` uses the
  recipient path, so they get a `400` before the plugin calls the sidecar.
  The broker cannot tell this from the chat id alone. Thus the broker queues
  a *drafted* send to one of them, and it fails with that `400` on approval.

One rule applies on both paths: the user part must be canonical. That is
digits, or `digits-digits` for an old-style group, or `status` for
`status@broadcast`. Hidden lists and grants compare exact strings. WhatsApp
possibly still delivers other spellings to the same account. Examples are
`+972…@s.whatsapp.net`, or a space or an invisible character inside the
digits. The plugin rejects such a spelling with `400`. Thus it cannot act as
a second name for a hidden chat.

## Environment

The container reads only these names. Compose maps each one from the `.env`
entry of this service. No other service receives them.

| In the container | Fed from `.env` / value | Read by | Required |
|---|---|---|---|
| `PLUGIN_TOKEN` | `PLUGIN_TOKEN_WHATSAPP` | runtime (`from_env`): the broker's `X-Plugin-Token` | yes; the runtime does not start when it is empty |
| `PLUGIN_SECRETS_KEY` | `PLUGIN_SECRETS_KEY_WHATSAPP` | runtime: the Fernet key for `/secrets` | yes in compose |
| `PLUGIN_SECRETS_DIR` | `/secrets` (the `whatsapp_secrets` volume) | runtime | set by compose and by the image |
| `SIDECAR_URL` | `http://whatsapp-sidecar:8081` | plugin | default `http://whatsapp-sidecar:8081` |
| `SIDECAR_TOKEN` | `SIDECAR_TOKEN` | plugin: the sidecar's `X-Internal-Token` | yes; the plugin does not start when it is empty |
| `MESSAGES_DB` | `/data/messages.db` | plugin: the archive path | default `/data/messages.db` |

The `config_schema` of the manifest is **empty** on purpose, as the
configuration principle says. These are deployment values, not console
settings. `/configure` accepts a request and ignores its config. Thus
nothing that reaches the plugin API can point the plugin at another sidecar
or another database. A hijacked console session is an example of such a
caller. The secret store under `/secrets` exists because the runtime gives
it. This plugin stores nothing in it today.

The image runs `uvicorn --factory aab_plugin_whatsapp.main:create_app` with
one worker on `:8090`, as uid 10001. Every plugin API route requires the
token. Thus the healthcheck only checks that the port accepts a TCP
connection. `python -m aab_plugin_whatsapp` does the same outside Docker.

## Pairing (connect flow)

The WhatsApp session belongs to the sidecar. It is whatsmeow's own
`session.db` in `wa_session`, a volume that only the sidecar mounts. It is
the one credential without encryption at rest (see `docs/architecture.md`).
This container mounts only the archive (`wa_data`, read-only), so it cannot
read the session. The plugin passes the pairing flow through and never holds
the session:

1. The owner enables the plugin in the console. With nothing to configure,
   enabling runs `/configure` (a no-op) and then reads `/status`. Before
   pairing, the plugin reports `connected: false` and `health: "waiting for
   QR pairing"`. The plugin shows as enabled, but agents cannot use it.
   Every agent call that the grant of a key covers returns
   `503 not_connected`. Any other call gets the same `403 out_of_grant` that
   it gets after pairing. Thus a key without authority cannot tell whether
   the owner paired WhatsApp.
2. The console calls `POST /v1/admin/plugins/whatsapp/connect/start` and gets
   `{"kind": "qr"}`. After pairing, the same call returns `{"kind": "none"}`.
3. The console shows `GET /v1/admin/plugins/whatsapp/connect/qr.png`. The
   broker proxies it from the plugin, and the plugin proxies it from the
   `/qr` of the sidecar. The response has `Cache-Control: no-store`, because
   a QR is a pairing secret while it is valid. It returns `503` until the
   sidecar has a first code, and `409` after the device pairs.
4. The owner scans the QR with the phone (WhatsApp > Linked devices).
5. The console calls `POST …/connect/finish`. For WhatsApp this is a no-op
   (`{"ok": true}`). But it makes the broker refresh health, so the plugin
   becomes `connected: true`. `POST …/health` does the same.

**Disconnect** returns `409` with the message "unlink from the phone (Linked
devices); the sidecar exits and re-pairs". The plugin mounts the session
read-only, and by design it cannot wipe it. When the owner unlinks the device
on the phone, the sidecar sees `LoggedOut`, clears its session and exits.
Docker restarts it into a fresh QR flow.

### Status

`/status` maps the sidecar's `{connected, logged_in, jid, push_name,
waiting_for_qr, fatal}` onto the runtime's fields:

| Sidecar state | `connected` | `healthy` | `health` |
|---|---|---|---|
| logged in, connected | true | true | `ok` |
| logged in, reconnecting | true | false | `reconnecting to WhatsApp` |
| not logged in, QR shown | false | false | `waiting for QR pairing` |
| not logged in | false | false | `not paired` |
| `fatal` set (for example a temporary ban) | = logged in | false | `fatal: <reason>` |
| sidecar unreachable | (`/status` answers 503; the broker keeps the last known value) | | |

`connected` means **paired**. The archive stays readable while WhatsApp
reconnects. Thus a network blip does not turn every read into a `503`. The
sidecar's own details are under `connection`. The JID and push name of the
owner appear only on the admin plane.

## Errors: 503 versus 502

| What happened | Plugin answers | Broker behaviour |
|---|---|---|
| Hidden or missing chat, missing message or media | `404` | The generic `not found` body |
| Bad recipient (including a read-only chat), empty or oversized text (the sidecar reads at most 64 KiB), bad params, malformed scope | `400`, before any sidecar call | Passed through |
| Agent params that are not valid UTF-8 JSON (a lone surrogate, NaN) | never reaches the plugin | The broker answers a recorded `400 invalid_params` before evaluating, and takes no budget |
| Sidecar unreachable (connection refused, connect timeout) | `503` | The plugin did nothing; a queued send returns to pending |
| Sidecar says "not logged in" | `503` | As above |
| Sidecar rejected `SIDECAR_TOKEN` (`401`/`403`) | `503` | As above; not shown to the agent as its own `401` |
| Archive locked or unreadable | `503` | As above (every archive read comes before any sidecar call) |
| Read timeout or reset after the request was sent, a garbled `2xx`, a redirect, the sidecar's `502` or any other `5xx` | `502` | Outcome unknown: the budget stays reserved and a queued send becomes `failed` and is **never** retried |

The sidecar client never follows redirects and ignores proxy environment
variables. Thus `X-Internal-Token` can only ever go to `SIDECAR_URL`. Its
timeout (25 s) is shorter than the plugin timeout of the broker (30 s). Thus,
for a hanging sidecar, the plugin answers its own clean `502` before the
broker gives up.

## Operational notes

- **Read-only WAL archive.** This container mounts `wa_data` read-only. The
  plugin opens the archive with `mode=ro` and `PRAGMA query_only` on each
  call. A test under Docker verified this for 0.2.0 (`docs/deployment.md` >
  Verify after `docker compose up`, step 7). These are the results:
  - *Sidecar running.* The sidecar creates `messages.db`, `-wal` and `-shm`
    at start, before pairing. SQLite reads the WAL through the `-shm` of the
    sidecar, which it maps read-only. A stand-in writer made 3,000
    single-row commits and several checkpoints. All 1,000 reads between them
    answered 200, and the newest row never went backwards.
  - *Sidecar stopped cleanly* (`docker compose stop`). The sidecar closes
    the archive on SIGTERM. Thus SQLite folds the WAL into `messages.db` and
    removes `-wal` and `-shm`. The plugin cannot recreate them on a
    read-only mount. Thus reads answer `503`, never stale or partial data,
    until the sidecar starts again. The plugin needs no restart.
  - *Sidecar crashed* (SIGKILL, OOM). `-wal` and `-shm` stay behind. SQLite
    rebuilds the WAL index in heap memory from the `-wal` file, because it
    cannot write the `-shm`. Reads answer 200 with the last committed rows.
    No writer is alive, so nothing is partial.
  - *Before the sidecar has created `messages.db`*, reads return empty
    results.

  The mount stays read-only on purpose. A read-write mount (WA_GW's setup,
  with `mode=ro` in the URI) would let this container write the archive that
  the sidecar owns. Before the session moved to `wa_session`, it would also
  have let it write the WhatsApp session itself. `?immutable=1` would make
  SQLite ignore the WAL and the locks of the sidecar. Then a read can return
  stale or torn data while the sidecar writes. A separate writable place for
  the `-shm` does not exist. SQLite keeps it beside the database. It must be
  the very file that the sidecar maps, or the plugin would not see the
  commits of the sidecar.
- **Long-poll bootstrap.** Call `check_new_messages` one time without a
  cursor. Then long-poll with `?wait=` and the cursor. The broker answers a
  call without a cursor at once, whatever `wait` says. This is the engine
  rule for every `long_poll` action that declares a `cursor`. Thus the agent
  misses nothing that arrives during a wait.
- **Labels.** `/label` and `/resolve` apply no visibility. `/label` is for
  approval cards, for example "Send to Alice: …". `/resolve` is for the
  pickers of the console. They serve the owner. The broker filters `resolve`
  itself for agents.

## Development

```bash
pip install -e plugin-runtime -e "plugins/whatsapp[dev]"    # from the repo root
cd plugins/whatsapp && python -m pytest                      # the plugin's own tests
cd broker && python -m pytest tests/targets                  # end to end through the broker
```

`plugins/whatsapp/tests/fakes.py` holds the archive schema, a seeded
archive, and a scripted sidecar behind `httpx.MockTransport`. The archive
schema must change in lockstep with
`sidecars/whatsapp/internal/store/store.go`. The end-to-end test of the
broker loads the same file.
