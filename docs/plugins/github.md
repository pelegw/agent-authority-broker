# GitHub plugin (`plugin-github`)

The GitHub plugin gives agents issues, pull requests, branches and files in
the repositories of a GitHub App installation. It works inside the limits of
the broker's grants. It is its own container. The broker reaches it over the
internal plugin API (`docs/plugin-api.md`). The broker never holds a GitHub
credential. The App's private key, the installation and every minted token
live only in this container.

```
broker ──(net_github, X-Plugin-Token)──> plugin-github ──(HTTPS, per-call installation token)──> api.github.com
                                               │
                                               └── /secrets (github_secrets): App key, installation id,
                                                   connect state, encrypted under PLUGIN_SECRETS_KEY
```

- **Package:** `plugins/github/aab_plugin_github/`. `aab_plugin_runtime`
  serves it.
- **Manifest:** `plugins/github/aab_plugin_github/manifest.yaml` is the
  source of truth. The broker keeps a vendored copy at
  `broker/broker/targets/github/manifest.yaml`. The copy must stay
  byte-identical: `broker/tests/targets/test_github.py` fails on any drift.
  The broker pins id and version and then uses its own copy.

## What is enforced where

With a GitHub App, **GitHub itself** enforces two dimensions. Every call gets
its own installation token, and GitHub rejects anything outside it. The
broker reports this per call in `enforced_where` (and `get_my_access`).

| Dimension | App mode | PAT mode | How |
|---|---|---|---|
| `repo` (list) | **target** | proxy | The token is minted for exactly the addressed repository (`list_repos`: the capability's repo list minus hidden/denied ones). |
| `permissions` (derived) | **target** | proxy | The token carries exactly the called action's `target_permissions`, for example `{contents: read}` for `get_file`. |
| `branch` (pattern) | proxy | proxy | The plugin checks the branch an action changes. GitHub has no per-branch token. |
| hidden repositories, key denies | proxy | proxy | 404 before any token is minted, and filtered out of every list. |
| `mode`, `budget` | proxy | proxy | Broker-side, as for every plugin. |

Know these two limits of target enforcement:

- **A token can never be wider than the installation.** Suppose a call
  needs a permission that the installation does not have. Then the plugin
  answers `403 installation lacks permission contents:write`, and it does
  not ask GitHub for a token. Give the App only the permissions that you
  want agents ever to have.
- **Only repositories of the installation's account are reachable.** GitHub
  resolves the `repositories` of the token by name inside the account of the
  installation. Thus a call cannot name `other-owner/x` at all. The plugin
  drops such repositories (404). It does not send `x`, which would mean
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
| `create_pr` | write | `pull_requests:write` | From a branch of the same repository (the plugin rejects `owner:branch` heads). |
| `merge_pr` | destructive | `contents:write`, `pull_requests:write` | Reads the PR, checks its **base** branch against the grant, then merges with the head `sha` it read, so a head that moved in between is a 409. |
| `delete_branch` | destructive | `contents:write` | A missing branch is 404. |

List results are `{"items": [...], "next_page": n | null}`. Every row and
every read result carries `"resource_ref": {"kind": "repo", "id":
"owner/name"}`. Thus the post-filter of the broker drops anything outside
the scope of the call, even if this plugin has a bug. Write results carry no
`resource_ref`, on purpose. The action is already complete, and a
post-filter 404 would tell the agent that it is not.

### Visibility and branches (the CallScope)

- **Hidden == missing.** A hidden or denied repository gives the same
  `404 not found` as a repository that does not exist, on every action. It
  is absent from `list_repos` and `resolve`. The check runs before minting.
  Thus no token for it ever exists, and GitHub never sees its name.
- **Canonical ids.** GitHub resolves `owner/name` case-insensitively. Thus
  the plugin lowercases both parts of an id (`Octo/Hello` is `octo/hello`).
  It accepts only ASCII before lowercasing, and it rejects `name.git`. The
  broker normalizes hidden lists and denies through the plugin. **The broker
  compares grant selectors as written**, so write repo ids in lowercase in
  grants.
- **Renamed repositories are not followed.** GitHub answers a renamed or
  transferred repository with a redirect. The plugin never follows it,
  because it never checked the new name against the scope. It answers 404.
- **`allow_only: []` means no repositories at all**, never "unrestricted".
  A malformed scope is a 400.
- **Branches are exact strings**, as the grant algebra defines `pattern`. A
  grant that names `feat/*` reaches only a branch with the literal name
  `feat/*`, which git forbids. The checked branch is the one that the action
  changes:
  - `branch` for `create_branch`/`push_file`/`delete_branch`.
  - `head` for `create_pr` (opening a PR changes no branch).
  - The `base` of the PR for `merge_pr` (what a merge writes to).

  Outside the selector: 403. In the denies of the key: 404. Reads have no
  branch restriction.
- **Content is data.** The plugin returns issue, comment and file text as
  is. The text can refer to anything, including hidden repositories.

## Creating the GitHub App

1. Go to GitHub > Settings > Developer settings > GitHub Apps >
   **New GitHub App**. For an organization App, use the settings of the
   organization.
2. **Homepage URL**: anything, for example the URL of your broker.
3. **Setup URL** (under "Post installation"):
   `https://<SITE_DOMAIN>/oauth/callback/github`. Tick **Redirect on
   update**, so that re-configuring the installation also comes back. Leave
   **Request user authorization (OAuth) during installation** off. The
   plugin never acts as a user and ignores `code`.
4. **Webhook**: untick *Active*. The plugin needs no events.
5. **Repository permissions**: this is the ceiling that the plugin cuts every
   token from. Give the App only the permissions that agents will ever need:

   | Permission | Level | Needed by |
   |---|---|---|
   | Metadata | Read (mandatory) | `list_repos`, every call |
   | Contents | Read, or Read and write | `get_file`; writes: `create_branch`, `push_file`, `merge_pr`, `delete_branch` |
   | Issues | Read, or Read and write | `list_issues`, `get_issue`; writes: `create_issue`, `comment_issue`, `close_issue` |
   | Pull requests | Read, or Read and write | `list_prs`; writes: `create_pr`, `merge_pr` |

   Nothing else (no Administration, no Workflows, no account permissions).
6. **Where can this GitHub App be installed?** *Only on this account.* Then
   only you can install it, so every installation of it is yours.
7. Create the App. Note the **App ID** and the **slug** (the last part of
   `https://github.com/apps/<slug>`). Under *Private keys*, click
   **Generate a private key** (a `.pem` download).

## Connecting

1. Open Console > Plugins > GitHub. Set `app_id` and `app_slug`. Paste the
   `.pem` into `private_key_pem`. The broker sends the private key one time
   to the plugin. The plugin validates it: it must be an unencrypted RSA key.
   The plugin rejects and wipes anything else. It stores the private key only
   in `github_secrets`. The database of the broker gets `app_id`, `app_slug`
   and the *name* of the secret field, never its value.

   As an alternative, put the private key in the `GITHUB_APP_KEY_DIR` bind
   (see Environment). Leave `private_key_pem` empty. Set `private_key_path`
   to `/run/secrets/github/app.pem`. The plugin reads only files that resolve
   inside `/run/secrets/github`, with symlinks followed. It rejects a path
   that is anywhere else, missing, or not a valid key. A console session must
   never be able to point it at another file, such as `/proc/self/environ`.
   A private key in `private_key_pem` wins over the file.
2. **Enable** the plugin. Before installation, it reports
   `connected: false` and
   `health: "App configured but not installed: use connect"`. Every agent
   call that the grant of a key covers is `503 not_connected`. Any other
   call is the usual `403 out_of_grant`.
3. **Connect**: the console gets `{"kind": "install", "url":
   "https://github.com/apps/<slug>/installations/new?state=…", "state"}`.
   The plugin generates and stores the `state` nonce. The nonce is single
   use and lasts 10 minutes. Any finish attempt, right or wrong, consumes it.
4. On GitHub, choose the account. Then choose **All repositories** or
   **Only select repositories**. The repository set of the installation is
   the outer ceiling, and grants narrow within it. GitHub redirects to the
   Setup URL with `installation_id` and `state`. The callback page needs no
   credential of the owner, because it puts nothing from the URL into the
   page. It sends both values to
   `POST /v1/admin/plugins/github/connect/finish`. That route requires the
   session of the owner and the CSRF header of the console. The broker logs
   neither the installation id nor the state. Its access line for
   `GET /oauth/callback/github` carries the path only, never the query
   string (`docs/logging.md`). The state is single-use and expires within
   minutes.
5. The plugin checks the state. Then it verifies the installation with an
   App JWT (`GET /app/installations/{id}`). The installation must exist and
   belong to this App. Only then does the plugin store the installation id
   and its account. Status now shows `connected: true`,
   `enforcement: "target"`, the installed permissions and the repository
   count.

**Disconnect** forgets the installation, the pending state and every cached
token. In PAT mode, it also wipes the PAT, which *is* the connection there.
The App id and the private key stay, so reconnecting needs no new upload.
Disconnect does not uninstall the App on GitHub. To make GitHub forget it
too, uninstall it under *Installed GitHub Apps* of the account.

### Status

`/status` always reports `mode` and `enforcement` from local config, even
when GitHub is unreachable. The broker stores it as `last_health`. Thus the
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

If you set `pat` and configure no App, the plugin runs in PAT mode. PAT mode
exists for trying the broker out, and for accounts where an App is not an
option. Know what you give up:

- **Nothing is target-enforced.** GitHub cannot narrow a PAT per call. The
  plugin sends the same PAT for every call. Only the broker and this plugin
  enforce every restriction (repo, permissions, branch, hidden).
  `enforced_where` says `proxy` for every dimension.
- **The PAT is the ceiling.** A classic PAT with `repo` scope reaches every
  repository that you can access, including those of other owners. Prefer a
  **fine-grained PAT** that GitHub restricts to specific repositories and to
  the permissions in the table above. Then GitHub still bounds the worst
  case, even though it cannot narrow per call.
- A PAT expires on the schedule of GitHub and acts as you. Your account is
  the author of commits and comments, not an App bot.
- If you configure both an App and a PAT, the App wins and the plugin does
  not use the PAT.

## Tokens

- **Minting.** App JWT (RS256, `iat` 60 s back, `exp` 10 minutes ahead,
  `iss` = App ID) → `POST /app/installations/{id}/access_tokens`. The request
  has `permissions` = the requirement of the call and `repositories` =
  repository names. The plugin rejects a requirement with no permissions,
  because GitHub would read it as "everything installed".
- **Checked on arrival.** The plugin rejects a token whose permissions or
  repositories are wider than the request (503). It never uses or caches
  that token.
- **Cached in memory only.** The plugin caches each token by the exact
  (installation, repositories, permissions) tuple. It keeps a token for at
  most 50 minutes, and never within 5 minutes of expiry. The plugin never
  logs a token, never writes it to disk, never puts it in a `repr` and never
  returns it over the plugin API. The plugin evicts a token that GitHub
  rejects (401).
- Cost: at most one mint per repository and permission set in use, per 50
  minutes.

## Environment

| In the container | Fed from `.env` / value | Read by | Required |
|---|---|---|---|
| `PLUGIN_TOKEN` | `PLUGIN_TOKEN_GITHUB` | runtime: the broker's `X-Plugin-Token` | yes; the runtime does not start when it is empty |
| `PLUGIN_SECRETS_KEY` | `PLUGIN_SECRETS_KEY_GITHUB` | runtime: the Fernet key for `/secrets` | yes in compose |
| `PLUGIN_SECRETS_DIR` | `/secrets` (the `github_secrets` volume) | runtime | set by compose and the image |

No GitHub-specific value is in the environment. Compose reads
`GITHUB_APP_KEY_DIR` itself (default `./data/github-app`). Compose
bind-mounts that directory read-only at `/run/secrets/github`, into this
container only. Install the private key there as uid 10001, mode 0400. Name
it in `private_key_path` in the console. The App ID, slug, private key and
PAT are console config. The image runs
`uvicorn --factory aab_plugin_github.main:create_app` with one worker on
`:8090`, as uid 10001. The healthcheck only checks that the port accepts a
connection, because every route needs the token.

`github_secrets` holds two encrypted files:

- `github.secrets`: `app_id`, `app_slug`, `private_key_path`,
  `private_key_pem` and `pat`. This is the console config. The plugin
  persists it, so that a container restart does not lose it.
- `github_app.secrets`: the installation id and account, and the pending
  state.

`/configure` can only ever write the first file. Thus no config that reaches
`/configure` can plant an installation.

## Errors: 503 versus 502

| What happened | Plugin answers | Broker behaviour |
|---|---|---|
| Hidden, denied, out of scope, missing, renamed (redirect) | `404 not found` | The one generic `not found` body |
| Bad params, bad ids, malformed scope, GitHub validation (422) | `400` | Passed through |
| Branch outside the grant | `403` | Passed through |
| Permission not installed | `403 installation lacks permission …` | Passed through |
| Conflict: branch exists, file changed, PR head moved, not mergeable | `409` | Passed through |
| GitHub rate limit (primary or secondary) | `429`, `Retry-After`, "retry after N s" | Budget released; a queued action is retried later |
| GitHub unreachable (connect failure); credential rejected (401); not installed; minting failed; a lookup *before* a write failed | `503` | The plugin did nothing: budget released, queued action returns to pending |
| The write was sent and then timed out, got a 5xx, or an unreadable 2xx | `502` | Outcome unknown: budget kept, queued action `failed`, never retried |

Reads that time out after sending are also `502`. The plugin never follows
redirects and ignores proxy environment variables. Thus a token can only go
to `api.github.com`. The GitHub timeout of the plugin (25 s) is below the
plugin timeout of the broker (30 s).

## Operational notes

- **Page gaps.** `list_repos` removes hidden repositories after GitHub pages
  the list. Thus a page can be short. The plugin calculates `next_page`
  before filtering. This is acceptable at personal scale.
- **Picker and labels.** `/resolve` is the repository picker of the console.
  It lists the repositories of the installation with an internal
  `metadata:read` token, cached for 5 minutes. `/label` returns ids as
  labels without calling GitHub. Neither applies visibility. They serve the
  owner, and the broker filters `resolve` itself for agents.

## Development

```bash
pip install -e plugin-runtime -e "plugins/github[dev]"    # from the repo root
cd plugins/github && python -m pytest                    # the plugin's own tests
cd broker && python -m pytest tests/targets              # end to end through the broker
```

`plugins/github/tests/fakes.py` is a scripted GitHub behind
`httpx.MockTransport`. It does these things:

- It checks App JWTs.
- It mints installation tokens with exactly the requested scope. It answers
  422 for anything that the installation does not have.
- It enforces the tokens on every repository endpoint.

Thus the tests can show GitHub itself rejecting what the broker rejected.
The end-to-end test of the broker loads the same file.
