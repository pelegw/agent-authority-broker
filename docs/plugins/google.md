# Google plugins (`plugin-google`: `gmail`, `gcal`, `gdrive`)

One container hosts three plugin ids, because they share one Google account:
one OAuth client, one refresh token, one encrypted secret slot. The broker
reaches the service over the internal plugin API (`docs/plugin-api.md`) and
never holds a Google credential of any kind.

```
broker ──(net_google, X-Plugin-Token, X-Plugin-Id: gmail|gcal|gdrive)──> plugin-google ──(HTTPS)──> oauth2.googleapis.com
                                                                              │                     gmail / calendar / drive APIs
                                                                              └── /secrets (google_secrets): client id + secret,
                                                                                  refresh token, connect state (encrypted)
```

- **Package:** `plugins/google/aab_plugin_google/`: `connection.py` (the
  `google_oauth` connection), `client.py` (the HTTP client all three share),
  `adapters/{gmail,gcal,gdrive}.py` plus their helpers, served by
  `aab_plugin_runtime` from `main.py`.
- **Manifests:** `plugins/google/aab_plugin_google/manifests/{gmail,gcal,gdrive}.yaml`
  are the source of truth. The broker vendors byte-identical copies at
  `broker/broker/targets/<id>/manifest.yaml`, and
  `broker/tests/targets/test_google.py` fails on any drift. The broker pins
  id and version and uses its own copy.

## Enforcement: what Google enforces and what only this plugin does

| | Target-enforced (Google refuses) | Proxy-enforced (this plugin, then the broker's post-filter) |
|---|---|---|
| Gmail | read vs write, by scope: `gmail.readonly` for every read | `label`, `contact`, `domain`, `date_window_days`, `attachments`, `bcc`, `mark_read`, hidden threads/labels |
| Calendar | `calendar.readonly` for reads, `calendar.events` for writes | `calendar`, `attendee`, `visibility`, `time_window_days`, `private_events`, `others_events`, hidden calendars |
| Drive | `drive.readonly` for reads, `drive` for writes | `folder` (subtree), `mime`, `shared_drives`, `file_content`, `max_download_mb`, `external_sharing`, hidden files/folders |

Every call runs on an access token minted for **exactly** the scopes of the
action being performed (the manifest's `target_permissions`, sent by the
broker as the CallScope `credential`). The connection refreshes with
`grant_type=refresh_token&scope=<that subset>`, caches the token in memory
by the exact sorted scope set, and refreshes again when less than five
minutes remain. So a read-only key's calls hold a token Google itself
refuses to write with, whatever this plugin's code did. `get_my_access`
reports `enforced_where.scopes = "target"` and every other dimension as
`proxy`. `/status` reports `enforcement: "mixed"`.

- **Downscoping is verified on every refresh.** If Google ever returns a
  token with a scope that was not requested, the token is refused (503), is
  not cached, and `/status` says "refusing tokens: Google returned more
  scopes than requested". Claiming target enforcement for a token that is
  wider would be a lie; the plugin stops instead. If that ever happens for
  real, flip the manifests' `scopes` narrowing to `proxy` rather than work
  around it.
- **Nothing finer than a scope is target-enforced.** Google's Credential
  Access Boundaries (downscoped tokens restricted to particular resources)
  exist only for Cloud Storage. There is no Gmail token limited to a label,
  no Calendar token limited to one calendar, and no Drive token limited to a
  folder. Everything in the right-hand column above is this plugin's code
  (and the broker's `resource_ref` post-filter), and is reported as such.
- **`drive.file` is not a substitute for folder narrowing.** It reaches only
  files this app created or the user explicitly opened with it (via the
  Picker), not "folder X and everything under it". A grant on a folder is
  enforced by walking the parent chain, below.
- **`delete_thread` needs full mail access.** Gmail permanently deletes only
  with `https://mail.google.com/`. Its token is therefore not narrower than
  the account, and enabling Gmail makes the consent screen ask for it. Keys
  that do not hold `delete_thread` never get a token with that scope.
- **Replies need `gmail.metadata`.** `send` and `create_draft` accept a
  `thread_id` to reply in a thread. The plugin first checks that thread
  against the call's visibility (labels, contacts, hidden) and reads its
  `Message-ID` to thread the reply, which needs a read scope. They carry
  `gmail.metadata` (headers and labels only, no bodies) rather than
  `gmail.readonly`.

## Setting up Google

### 1. The OAuth client

In Google Cloud console, in a project of your own:

1. **Enable the APIs** you will use: Gmail API, Google Calendar API, Google
   Drive API.
2. **OAuth consent screen:** user type *Internal* if the account is in a
   Google Workspace organization you administer; otherwise *External*. Add
   the scopes below (or let the consent request add them). With *External*
   and publishing status *Testing*, add your own address as a test user, and
   note that Google then expires refresh tokens after seven days: you will
   have to reconnect weekly. Publishing the app (unverified, for your own
   use) or using an *Internal* app avoids that; Gmail's scopes are
   "restricted" and show an "unverified app" warning until verified.
3. **Credentials → Create OAuth client ID →** application type
   **Web application**. Under *Authorized redirect URIs* register exactly:

   | Deployment | Redirect URI |
   |---|---|
   | Public (`docker-compose.public.yml`) | `https://<SITE_DOMAIN>/oauth/callback/google` |
   | Local | `http://localhost:<BROKER_PORT>/oauth/callback/google` (default port 8080) |

   The broker computes the redirect URI and passes it on every connect: in
   public mode from `SITE_DOMAIN` (the Host header is never used; a missing
   `SITE_DOMAIN` refuses the connect), locally from the address you opened
   the console at. Google accepts plain `http` only for `localhost` and
   `127.0.0.1`, and the plugin refuses any other `http` redirect before
   Google sees it. Open the console at the address you registered.

### 2. The scopes the consent screen asks for

The union of the `target_permissions` of the **enabled** Google plugins:

| Plugin | Scopes |
|---|---|
| Gmail | `gmail.readonly`, `gmail.metadata`, `gmail.compose`, `gmail.send`, `gmail.modify`, `https://mail.google.com/` (for `delete_thread` only) |
| Calendar | `calendar.readonly`, `calendar.events` |
| Drive | `drive.readonly`, `drive` |

(Short names are `https://www.googleapis.com/auth/<name>`.) Consent is
requested with `access_type=offline` (a refresh token), `prompt=consent`
(Google returns one every time) and `include_granted_scopes=true`
(incremental grants). If you untick a scope on the consent screen, the
actions that need it answer `403 scopes not granted; reconnect` and
`/status` lists it under `missing_scopes`.

### 3. In the console

1. **Google account form.** Gmail, Calendar and Drive show one shared form
   (`client_id`, `client_secret`; manifest fields marked `shared: true`).
   Enter the values once, from any of the three. The client id is kept
   identical on all three plugins in the broker's DB; the client secret is
   relayed once to plugin-google, stored encrypted in its `google` slot, and
   never kept by the broker.
2. **Enable** the plugins you want. Enabling needs the client id.
3. **Connect** (one button for the whole service). The consent page opens;
   after you approve, Google redirects to the broker's
   `/oauth/callback/google` page. That page needs no owner credential
   (Google's redirect is cross-site, so the SameSite=Strict session cookie is
   not sent on it; Cloudflare Access still applies in public mode) and holds
   no data: it strips `code` and `state` from the address bar and POSTs them,
   same origin, with your session cookie and the CSRF header, to the
   admin-guarded `connect/finish`, which relays them once to plugin-google.
   If your console session has expired, the page says "log in to the console
   in another tab, then retry" and keeps the code in memory only. The plugin
   checks the state and exchanges the code itself.

   The authorization code is not logged: the broker's access line for
   that `GET /oauth/callback/google` carries the path only, never the query
   string (`docs/logging.md`). It is single-use, expires within minutes, and
   is useless without the client secret, which only plugin-google holds.
4. **Enabling another Google plugin later** shows it as
   `reconnect needed: scopes missing` until you connect again.
5. **Disconnect** revokes the refresh token at Google (best effort) and
   wipes it; the client id and secret stay.

## The connection (`google_oauth`)

- **State nonce:** 32 random bytes, stored (as a SHA-256 hash) in the
  encrypted slot with the redirect URI, the requested scopes and a 10-minute
  expiry. **Single use:** any `/connect/finish` attempt, right or wrong,
  consumes it, so a guessed or replayed state gets exactly one try.
- **Code exchange:** at `https://oauth2.googleapis.com/token` with the client
  secret and the stored redirect URI. No refresh token in the answer is an
  error (remove the app's access in your Google account and connect again).
- **A new client id** invalidates the refresh token (it belongs to the old
  client), so it is wiped.
- **Token errors:** unreachable token endpoint or a 5xx is `503` (nothing was
  performed); `invalid_grant` on refresh (revoked or expired authorization)
  is `503` with health `reconnect required`; `invalid_scope` is `403`.
- **Secrets:** access tokens live only in memory, never in a log line, an
  error, a `repr` or on disk; refresh token, client secret and state are in
  `/secrets/google.secrets`, encrypted under `PLUGIN_SECRETS_KEY`. Only the
  client id appears in a URL (the consent URL, which is public anyway).

## What each plugin does

Every row an agent receives that names a thread, label, calendar, file or
folder carries `resource_ref`, so the broker's post-filter re-checks it.
Write results carry none (a post-filter 404 on "sent" would claim it was
not sent). A hidden or out-of-grant resource is the same `404 not found` as
a missing one, and the plugin checks deny sets **before** asking Google
about a hidden id at all.

### Gmail

A thread is visible when its id is not hidden; none of its labels (the union
over its messages) is hidden and, if the capability names labels, it carries
one of them; none of its participants (From/To/Cc of any message, lowercase)
is a denied contact and, if the capability names contacts, one of them takes
part; and under `date_window_days` at least one message is in the window
(only those messages are shown). Every thread the API returns goes through
that check.

- **`search_threads`:** the label, contact and date restrictions are also
  injected into Gmail's query (`{label:work} -label:secret newer_than:30d`,
  with the agent's query in parentheses) so Google returns fewer wasted
  results, but the injected terms are never trusted: an agent's query sits
  next to them and could try to re-group them, and label names are only
  approximated in Gmail's search syntax. The post-filter is the enforcement.
- **`get_thread`**, **`get_attachment`:** attachments are listed by `part_id`
  (Gmail's attachment ids change between fetches). `attachments: false`
  strips the list and refuses `get_attachment` before any call. Attachment
  types that a browser would render as a page are served as
  `application/octet-stream`.
- **`send`**, **`create_draft`:** plain text, built with the stdlib `email`
  package; CR/LF in any header is refused. Every recipient (to, cc, bcc) is
  canonicalized, then checked: outside the `contact` or `domain` allow set is
  `403`; a denied contact or domain is `404`. The allow check runs first, so
  a `404` never tells an agent which addresses outside its grant are hidden.
  `bcc: false` refuses any bcc.
- **`label_thread`:** added and removed labels must be in the allow set
  (`403`) and not hidden (`404`); TRASH, SPAM, DRAFT, SENT and CHAT cannot be
  set this way. `UNREAD` is governed by `mark_read` instead: `false` refuses
  changing read state. Reads never change read state at all: they run on a
  read-only token (a structural guarantee, not a check).
- **`archive_thread`**, **`trash_thread`**, **`delete_thread`:** the thread is
  checked like a read first (including the date window).

### Calendar

`primary` is resolved to the account's real calendar id before anything is
compared, so it can never name a hidden calendar a second way. Calendar ids
are compared lowercase. A hidden event (by its id, or by its recurring
event's id for an instance) is dropped from lists and is a `404` on every
get, update, answer or delete, before Google is asked about it.

- **`visibility: freebusy`** reduces events to `{start, end, busy}`: no id,
  no title.
- **`time_window_days`** clamps `timeMin`/`timeMax` to [now − N, now + N]
  days and drops any event the API returns outside it; a get outside it is
  `404`. Times the agent sends need a UTC offset (or `YYYY-MM-DD`).
- **`private_events: false`** hides private and confidential events (and so
  they cannot be updated, answered or deleted either).
- **`others_events: false`** refuses updating or deleting events the
  account did not create (`creator.self`).
- **`attendee`** allow set on create and update; invitations are emailed only
  with `notify_attendees: true`. `respond` changes only the account's own
  attendee entry and notifies the organizer.
- **`freebusy`** checks every named calendar (outside the allow set `403`,
  hidden `404`).

### Drive

- **Folder subtree:** a file is inside when it or any ancestor is an allowed
  folder id; hiding a folder hides everything under it. Ancestry comes only
  from Drive's `parents` field, walked by id one hop at a time and cached for
  60 seconds per id; a path or name from the agent is never consulted. The
  broker asks the same walk (`/resolve` with `relation: ancestors`) for its
  own subtree checks.
- **Incomplete chains:** when a hop cannot be read (a file shared with the
  account from a folder it cannot see), the allow check fails, and while any
  folder is hidden such a file counts as hidden too: a hidden folder above
  the unreadable hop cannot be ruled out.
- **`root`** is resolved to My Drive's real id. **Shortcuts are not
  followed** (a shortcut to a file outside the subtree would otherwise be a
  way out); download the target by its own id.
- **`mime`** allow/deny applies to files; folders are governed by `folder`.
  Uploads must declare an allowed type.
- **`shared_drives: false`** never sends `supportsAllDrives` (Drive then
  hides shared-drive files itself) and drops any shared-drive row anyway.
- **`file_content: false`** (metadata only) refuses `download_file` before
  any call and makes `search_files` match names only (a full-text match
  would reveal content).
- **`max_download_mb`** is checked against the declared size before the
  download and against the bytes after it (a lying size is still refused).
  Google Docs, Sheets and Slides are exported as PDF.
- **`external_sharing: false`** refuses `anyone` links and any user, group or
  domain outside the account's own domain. A consumer account (gmail.com)
  has no organization, so every principal counts as external.

## Why some names differ from the plan

The grant algebra treats an absent flag as `true` and drops `true` as the top
value. A flag whose `true` *restricts* would therefore vanish from every
grant at normalization and fail open. Every flag here is phrased so that
`true` is the permissive side, and a test lints the names:

| Plan | Manifest | Restricting value |
|---|---|---|
| `hide_private` | `private_events` | `false` |
| `own_events_only` | `others_events` | `false` |
| `metadata_only` | `file_content` | `false` |

The plan's Calendar `hide_keyword` is **dropped** in 0.2: it is a deny list,
which a constraint (scalar only) cannot express, and a keyword filter on
free text is easy to evade anyway. The owner hides individual events by id
instead (`event` is hideable; hiding a recurring event hides every
instance), or keeps sensitive events private and grants
`private_events: false`.

## Errors

The shared client maps Google's answers onto the broker's contract: never
reached Google `503`; lost after sending `503` for a read (nothing changed)
but `502` for a write (it may have happened, never retried); `401` drops the
cached token and answers `503`; `429` and `403` rate-limit reasons are `429`;
other `4xx` pass through (`404` is always the one `not found`); `5xx` and
redirects (never followed: they would carry the bearer token) are `503` for
reads, `502` for writes.

## Environment

The container reads only the runtime's generic names; compose maps each from
this service's own `.env` entry and no other service receives them.

| In the container | Fed from `.env` | Required |
|---|---|---|
| `PLUGIN_TOKEN` | `PLUGIN_TOKEN_GOOGLE` | yes; boot refuses when it is empty |
| `PLUGIN_SECRETS_KEY` | `PLUGIN_SECRETS_KEY_GOOGLE` | yes in compose |
| `PLUGIN_SECRETS_DIR` | `/secrets` (the `google_secrets` volume) | set by compose and by the image |

No Google value is in the environment: the client id and secret are console
configuration, and the redirect URI comes from the broker with each connect.
The image runs `uvicorn --factory aab_plugin_google.main:create_app` with one
worker on `:8090`, as uid 10001 (the token cache and the connect state live
in that one process).

## Known limits

- Post-filtering can make a page shorter than `limit` (hidden or out-of-grant
  rows are dropped after Google paginated). Use `next_page_token`.
- Label and contact restrictions apply to a thread as a whole (the union of
  its messages' labels and participants).
- `freebusy` returns Google's busy blocks, which carry no event identity: time
  taken by a hidden or private event still shows as busy (never what it is).
- Drive has enforced a single parent per file since 2020; older multi-parent
  files are walked through every parent, and any hidden ancestor hides them.
