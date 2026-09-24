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
| `check_new_messages` | read, long-poll | Incoming messages after `cursor`, oldest first: `{"cursor", "items"}`. Called with no cursor, it returns the current top of the archive and no backlog, so the agent starts "from now". Messages older than 5 minutes are never counted as new: history sync re-inserts old messages as new rows. |
| `search_contacts` | read | Address-book search by name or phone fragment. It returns JIDs. |
| `get_media` | read, binary | Downloads a message's media through the sidecar. The plugin checks the chat's visibility before calling the sidecar. |
| `send_message` | write | Sends a text message through the sidecar. It can be drafted and scheduled; the draft routing and the queue live in the broker. |

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

`normalize` (for both `chat` and `contact`) is WA_GW's `normalize_jid`,
unchanged. It mirrors the sidecar's `ParseRecipient`, and a test runs the Go
test's own vectors against it. It accepts `…@s.whatsapp.net`, `…@g.us`,
`…@lid` or an international phone number, and it strips device (`:N`) and
agent (`.N`) suffixes. One rule is added: the user part must be canonical
(digits, or `digits-digits` for an old-style group). Hidden lists and grants
compare exact strings, so a spelling that WhatsApp might still deliver to the
same account (`+972…@s.whatsapp.net`, a space or an invisible character
inside the digits) is refused with `400` and cannot act as a second name for a
hidden chat.

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
   QR pairing"`. It is enabled but not usable, and every agent call returns
   `503 not_connected`.
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
| Bad recipient, empty or oversized text (the sidecar reads at most 64 KiB), bad params, malformed scope | `400`, before any sidecar call | Passed through |
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
  is opened with `mode=ro` and `PRAGMA query_only`. SQLite reads a WAL
  database through the writer's `-shm` file, so archive reads work while the
  sidecar is running. If the sidecar is stopped and has cleaned up its `-shm`,
  reads return `503` (never stale or partial data). Before the sidecar has
  created `messages.db`, reads return empty results.
- **Long-poll bootstrap.** Call `check_new_messages` once *without* a cursor,
  then long-poll with `?wait=` and the cursor. The broker's wait loop ends
  early only when a list is non-empty. A bootstrap call always returns an
  empty list, so a bootstrap sent with `wait` holds for the whole wait and
  returns the top of the archive at the end.
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
