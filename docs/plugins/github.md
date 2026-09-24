# GitHub plugin (`plugin-github`)

The GitHub plugin gives agents issues, pull requests, branches and files in
the repositories a GitHub App is installed on, inside whatever the broker's
grants allow. It is its own container. The broker reaches it over the
internal plugin API (`docs/plugin-api.md`) and never holds a GitHub
credential: the App's private key, the installation and every minted token
live only here.

```
broker ──(broker_net, X-Plugin-Token)──> plugin-github ──(HTTPS, per-call installation token)──> api.github.com
                                               │
                                               └── /secrets (github_secrets): App key, installation id,
                                                   connect state, encrypted under PLUGIN_SECRETS_KEY
```

- **Package:** `plugins/github/aab_plugin_github/`, served by
  `aab_plugin_runtime`.
- **Manifest:** `plugins/github/aab_plugin_github/manifest.yaml` is the
  source of truth. The broker keeps a vendored copy at
  `broker/broker/targets/github/manifest.yaml` that must stay
  byte-identical (`broker/tests/targets/test_github.py` fails on any drift).
  The broker pins id and version and then uses its own copy.

## What is enforced where

With a GitHub App, two dimensions are enforced **by GitHub itself**: every
call gets its own installation token, and GitHub refuses anything outside it.
The broker reports this per call in `enforced_where` (and `get_my_access`).

| Dimension | App mode | PAT mode | How |
|---|---|---|---|
| `repo` (list) | **target** | proxy | The token is minted for exactly the addressed repository (`list_repos`: the capability's repo list minus hidden/denied ones). |
| `permissions` (derived) | **target** | proxy | The token carries exactly the called action's `target_permissions`, e.g. `{contents: read}` for `get_file`. |
| `branch` (pattern) | proxy | proxy | The plugin checks the branch an action changes. GitHub has no per-branch token. |
| hidden repositories, key denies | proxy | proxy | 404 before any token is minted, and filtered out of every list. |
| `mode`, `budget` | proxy | proxy | Broker-side, as for every plugin. |

Two limits of target enforcement worth knowing:

- **A token can never be wider than the installation.** If a call needs a
  permission the installation was not granted, the plugin answers `403
  installation lacks permission contents:write` without asking GitHub for a
  token. Grant the App only what you want agents ever to have.
- **Only repositories of the installation's account are reachable.** GitHub
  resolves the token's `repositories` by name inside the installation's
  account, so `other-owner/x` cannot be named at all; the plugin drops such
  repositories (404) instead of sending `x`, which would mean
  `<account>/x`. One plugin instance serves one installation (one user or
  organization).

## Actions

| Action | Kind | Token permissions | What happens |
|---|---|---|---|
| `list_repos` | read | `metadata:read` | Repositories you can see (`id` = lowercase `owner/name`). |
| `list_issues` | read | `issues:read` | Issues (pull requests included, flagged `pull_request`). |
| `get_issue` | read | `issues:read` | One issue, its body and its first 100 comments. |
| `get_file` | read | `contents:read` | UTF-8 text, else base64, with the blob `sha`; or a directory listing. Over 1 MB: 400. |
| `list_prs` | read | `pull_requests:read` | Pull requests; a fork's name is never shown (`cross_repository: true`). |
| `create_issue` | write, schedulable | `issues:write` | Opens an issue. |
| `comment_issue` | write, schedulable | `issues:write` | Comments on an issue or a pull request's conversation. |
| `close_issue` | write | `issues:write` | Closes as `completed` or `not_planned`. |
| `create_branch` | write | `contents:write` | From `from_ref` or the default branch; an existing branch is 409. |
| `push_file` | write | `contents:write` | One file, one commit. Reads the current blob `sha` first (or takes yours), so a concurrent change is a 409, never an overwrite. |
| `create_pr` | write | `pull_requests:write` | From a branch of the same repository (`owner:branch` heads are refused). |
| `merge_pr` | destructive | `contents:write`, `pull_requests:write` | Reads the PR, checks its **base** branch against the grant, then merges with the head `sha` it read, so a head that moved in between is a 409. |
| `delete_branch` | destructive | `contents:write` | A missing branch is 404. |

List results are `{"items": [...], "next_page": n | null}`. Every row and
every read result carries `"resource_ref": {"kind": "repo", "id":
"owner/name"}`, so the broker's post-filter drops anything the call may not
see even if this plugin had a bug. Write results carry no `resource_ref`
on purpose: the action has happened, and a post-filter 404 would tell the
agent it had not.

### Visibility and branches (the CallScope)

- **Hidden == missing.** A hidden or denied repository is the same `404 not
  found` as one that does not exist, on every action, and absent from
  `list_repos` and `resolve`. The check runs before minting: no token for
  it ever exists and GitHub never sees its name.
- **Canonical ids.** GitHub resolves `owner/name` case-insensitively, so
  ids are lowercased in both parts (`Octo/Hello` is `octo/hello`); only
  ASCII is accepted before lowercasing, and `name.git` is refused. Hidden
  lists and denies are normalized through the plugin; **grant selectors are
  compared as written**, so write repo ids in lowercase in grants.
- **Renamed repositories are not followed.** GitHub answers a renamed or
  transferred repository with a redirect; the plugin never follows it (the
  new name was never checked against the scope) and answers 404.
- **`allow_only: []` means no repositories at all**, never "unrestricted".
  A malformed scope is a 400.
- **Branches are exact strings**, as the grant algebra defines `pattern`: a
  grant naming `feat/*` reaches only a branch literally named `feat/*`
  (which git forbids). The checked branch is the one the action changes:
  `branch` for `create_branch`/`push_file`/`delete_branch`, `head` for
  `create_pr` (opening a PR changes no branch), the PR's `base` for
  `merge_pr` (what a merge writes to). Outside the selector: 403; in the
  key's denies: 404. Reads are not branch-restricted.
- **Content is data.** Issue, comment and file text is returned as is; it may
  mention anything, including hidden repositories.

## Creating the GitHub App

1. GitHub > Settings > Developer settings > GitHub Apps > **New GitHub App**
   (under an organization's settings for an organization App).
2. **Homepage URL**: anything, e.g. your broker's URL.
3. **Setup URL** (under "Post installation"): `https://<SITE_DOMAIN>/oauth/callback/github`.
   Tick **Redirect on update** so re-configuring the installation also
   comes back. Leave **Request user authorization (OAuth) during
   installation** off: the plugin never acts as a user and ignores `code`.
4. **Webhook**: untick *Active*. The plugin needs no events.
5. **Repository permissions**, the ceiling every token is cut from. Grant
   only what agents may ever need:

   | Permission | Level | Needed by |
   |---|---|---|
   | Metadata | Read (mandatory) | `list_repos`, every call |
   | Contents | Read, or Read and write | `get_file`; writes: `create_branch`, `push_file`, `merge_pr`, `delete_branch` |
   | Issues | Read, or Read and write | `list_issues`, `get_issue`; writes: `create_issue`, `comment_issue`, `close_issue` |
   | Pull requests | Read, or Read and write | `list_prs`; writes: `create_pr`, `merge_pr` |

   Nothing else (no Administration, no Workflows, no account permissions).
6. **Where can this GitHub App be installed?** *Only on this account.* Then
   only you can install it, so every installation of it is yours.
7. Create it, note the **App ID** and the **slug** (the last part of
   `https://github.com/apps/<slug>`), and under *Private keys* **Generate a
   private key** (a `.pem` download).

## Connecting

1. Console > Plugins > GitHub: set `app_id`, `app_slug` and paste the
   `.pem` into `private_key_pem`. The key is relayed once to the plugin,
   validated (an unencrypted RSA key; anything else is refused and wiped)
   and stored only in `github_secrets`. The broker's database gets
   `app_id` and `app_slug` and the *name* of the secret field, never its
   value. Alternatively put the key in the `GITHUB_APP_KEY_DIR` bind (see
   Environment), leave `private_key_pem` empty and set `private_key_path`
   to `/run/secrets/github/app.pem`. The plugin reads only files that
   resolve (symlinks followed) inside `/run/secrets/github`, and refuses a
   path that is anywhere else, missing, or not a valid key: a console
   session must never be able to point it at another file such as
   `/proc/self/environ`. A key set in `private_key_pem` wins over the file.
2. **Enable** the plugin. Before installation it reports `connected: false`,
   `health: "App configured but not installed: use connect"`, and every
   agent call is `503 not_connected`.
3. **Connect**: the console gets `{"kind": "install", "url":
   "https://github.com/apps/<slug>/installations/new?state=…", "state"}`.
   The `state` nonce is generated and stored by the plugin: single use,
   10 minutes, and consumed by any finish attempt, right or wrong.
4. On GitHub choose the account and **All repositories** or **Only select
   repositories**: the installation's repository set is the outer ceiling
   that grants narrow within. GitHub redirects to the Setup URL with
   `installation_id` and `state`; the callback page relays both to
   `POST /v1/admin/plugins/github/connect/finish`.
5. The plugin checks the state, then verifies the installation with an App
   JWT (`GET /app/installations/{id}`: it must exist and belong to this
   App), and only then stores the installation id and its account. Status
   now shows `connected: true`, `enforcement: "target"`, the installed
   permissions and the repository count.

**Disconnect** forgets the installation, the pending state and every cached
token (in PAT mode it also wipes the PAT, which *is* the connection there).
The App id and key stay, so reconnecting needs no new upload. It does not
uninstall the App on GitHub: do that under the account's *Installed GitHub
Apps* if you want GitHub to forget it too.

### Status

`/status` (the broker stores it as `last_health`) always reports `mode` and
`enforcement` from local config, even when GitHub is unreachable, so the
broker never guesses:

| Situation | `connected` | `healthy` | `enforcement` |
|---|---|---|---|
| nothing configured | false | false | proxy |
| App configured, not installed | false | false | target |
| App installed, GitHub answers | true | true | target |
| App installed, GitHub unreachable | true | false | target |
| installation deleted on GitHub, or GitHub rejects the App JWT | false | false | target |
| PAT accepted | true | true | proxy |
| PAT rejected by GitHub | false | false | proxy |

## PAT fallback (proxy only)

Setting `pat` with no App configured runs the plugin in PAT mode. It exists
for trying the broker out, and for accounts where an App is not an option.
Know what you give up:

- **Nothing is target-enforced.** GitHub cannot narrow a PAT per call; the
  plugin sends the same PAT for every call, and every restriction (repo,
  permissions, branch, hidden) is enforced only by the broker and this
  plugin. `enforced_where` says `proxy` for every dimension.
- **The PAT is the ceiling.** A classic PAT with `repo` scope reaches every
  repository you can access, including other owners'. Prefer a
  **fine-grained PAT** restricted at GitHub to specific repositories and the
  permissions in the table above: GitHub then still bounds the worst case,
  even though it cannot narrow per call.
- A PAT expires on GitHub's schedule and acts as you (commits and comments
  are authored by your account, not an App bot).
- If both an App and a PAT are configured, the App wins and the PAT is
  unused.

## Tokens

- **Minting.** App JWT (RS256, `iat` 60 s back, `exp` 10 minutes ahead,
  `iss` = App ID) → `POST /app/installations/{id}/access_tokens` with
  `permissions` = the call's requirement and `repositories` = repository
  names. A requirement with no permissions is refused (GitHub would read
  it as "everything installed").
- **Checked on arrival.** A token whose permissions or repositories are
  wider than requested is refused (503) and never used or cached.
- **Cached in memory only**, keyed by the exact (installation, repositories,
  permissions) tuple, for at most 50 minutes and never within 5 minutes of
  expiry. Never logged, never written to disk, never in a `repr`, never
  returned over the plugin API. A token GitHub refuses (401) is evicted.
- Cost: at most one mint per repository and permission set in use per 50
  minutes.

## Environment

| In the container | Fed from `.env` / value | Read by | Required |
|---|---|---|---|
| `PLUGIN_TOKEN` | `PLUGIN_TOKEN_GITHUB` | runtime: the broker's `X-Plugin-Token` | yes; boot refuses when empty |
| `PLUGIN_SECRETS_KEY` | `PLUGIN_SECRETS_KEY_GITHUB` | runtime: the Fernet key for `/secrets` | yes in compose |
| `PLUGIN_SECRETS_DIR` | `/secrets` (the `github_secrets` volume) | runtime | set by compose and the image |

Nothing GitHub-specific is env. `GITHUB_APP_KEY_DIR` (read by compose on the
host, default `./data/github-app`) is bind-mounted read-only at
`/run/secrets/github`, into this container only; install the key there as
uid 10001, mode 0400, and name it in the console's `private_key_path`. The
App ID, slug, key and PAT are console config. The image runs `uvicorn --factory
aab_plugin_github.main:create_app` with one worker on `:8090`, as uid 10001;
the healthcheck only checks that the port accepts a connection (every route
needs the token).

`github_secrets` holds two encrypted files: `github.secrets` (`app_id`,
`app_slug`, `private_key_path`, `private_key_pem`, `pat`: the console config, persisted so a
container restart does not lose it) and `github_app.secrets` (installation
id and account, pending state). `/configure` can only ever write the first,
so no config relay can plant an installation.

## Errors: 503 versus 502

| What happened | Plugin answers | Broker behaviour |
|---|---|---|
| Hidden, denied, out of scope, missing, renamed (redirect) | `404 not found` | The one generic `not found` body |
| Bad params, bad ids, malformed scope, GitHub validation (422) | `400` | Passed through |
| Branch outside the grant | `403` | Passed through |
| Permission not installed | `403 installation lacks permission …` | Passed through |
| Conflict: branch exists, file changed, PR head moved, not mergeable | `409` | Passed through |
| GitHub rate limit (primary or secondary) | `429`, `Retry-After`, "retry after N s" | Budget released; a queued action is retried later |
| GitHub unreachable (connect failure); credential refused (401); not installed; minting failed; a lookup *before* a write failed | `503` | Not performed: budget released, queued action returns to pending |
| The write was sent and then timed out, got a 5xx, or an unreadable 2xx | `502` | Outcome unknown: budget kept, queued action `failed`, never retried |

Reads that time out after sending are also `502`. Redirects are never
followed and proxy environment variables are ignored, so a token can only
go to `api.github.com`. The plugin's GitHub timeout (25 s) is below the
broker's plugin timeout (30 s).

## Operational notes

- **Page gaps.** `list_repos` removes hidden repositories after GitHub pages
  the list, so a page can be short; `next_page` is computed before
  filtering. Acceptable at personal scale.
- **Picker and labels.** `/resolve` (the console's repository picker)
  lists the installation's repositories through an internal
  `metadata:read` token, cached 5 minutes; `/label` returns ids as labels
  without calling GitHub. Neither applies visibility: they serve the owner,
  and the broker filters `resolve` for agents itself.

## Development

```bash
pip install -e plugin-runtime -e "plugins/github[dev]"    # from the repo root
cd plugins/github && python -m pytest                    # the plugin's own tests
cd broker && python -m pytest tests/targets              # end to end through the broker
```

`plugins/github/tests/fakes.py` is a scripted GitHub behind
`httpx.MockTransport`: it checks App JWTs, mints installation tokens with
exactly the requested scope (422 on anything the installation lacks), and
enforces them on every repository endpoint, so the tests can show GitHub
itself refusing what the broker refused. The broker's end-to-end test loads
the same file.
