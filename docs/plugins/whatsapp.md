# WhatsApp plugin (`plugin-whatsapp`)

The WhatsApp plugin gives agents read and send access to one WhatsApp
account, inside whatever the broker's grants allow. It is its own container.
The broker reaches it over the internal plugin API (`docs/plugin-api.md`) and
never touches WhatsApp, the Go sidecar or the message archive itself.

```
broker ──(broker_net, X-Plugin-Token)──> plugin-whatsapp ──(wa_internal, X-Internal-Token)──> whatsapp-sidecar ──> WhatsApp
                                               │
                                               └── reads /data/messages.db (wa_data, mounted read-only)
```

- **Package:** `plugins/whatsapp/aab_plugin_whatsapp/`. It is served by
  `aab_plugin_runtime` and ported from WA_GW's `wastore.py`, `sidecar.py`,
  `policy.normalize_jid` and the read half of `services.py`.
- **Manifest:** `plugins/whatsapp/aab_plugin_whatsapp/manifest.yaml` is the
  source of truth. The broker keeps a vendored copy at
  `broker/broker/targets/whatsapp/manifest.yaml`, and it must stay
  byte-identical: `broker/tests/targets/test_whatsapp.py` fails on any drift.
  The broker pins id and version and then uses its own copy, so the container
  cannot widen the lattice at runtime.
- **Enforcement: proxy only.** The linked device has full access to the
  account, and WhatsApp has no scoped credentials. The broker and this plugin
  are the only things that restrict an agent. `enforced_where` reports `proxy`
  for every dimension.

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
| `send_message` | write | Sends a text message through the sidecar to a person or a group. Read-only chats (status, broadcast lists, channels) are refused with `400`. It can be drafted and scheduled; the draft routing and the queue live in the broker. |

List results are `{"items": [...]}`. Every chat and message row carries
`"resource_ref": {"kind": "chat", "id": <jid>}` (contacts use `"kind":
"contact"`), so the broker's post-filter drops any row the call may not see,
even if this plugin had a bug. A send result carries no `resource_ref` on
purpose: once a message is sent, a post-filter `404` would tell the agent it
was not.

### Visibility (the CallScope)

Every `/perform` call carries `scope.visibility.chat = {deny, allow_only}`.
`deny` is the owner's hidden chats plus the key's own denies. `allow_only` is
the capability's chat selector. The plugin applies both **inside the SQL**, the
way WA_GW did, so pagination stays honest and counts never leak:

- **Hidden == missing.** A get, read, chat-scoped search or media download of
  a hidden chat returns exactly the same `404` as a chat that does not exist.
  A hidden chat never appears in a list, a search or the new-messages feed.
- **Media never leaves the sidecar** for a hidden chat. The check runs first,
  and the sidecar is not called.
- **Sends** are checked against the scope by id, not against the archive, so
  a first message to a new chat works. A hidden or out-of-scope recipient is
  a `404`, and the sidecar is never called.
- **`allow_only: []` means no chats at all.** WA_GW treated an empty per-key
  allowlist as "unrestricted". In a CallScope, `null` means unrestricted and
  `[]` means nothing. Treating `[]` as unrestricted would fail open.
- A scope the plugin cannot read (for example a string where a list belongs)
  returns `400`. It is never read as "no restriction".
- **Contacts stay visible when a chat is hidden.** This is WA_GW's decision,
  kept on purpose: contacts are how an agent turns a name into a JID. A key's
  own `contact` denies still apply.

### JIDs

`jid.py` has two paths, because not every chat can be written to:

- **`normalize_recipient`** (the send path, and `normalize` for a
  `contact`) is WA_GW's `normalize_jid`, unchanged. It mirrors the sidecar's
  `ParseRecipient`, and a test runs the Go test's own vectors against it. It
  accepts `…@s.whatsapp.net`, `…@g.us`, `…@lid` or an international phone
  number, and it strips device (`:N`) and agent (`.N`) suffixes.
- **`normalize_jid`** (`normalize` for a `chat`, and every read) accepts all of
  that plus the chats the archive holds but the sidecar cannot send to:
  status updates (`status@broadcast`), broadcast lists (`<digits>@broadcast`)
  and channels (`<digits>@newsletter`). The owner can hide them and agents can
  read them. `send_message` uses the recipient path, so they get a `400`
  before the sidecar is called. The broker cannot tell this from the chat id
  alone, so a *drafted* send to one of them is queued and fails with that
  `400` on approval.

One rule is added on both paths: the user part must be canonical (digits, or
`digits-digits` for an old-style group; `status` for `status@broadcast`).
Hidden lists and grants compare exact strings, so a spelling that WhatsApp
might still deliver to the same account (`+972…@s.whatsapp.net`, a space or
an invisible character inside the digits) is refused with `400` and cannot act
as a second name for a hidden chat.

## Environment

The container reads only these names. Compose maps each one from the
service's own `.env` entry, and no other service receives them.

| In the container | Fed from `.env` / value | Read by | Required |
|---|---|---|---|
| `PLUGIN_TOKEN` | `PLUGIN_TOKEN_WHATSAPP` | runtime (`from_env`): the broker's `X-Plugin-Token` | yes; boot refuses when it is empty |
| `PLUGIN_SECRETS_KEY` | `PLUGIN_SECRETS_KEY_WHATSAPP` | runtime: the Fernet key for `/secrets` | yes in compose |
| `PLUGIN_SECRETS_DIR` | `/secrets` (the `whatsapp_secrets` volume) | runtime | set by compose and by the image |
| `SIDECAR_URL` | `http://whatsapp-sidecar:8081` | plugin | default `http://whatsapp-sidecar:8081` |
| `SIDECAR_TOKEN` | `SIDECAR_TOKEN` | plugin: the sidecar's `X-Internal-Token` | yes; boot refuses when it is empty |
| `MESSAGES_DB` | `/data/messages.db` | plugin: the archive path | default `/data/messages.db` |

The manifest's `config_schema` is **empty** on purpose, following the
configuration principle. These are deployment values, not console settings.
`/configure` accepts a request and ignores its config, so nothing that reaches
the plugin API (a hijacked console session, for example) can point the plugin
at another sidecar or another database. The secret store under `/secrets`
exists because the runtime provides it; this plugin stores nothing in it
today.

The image runs `uvicorn --factory aab_plugin_whatsapp.main:create_app` with
one worker on `:8090`, as uid 10001. Every plugin API route requires the
token, so the healthcheck only checks that the port accepts a TCP connection.
`python -m aab_plugin_whatsapp` does the same outside Docker.

## Pairing (connect flow)

The WhatsApp session belongs to the sidecar. It is whatsmeow's own
`session.db` in `wa_data`, and it is the one credential not encrypted at rest
(see `docs/architecture.md`). The plugin relays the pairing flow and never
holds the session:

1. The owner enables the plugin in the console. With nothing to configure,
   enabling runs `/configure` (a no-op) and then reads `/status`. Before
   pairing, the plugin reports `connected: false` and `health: "waiting for
   QR pairing"`. It is enabled but not usable: every agent call that a
   key's grant covers returns `503 not_connected`, and any other call the
   same `403 out_of_grant` it gets once paired (a key without authority
   cannot tell whether WhatsApp is paired).
2. The console calls `POST /v1/admin/plugins/whatsapp/connect/start` and gets
   `{"kind": "qr"}`. After pairing, the same call returns `{"kind": "none"}`.
3. The console shows `GET /v1/admin/plugins/whatsapp/connect/qr.png`. The
   broker proxies it from the plugin, and the plugin proxies it from the
   sidecar's `/qr`. It is sent with `Cache-Control: no-store` because a QR is
   a pairing secret while it is valid. It returns `503` until the sidecar has
   a first code and `409` once the device is paired.
4. The owner scans the QR with the phone (WhatsApp > Linked devices).
5. The console calls `POST …/connect/finish`. For WhatsApp this is a no-op
   (`{"ok": true}`), but it makes the broker refresh health, so the plugin
   becomes `connected: true`. `POST …/health` does the same.

**Disconnect** returns `409` with the message "unlink from the phone (Linked
devices); the sidecar exits and re-pairs". The plugin mounts the session
read-only and cannot wipe it by design. When the device is unlinked on the
phone, the sidecar sees `LoggedOut`, clears its session and exits. Docker
restarts it into a fresh QR flow.

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
reconnects, so a network blip does not turn every read into a `503`. The
sidecar's own details are under `connection` (the owner's JID and push name
appear only on the admin plane).

## Errors: 503 versus 502

| What happened | Plugin answers | Broker behaviour |
|---|---|---|
| Hidden or missing chat, missing message or media | `404` | The generic `not found` body |
| Bad recipient (including a read-only chat), empty or oversized text (the sidecar reads at most 64 KiB), bad params, malformed scope | `400`, before any sidecar call | Passed through |
| Agent params that are not valid UTF-8 JSON (a lone surrogate, NaN) | never reaches the plugin | The broker answers a recorded `400 invalid_params` before evaluating, and takes no budget |
| Sidecar unreachable (connection refused, connect timeout) | `503` | Not performed; a queued send returns to pending |
| Sidecar says "not logged in" | `503` | As above |
| Sidecar refused `SIDECAR_TOKEN` (`401`/`403`) | `503` | As above; not shown to the agent as its own `401` |
| Archive locked or unreadable | `503` | As above (every archive read comes before any sidecar call) |
| Read timeout or reset after the request was sent, a garbled `2xx`, a redirect, the sidecar's `502` or any other `5xx` | `502` | Outcome unknown: the budget stays reserved and a queued send becomes `failed` and is **never** retried |

The sidecar client never follows redirects and ignores proxy environment
variables, so `X-Internal-Token` can only ever go to `SIDECAR_URL`. Its
timeout (25 s) is shorter than the broker's plugin timeout (30 s), so a
hanging sidecar produces the plugin's own clean `502` before the broker gives
up.

## Operational notes

- **Read-only WAL archive.** `wa_data` is mounted read-only here. The archive
  is opened with `mode=ro` and `PRAGMA query_only`, per call. Verified under
  Docker for 0.2.0 (`docs/deployment.md` > Verify after `docker compose up`,
  step 7):
  - *Sidecar running.* The sidecar creates `messages.db`, `-wal` and `-shm`
    at start, before pairing. SQLite reads the WAL through the sidecar's
    `-shm`, which it maps read-only. 1,000 reads interleaved with 3,000
    single-row commits and several checkpoints by a stand-in writer all
    answered 200, and the newest row never went backwards.
  - *Sidecar stopped cleanly* (`docker compose stop`). The sidecar closes
    the archive on SIGTERM, so SQLite folds the WAL into `messages.db` and
    removes `-wal` and `-shm`. The plugin cannot recreate them on a
    read-only mount, so reads answer `503` (never stale or partial data)
    until the sidecar starts again. No plugin restart is needed.
  - *Sidecar crashed* (SIGKILL, OOM). `-wal` and `-shm` stay behind. SQLite
    rebuilds the WAL index in heap memory from the `-wal` file, because it
    cannot write the `-shm`, and reads answer 200 with the last committed
    rows. No writer is alive, so nothing is partial.
  - *Before the sidecar has created `messages.db`*, reads return empty
    results.

  The mount stays read-only on purpose. A read-write mount (WA_GW's setup,
  with `mode=ro` in the URI) would give this container write access to
  `session.db`, the WhatsApp session. `?immutable=1` would make SQLite
  ignore the WAL and the sidecar's locks, so reads could be stale or torn
  while the sidecar writes. A separate writable place for the `-shm` does
  not exist: SQLite keeps it beside the database, and it must be the very
  file the sidecar maps, or the plugin would not see the sidecar's commits.
- **Long-poll bootstrap.** Call `check_new_messages` once without a cursor,
  then long-poll with `?wait=` and the cursor. A call without a cursor is
  answered at once whatever `wait` says (engine rule for every `long_poll`
  action that declares a `cursor`), so nothing that arrives during a wait is
  skipped.
- **Labels.** `/label` (used for approval cards, for example "Send to Alice:
  …") and `/resolve` (the console's pickers) apply no visibility. They serve
  the owner. The broker filters `resolve` itself for agents.

## Development

```bash
pip install -e plugin-runtime -e "plugins/whatsapp[dev]"    # from the repo root
cd plugins/whatsapp && python -m pytest                      # the plugin's own tests
cd broker && python -m pytest tests/targets                  # end to end through the broker
```

`plugins/whatsapp/tests/fakes.py` holds the archive schema (it must change in
lockstep with `sidecars/whatsapp/internal/store/store.go`), a seeded archive,
and a scripted sidecar behind `httpx.MockTransport`. The broker's end-to-end
test loads the same file.
