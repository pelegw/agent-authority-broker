# Google plugins (`plugin-google`: `gmail`, `gcal`, `gdrive`)

One container serves three plugin ids, because they share one Google
account: one OAuth client, one refresh token, one encrypted secret slot. The
broker reaches the plugin service over the internal plugin API
(`docs/plugin-api.md`). The broker never holds a Google credential of any
kind.

```
broker ──(net_google, X-Plugin-Token, X-Plugin-Id: gmail|gcal|gdrive)──> plugin-google ──(HTTPS)──> oauth2.googleapis.com
                                                                              │                     gmail / calendar / drive APIs
                                                                              └── /secrets (google_secrets): client id + secret,
                                                                                  refresh token, connect state (encrypted)
```

- **Package:** `plugins/google/aab_plugin_google/`. It has `connection.py`
  (the `google_oauth` connection), `client.py` (the HTTP client that all
  three share) and `adapters/{gmail,gcal,gdrive}.py` with their helpers.
  `aab_plugin_runtime` serves them from `main.py`.
- **Manifests:** `plugins/google/aab_plugin_google/manifests/{gmail,gcal,gdrive}.yaml`
  are the source of truth. The broker vendors byte-identical copies at
  `broker/broker/targets/<id>/manifest.yaml`.
  `broker/tests/targets/test_google.py` fails on any drift. The broker pins
  id and version and uses its own copy.

## Enforcement: what Google enforces and what only this plugin does

| | Target-enforced (Google rejects) | Proxy-enforced (this plugin, then the broker's post-filter) |
|---|---|---|
| Gmail | read vs write, by scope: `gmail.readonly` for every read | `label`, `contact`, `domain`, `date_window_days`, `attachments`, `bcc`, `mark_read`, hidden threads/labels |
| Calendar | `calendar.readonly` for reads, `calendar.events` for writes | `calendar`, `attendee`, `visibility`, `time_window_days`, `private_events`, `others_events`, hidden calendars |
| Drive | `drive.readonly` for reads, `drive` for writes | `folder` (subtree), `mime`, `shared_drives`, `file_content`, `max_download_mb`, `external_sharing`, hidden files/folders |

Every call runs on an access token that the connection mints for **exactly**
the scopes of the current action. These scopes are the `target_permissions`
of the manifest, which the broker sends as the CallScope `credential`. The
connection refreshes with `grant_type=refresh_token&scope=<that subset>`. It
caches the token in memory by the exact sorted scope set. It refreshes again
when less than five minutes remain. Thus the calls of a read-only key hold a
token that Google itself rejects for writes, whatever the code of this
plugin does. `get_my_access` reports `enforced_where.scopes = "target"` and
every other dimension as `proxy`. `/status` reports `enforcement: "mixed"`.

- **The plugin verifies downscoping on every refresh.** Suppose Google
  returns a token with a scope that the plugin did not request. Then the
  plugin rejects the token (503) and does not cache it. `/status` says
  "refusing tokens: Google returned more scopes than requested". A claim of
  target enforcement for a wider token would be a lie, so the plugin stops.
  If this occurs for real, set the `scopes` narrowing of the manifests to
  `proxy`. Do not work around it.
- **Nothing finer than a scope is target-enforced.** Google's Credential
  Access Boundaries exist only for Cloud Storage. These boundaries give downscoped
  tokens restricted to particular resources. There is no Gmail token limited to a
  label, no Calendar token limited to one calendar, and no Drive token
  limited to a folder. Everything in the right-hand column above is the code
  of this plugin, plus the `resource_ref` post-filter of the broker. The
  broker reports it as such.
- **`drive.file` is not a substitute for folder narrowing.** It reaches only
  files that this app created, or that the user explicitly opened with it
  through the Picker. It does not reach "folder X and everything under it".
  The plugin enforces a grant on a folder by walking the parent chain,
  below.
- **`delete_thread` needs full mail access.** Gmail deletes permanently only
  with `https://mail.google.com/`. Thus the token of `delete_thread` is not
  narrower than the account. If you enable Gmail, the consent screen asks for
  that scope. Keys that do not hold `delete_thread` never get a token with
  that scope.
- **Replies need `gmail.metadata`.** `send` and `create_draft` accept a
  `thread_id` to reply in a thread. The plugin first checks that thread
  against the visibility of the call (labels, contacts, hidden). It reads
  the `Message-ID` of the thread to thread the reply, which needs a read
  scope. Thus they carry `gmail.metadata` (headers and labels only, no
  bodies), not `gmail.readonly`.

## Setting up Google

### 1. The OAuth client

Do these steps in Google Cloud console, in a project of your own:

1. **Enable the APIs** that you will use: Gmail API, Google Calendar API,
   Google Drive API.
2. **OAuth consent screen:** choose user type *Internal* if the account is
   in a Google Workspace organization that you administer. Otherwise, choose
   *External*. Add the scopes below, or let the consent request add them.
   With *External* and publishing status *Testing*, add your own address as
   a test user. Google then expires refresh tokens after seven days, so you
   must reconnect weekly. To avoid that, publish the app (unverified, for
   your own use) or use an *Internal* app. Gmail's scopes are "restricted".
   They show an "unverified app" warning until Google verifies the app.
3. In **Credentials → Create OAuth client ID →**, choose the application
   type **Web application**. Under *Authorized redirect URIs*, register
   exactly these URIs.

   | Deployment | Redirect URI |
   |---|---|
   | Public (`docker-compose.public.yml`) | `https://<SITE_DOMAIN>/oauth/callback/google` |
   | Local | `http://localhost:<BROKER_PORT>/oauth/callback/google` (default port 8080) |

   The broker calculates the redirect URI. It passes the URI on every
   connect. In public mode, it uses `SITE_DOMAIN`. It never uses the `Host`
   header, and it rejects the connect if `SITE_DOMAIN` is missing. Locally,
   it uses the address that you opened the console at. Google accepts plain
   `http` only for `localhost` and `127.0.0.1`. The plugin rejects any other
   `http` redirect before Google sees it. Open the console at the address
   that you registered.

### 2. The scopes the consent screen asks for

The consent screen asks for the union of the `target_permissions` of the
**enabled** Google plugins:

| Plugin | Scopes |
|---|---|
| Gmail | `gmail.readonly`, `gmail.metadata`, `gmail.compose`, `gmail.send`, `gmail.modify`, `https://mail.google.com/` (for `delete_thread` only) |
| Calendar | `calendar.readonly`, `calendar.events` |
| Drive | `drive.readonly`, `drive` |

Short names stand for `https://www.googleapis.com/auth/<name>`. The consent
request uses these parameters:

- `access_type=offline`, for a refresh token.
- `prompt=consent`, so Google returns a refresh token every time.
- `include_granted_scopes=true`, for incremental grants.

If you untick a scope on the consent screen, the actions that need it
answer `403 scopes not granted; reconnect`. `/status` lists the scope under
`missing_scopes`.

### 3. In the console

1. **Google account form.** Gmail, Calendar and Drive show one shared form:
   `client_id` and `client_secret`, the manifest fields with `shared: true`.
   Enter the values one time, from any of the three. The broker keeps the
   client id identical on all three plugins in its DB. The broker sends the
   client secret one time to plugin-google, which stores it encrypted in its
   `google` slot. The broker never keeps the client secret.
2. **Enable** the plugins that you want. Enabling needs the client id.
3. **Connect.** One button connects the whole service. The consent page
   opens. After you approve, Google redirects to the
   `/oauth/callback/google` page of the broker. That page needs no
   credential of the owner. Google's redirect is cross-site, so the browser
   does not send the SameSite=Strict session cookie with it. Cloudflare
   Access still applies in public mode. The page holds no data. It strips
   `code` and `state` from the address bar. It POSTs them, same origin, with
   your session cookie and the CSRF header, to the admin-guarded
   `connect/finish`. That route sends them one time to plugin-google. If
   your console session has expired, the page says "log in to the console in
   another tab, then retry". The page keeps the code in memory only. The
   plugin checks the state and exchanges the code itself.

   The broker does not log the authorization code. Its access line for that
   `GET /oauth/callback/google` carries the path only, never the query
   string (`docs/logging.md`). The code is single-use and expires within
   minutes. It is useless without the client secret, and only plugin-google
   holds that secret.
4. **Enabling another Google plugin later** shows it as
   `reconnect needed: scopes missing` until you connect again.
5. **Disconnect** revokes the refresh token at Google (best effort) and
   wipes it. The client id and secret stay.

## The connection (`google_oauth`)

- **State nonce:** 32 random bytes. The plugin stores the nonce as a SHA-256
  hash in the encrypted slot, with the redirect URI, the requested scopes and
  a 10-minute expiry. **Single use:** any `/connect/finish` attempt, right or
  wrong, consumes it. Thus a guessed or replayed state gets exactly one try.
- **Code exchange:** at `https://oauth2.googleapis.com/token`, with the
  client secret and the stored redirect URI. An answer with no refresh token
  is an error. Remove the access of the app in your Google account and
  connect again.
- **A new client id** makes the refresh token invalid, because the token
  belongs to the old client. Thus the plugin wipes it.
- **Token errors:**
  - An unreachable token endpoint or a 5xx is `503` (the plugin did
    nothing).
  - `invalid_grant` on refresh (revoked or expired authorization) is `503`
    with health `reconnect required`.
  - `invalid_scope` is `403`.
- **Secrets:** access tokens live only in memory. They are never in a log
  line, an error, a `repr` or on disk. The refresh token, the client secret
  and the state are in `/secrets/google.secrets`, encrypted under
  `PLUGIN_SECRETS_KEY`. Only the client id appears in a URL: the consent
  URL, which is public anyway.

## What each plugin does

Every row that an agent receives and that names a thread, label, calendar,
file or folder carries `resource_ref`. Thus the post-filter of the broker
checks it again. Write results carry none, because a post-filter 404 on
"sent" would claim that the message did not go out. A hidden or out-of-grant
resource gives the same `404 not found` as a missing one. The plugin checks
deny sets **before** it asks Google about a hidden id at all.

### Gmail

A thread is visible when all of these conditions are true:

- Its id is not hidden.
- None of its labels (the union over its messages) is a hidden label.
- If the capability names labels, the thread carries one of them.
- None of its participants (From/To/Cc of any message, lowercase) is a
  denied contact.
- If the capability names contacts, one of them takes part.
- Under `date_window_days`, at least one message is in the window. The
  plugin shows only those messages.

Every thread that the API returns goes through that check.

- **`search_threads`:** the plugin also puts the label, contact and date
  restrictions into the Gmail query, for example
  `{label:work} -label:secret newer_than:30d`, with the query of the agent in
  parentheses. Thus Google returns fewer wasted results. But the plugin never
  trusts the injected terms. The query of an agent sits next to them and can
  try to re-group them. Also, Gmail's search syntax only approximates label
  names. The post-filter is the enforcement.
- **`get_thread`**, **`get_attachment`:** the plugin lists attachments by
  `part_id`, because Gmail's attachment ids change between fetches.
  `attachments: false` strips the list and rejects `get_attachment` before
  any call. Attachment types that a browser would show as a page go out as
  `application/octet-stream`.
- **`send`**, **`create_draft`:** plain text, built with the stdlib `email`
  package. The plugin rejects CR/LF in any header. It canonicalizes every
  recipient (to, cc, bcc), then checks it. A recipient outside the `contact`
  or `domain` allowlist is `403`. A denied contact or domain is `404`. The
  allowlist check runs first. Thus a `404` never tells an agent about a
  hidden address outside its grant. `bcc: false` rejects any bcc.
- **`label_thread`:** added and removed labels must be in the allowlist
  (`403`) and not hidden (`404`). This action cannot set TRASH, SPAM, DRAFT,
  SENT and CHAT. `mark_read` controls `UNREAD` instead: `false` rejects
  changes to the read state. Reads never change the read state at all. They
  run on a read-only token, which is a structural rule, not a check.
- **`archive_thread`**, **`trash_thread`**, **`delete_thread`:** the plugin
  first checks the thread as for a read, including the date window.

### Calendar

The plugin resolves `primary` to the real calendar id of the account before
it compares anything. Thus `primary` can never name a hidden calendar a
second way. The plugin compares calendar ids in lowercase. A hidden event is
an event whose id is on the hidden list. For an instance, the id of its
recurring event also counts. The plugin drops a hidden event from lists. A
hidden event is a `404` on every get, update, answer or delete, before the
plugin asks Google about it.

- **`visibility: freebusy`** reduces events to `{start, end, busy}`: no id,
  no title.
- **`time_window_days`** clamps `timeMin`/`timeMax` to [now − N, now + N]
  days. The plugin drops any event that the API returns outside that window.
  A get outside it is `404`. Times that the agent sends need a UTC offset,
  or `YYYY-MM-DD`.
- **`private_events: false`** hides private and confidential events. Thus
  the agent cannot update, answer or delete them either.
- **`others_events: false`** rejects updates and deletes of events that the
  account did not create (`creator.self`).
- **`attendee`** is an allowlist on create and update. The plugin emails
  invitations only with `notify_attendees: true`. `respond` changes only the
  account's own attendee entry, and notifies the organizer.
- **`freebusy`** checks every named calendar: outside the allowlist is
  `403`, hidden is `404`.

### Drive

- **Folder subtree:** a file is inside when it or one of its ancestors is a
  folder id that the grant permits. Hiding a folder hides everything under
  it. Ancestry comes only from the `parents` field of Drive. The plugin
  walks it by id, one hop at a time, and caches each id for 60 seconds. It
  never uses a path or a name from the agent. The broker asks for the same
  walk (`/resolve` with `relation: ancestors`) for its own subtree checks.
- **Incomplete chains:** when the plugin cannot read a hop (a file shared
  with the account from a folder that it cannot see), the allowlist check
  fails. If any folder is on the hidden list, such a file counts as hidden
  too. The plugin cannot exclude a hidden folder above the unreadable hop.
- **`root`** resolves to the real id of My Drive. **Shortcuts are not
  followed**, because a shortcut to a file outside the subtree would
  otherwise be a way out. Download the target by its own id.
- **`mime`:** the allowlist and the deny list apply to files. `folder`
  controls folders. Uploads must declare a permitted type.
- **`shared_drives: false`** never sends `supportsAllDrives`, so Drive
  itself hides shared-drive files. The plugin also drops any shared-drive
  row.
- **`file_content: false`** (metadata only) rejects `download_file` before
  any call. It makes `search_files` match names only, because a full-text
  match would reveal content.
- **`max_download_mb`:** the plugin checks it against the declared size
  before the download, and against the bytes after it. Thus it still rejects
  a lying size. The plugin exports Google Docs, Sheets and Slides as PDF.
- **`external_sharing: false`** rejects `anyone` links, and any user, group
  or domain outside the account's own domain. A consumer account (gmail.com)
  has no organization, so every principal counts as external.

## Why some names differ from the plan

The grant algebra treats an absent flag as `true`, and drops `true` as the
top value. Consider a flag whose `true` *restricts*. Normalization drops it
from every grant, and the flag fails open. Thus every flag here has a name
that makes `true` the permissive side. A test lints the names:

| Plan | Manifest | Restricting value |
|---|---|---|
| `hide_private` | `private_events` | `false` |
| `own_events_only` | `others_events` | `false` |
| `metadata_only` | `file_content` | `false` |

Version 0.2 **drops** the Calendar `hide_keyword` of the plan. It is a deny
list, and a constraint (scalar only) cannot express a deny list. Also, a
keyword filter on free text is easy to evade. Instead, the owner hides
individual events by id. `event` is hideable, and hiding a recurring event
hides every instance. As an alternative, the owner keeps sensitive events
private and grants `private_events: false`.

## Errors

The shared client maps the answers of Google onto the contract of the
broker:

- Never reached Google: `503`.
- Lost after sending: `503` for a read, because nothing changed. `502` for a
  write, because the write possibly occurred. The broker never retries it.
- `401`: the client drops the cached token and answers `503`.
- `429`, and `403` with rate-limit reasons: `429`.
- Other `4xx`: passed through. `404` is always the one `not found`.
- `5xx` and redirects: `503` for reads, `502` for writes. The client never
  follows redirects, because they would carry the bearer token.

## Environment

The container reads only the generic names of the runtime. Compose maps each
one from the `.env` entry of this service. No other service receives them.

| In the container | Fed from `.env` | Required |
|---|---|---|
| `PLUGIN_TOKEN` | `PLUGIN_TOKEN_GOOGLE` | yes; the runtime does not start when it is empty |
| `PLUGIN_SECRETS_KEY` | `PLUGIN_SECRETS_KEY_GOOGLE` | yes in compose |
| `PLUGIN_SECRETS_DIR` | `/secrets` (the `google_secrets` volume) | set by compose and by the image |

No Google value is in the environment. The client id and secret are console
configuration. The redirect URI comes from the broker with each connect. The
image runs `uvicorn --factory aab_plugin_google.main:create_app` with one
worker on `:8090`, as uid 10001. The token cache and the connect state live
in that one process.

## Known limits

- Post-filtering can make a page shorter than `limit`, because the plugin
  drops hidden or out-of-grant rows after Google paginates. Use
  `next_page_token`.
- Label and contact restrictions apply to a thread as a whole: the union of
  the labels and participants of its messages.
- `freebusy` returns Google's busy blocks, which carry no event identity.
  Time that a hidden or private event takes still shows as busy, but never
  what the event is.
- Since 2020, Drive has enforced a single parent per file. The plugin walks
  older multi-parent files through every parent, and any hidden ancestor
  hides them.
