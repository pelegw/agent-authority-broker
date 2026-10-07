# Deploying the Agent Authority Broker to EC2 behind Cloudflare

This puts the broker on a public EC2 instance, reachable only through
Cloudflare, with the admin/management plane gated by Cloudflare Access SSO.

> Status (v0.3.0): all three plugin services (`plugin-whatsapp`,
> `plugin-github`, `plugin-google`) are real, and every third-party
> credential (the Telegram bot token, the GitHub App or PAT, the Google
> OAuth client) is entered in the console, never in `.env`. The images and
> the read-only `wa_data` mount have not yet been verified by a real build
> and run, so do the checks in `docs/deployment.md` > "Verify after
> `docker compose up`" on the host after the first deploy. The topology,
> env split and volumes are explained in `docs/deployment.md`.
>
> External plugins (0.3.0): the opt-in plugin installer is in
> step 10. Its images (`installer/`, `plugins/base/`) and the install path
> have been tested without Docker only (fake Docker, local git
> repositories); run the acceptance test in `docs/deployment.md` > "External
> plugins: the installer" before relying on it.

**Threat model recap.** The origin is locked down three ways so nobody who
learns the EC2 IP can bypass Cloudflare: (1) the **security group** only accepts
:443 from Cloudflare's IP ranges, (2) **Caddy** requires Cloudflare's client
certificate (Authenticated Origin Pulls), and (3) the app rejects any request
missing the **`X-AAB-Origin` secret** that a Cloudflare Transform Rule injects.
Admin routes additionally require a **Cloudflare Access** identity, on top of
the owner's own login (session or `aab_admin_` token). The broker **refuses
to boot** in public mode if Access isn't configured. Agent routes are not
behind Access: agents authenticate with their `aab_` key.

Inside the host, the edge (Caddy) sits on `edge_net` with the broker only; the
plugin containers each sit on their own network with the broker only
(`net_whatsapp`, `net_github`, `net_google`: no plugin can reach another); the WhatsApp
sidecar sits on `wa_internal` with `plugin-whatsapp` only. The broker holds no
target credential: each plugin container keeps its own, encrypted in its own
volume.

---

## 0. Prerequisites

- An EC2 Linux instance (Amazon Linux 2023 or Ubuntu), **t3.small or larger**
  recommended (2 GB RAM builds the images comfortably; the provision script adds
  swap as a safety net).
- An **Elastic IP** associated with the instance (so the address is stable).
- A domain on Cloudflare; we'll use `aab.example.com` below. Substitute yours.
- Your `.pem` SSH key, and the AWS CLI configured locally (for the security-group
  script; the console works too).

## 0b. Windows laptops: the SSH key and rsync

`deploy/push.sh` needs an OpenSSH-format private key. A PuTTY `.ppk` converts
in one line (PuTTYgen is installed with PuTTY):

```bash
"/c/Program Files/PuTTY/puttygen.exe" key.ppk -O private-openssh -o ~/.ssh/aab-ec2.pem
chmod 600 ~/.ssh/aab-ec2.pem
```

Git Bash ships without `rsync`; the script then ships the committed tree with
`git archive` over SSH instead, so commit before you push and nothing
untracked (`.env`, a venv, a database) can ever leave the laptop.

## 0c. Reusing the host that runs WA_GW

Only one stack can own port 443. Install the broker beside WA_GW
(`/opt/aab` next to `/opt/wa-gw`), then stop WA_GW before the deploy that
starts the broker's edge:

```bash
cd /opt/wa-gw && docker compose -f docker-compose.yml -f docker-compose.public.yml down   # never -v
```

WA_GW's volumes stay on disk for rollback (`up -d` there restores it after a
`down` in `/opt/aab`). Use a new hostname (`aab.<domain>`), a new Transform
Rule (the header is `X-AAB-Origin`, not `X-WAGW-Origin`) and a new Access
application with `/oauth*`; the origin certificate can be reused if it is a
wildcard for the zone (`sudo cp /opt/wa-gw/edge/certs/* /opt/aab/edge/certs/`).
Nothing from WA_GW's `gateway.db` carries over (keys, drafts, grants are a
clean break; agents get new `aab_` keys). The WhatsApp pairing is a fresh QR
scan; the old archive is not migrated unless you copy `messages.db` into the
`aab_wa_data` volume before the first pairing (chown 10001).

## 1. Provision the host

```bash
scp -i key.pem deploy/provision-ec2.sh ec2-user@<host>:~
ssh -i key.pem ec2-user@<host> 'bash provision-ec2.sh'
```
Log out and back in afterwards (for the `docker` group). This installs Docker +
Compose and python3, adds 2 GB swap, and creates `/opt/aab`.

## 2. Lock the security group to Cloudflare

Allow :443 **only** from Cloudflare, and :22 only from your IP:

```bash
SG_ID=sg-0abc123 SSH_CIDR=$(curl -s ifconfig.me)/32 deploy/update-security-group.sh
```
(Or in the console: inbound 443 from each range at <https://www.cloudflare.com/ips/>,
22 from your IP, remove any `0.0.0.0/0` on 443.)

## 3. Cloudflare: DNS, TLS, origin cert

1. **DNS**: add an `A` record `aab` pointing at your Elastic IP, **Proxied** (orange cloud).
2. **SSL/TLS mode**: set to **Full (strict)**.
3. **Origin certificate**: SSL/TLS > Origin Server > *Create Certificate*. Save
   the cert and key to the host as:
   - `/opt/aab/edge/certs/origin.pem`
   - `/opt/aab/edge/certs/origin.key`
4. **Authenticated Origin Pulls**: SSL/TLS > Origin Server > enable it. Download
   Cloudflare's origin-pull CA and save it as
   `/opt/aab/edge/certs/cloudflare-origin-pull-ca.pem`.
   (See `edge/certs/README.md`. For strong per-zone mTLS, upload your own custom
   AOP certificate instead and pin it in `edge/Caddyfile`.)

## 4. Generate secrets on the host

The first run of `deploy/push.sh` (step 7) syncs the code and, finding no
`.env`, runs `python3 scripts/init_secrets.py --out /opt/aab/.env` **on the
host**. That writes, with mode 0600, the broker's secrets (`SETUP_TOKEN`,
`ORIGIN_SECRET`, `BROKER_SECRETS_KEY`, `DECISION_SIGNING_KEY`), and per plugin
service a token and a key (`PLUGIN_TOKEN_WHATSAPP` / `PLUGIN_SECRETS_KEY_WHATSAPP`,
`..._GITHUB`, `..._GOOGLE`) plus `SIDECAR_TOKEN` (plugin-whatsapp to sidecar),
then empty, labelled placeholders for the Cloudflare Access values and
`SITE_DOMAIN`; it prints a checklist (including which credentials you enter in
the console instead) and stops. Secret values are never printed and never
leave the host. Compose hands each
container only its own values (table in `docs/deployment.md`).

To rotate one value later: `python3 scripts/init_secrets.py --out .env --rotate NAME`
(the script tells you what else to update; per-secret procedures are in
`docs/deployment.md`).

## 5. Cloudflare: origin secret (Transform Rule)

Read the generated `ORIGIN_SECRET` on the host (`grep ^ORIGIN_SECRET= /opt/aab/.env`)
and add it as a request header on your hostname:
Rules > Transform Rules > **Modify Request Header** > *When incoming requests
match* `Hostname equals aab.example.com` > **Set static** `X-AAB-Origin` = `<the secret>`.

## 6. Cloudflare: Access on the admin plane, then finish `.env`

1. Zero Trust > Access > **Applications** > *Add a self-hosted application*.
2. Application domains: `aab.example.com/admin*`, `aab.example.com/auth*`,
   `aab.example.com/v1/admin*` **and** `aab.example.com/oauth*`. The last one
   covers the OAuth callback page (`/oauth/callback/<service>`), which is part
   of the admin plane: without it, connecting Google or GitHub fails in public
   mode because the callback cannot pass the broker's Access check.
3. Add a policy: *Allow* your email(s).
4. From the app's settings copy the **Application Audience (AUD) tag** and your
   **team domain** (`yourteam.cloudflareaccess.com`).
5. *(Optional, recommended)* Security > WAF > **Rate limiting rule** on the
   hostname as an edge first line of defense.

Then edit `/opt/aab/.env` on the host: set `SITE_DOMAIN`, `CF_ACCESS_ENABLED=true`,
`CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, and optionally `CF_ACCESS_ALLOWED_EMAILS`.
Leave `ALLOW_INSECURE_ADMIN=false`: the boot interlock is your safety net.
`SITE_DOMAIN` also becomes the host of the OAuth redirect URIs below, and the
public overlay adds it to the `/mcp` Host allowlist, so MCP clients can use
`https://aab.example.com/mcp` without further configuration.

## 6b. Register the OAuth redirect URIs (only for the plugins you use)

The connect flows for Google and GitHub redirect the owner's browser back to
the broker's public URL, which relays the one-time code to the plugin
container. Register exactly these (substitute your `SITE_DOMAIN`):

| Provider | Where | Redirect / callback URL |
|---|---|---|
| Google | Cloud Console > APIs & Services > Credentials > your OAuth client (Web application) > Authorized redirect URIs | `https://aab.example.com/oauth/callback/google` |
| GitHub | Your GitHub App > General > Post installation > **Setup URL** (tick "Redirect on update"); GitHub appends `installation_id` to it | `https://aab.example.com/oauth/callback/github` |

No credential goes into `/opt/aab/.env` for any of this. The Google OAuth
client id and secret and the GitHub App id, slug and private key are entered
in the console, in the plugin config forms (Plugins > GitHub; for Google,
the one Google account card that Gmail, Calendar and Drive share), which
relay the secret fields once to the plugin container; the broker never
stores them (`docs/configuration.md`, `docs/plugins/google.md`,
`docs/plugins/github.md`).
For the GitHub App private key you may instead place it on the host as
`/opt/aab/data/github-app/app.pem` (readable by uid 10001 only:
`sudo install -o 10001 -g 10001 -m 0400 app.pem /opt/aab/data/github-app/`)
and name `/run/secrets/github/app.pem` in the GitHub plugin's
`private_key_path` field; the plugin reads only files that resolve inside
`/run/secrets/github`. Only `plugin-github` mounts that directory,
read-only, and `deploy/push.sh` never syncs or deletes `data/`. The
Telegram bot token is entered in the console as well (Channels > Telegram).

## 7. Deploy

From your laptop:
```bash
HOST=ec2-user@<host> SSH_KEY=key.pem deploy/push.sh
```
This syncs the code (never your secrets, never `data/`, never the installed
plugins in `plugins.d/`) and runs `docker compose $(scripts/compose-files.sh)
up -d --build`: `scripts/compose-files.sh` prints the compose file set
(`-f docker-compose.yml -f docker-compose.public.yml` once `SITE_DOMAIN` is
set, plus the installer overlay and every installed plugin's overlay when the
installer is on), so nobody hand-lists files.
Only Caddy (:443) is exposed; the broker is reachable only from Caddy
(`edge_net`), and no plugin or sidecar port is ever published. The public
overlay is also what turns on origin lockdown (`ORIGIN_SECRET`); the base
compose file alone is local-only.

## 8. Create the owner account

Open **`https://aab.example.com/admin`**. Cloudflare Access prompts for SSO,
then the setup page asks for the `SETUP_TOKEN` from `/opt/aab/.env` and your
username and password. The token is inert once the owner exists.

For the `aab` CLI from your laptop, mint an admin token in the console
(Account > Admin tokens; shown once) and create a Cloudflare Access service
token for the same Access application, then export `AAB_URL`,
`AAB_ADMIN_TOKEN`, `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`. Both
layers are checked: the Access identity and the owner credential.

## 9. Verify

```bash
curl https://aab.example.com/v1/health          # {"status":"ok","version":"0.3.0"}
curl -s -o /dev/null -w '%{http_code}\n' https://<elastic-ip>/v1/health   # should FAIL/timeout: origin not directly reachable
curl -H "Authorization: Bearer $AAB_ADMIN_TOKEN" -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
     -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" https://aab.example.com/v1/admin/health   # 200 ok / 503 degraded: the owner's view, behind Access
curl -u uptimerobot:$AAB_MONITOR_TOKEN https://aab.example.com/v1/health   # the same verdict for a monitor token, outside Access: the line for UptimeRobot
```
`https://aab.example.com/v1/admin/*` should require Access; a request without the
Cloudflare secret header (i.e. straight to the origin) should get 403.

Then, on the host (`cd /opt/aab`, with `C="docker compose
$(scripts/compose-files.sh)"` and `$C` for every compose command), run the
checklist in
`docs/deployment.md` > "Verify after `docker compose up`": container health,
published ports (only `edge` on 443), the plugin cards in the console, the
read-only `wa_data` mount once WhatsApp is paired, and the decision-chain
verification.

## 10. External plugins (optional): the plugin installer

Plugins that live in their own repositories (the finance plugin,
`github.com/pelegw/aab-plugin-finance`) are installed from the console by
`aab-installer`, an opt-in container. It holds the Docker socket, so it is
**root on this host**; what bounds it is that only the broker can reach it
(`net_installer`), only with `INSTALLER_TOKEN`, only for repositories in
`INSTALLER_ALLOWED_SOURCES`, at the commit you reviewed, with an overlay it
renders itself (`docs/plugin-packaging.md`).

1. **The plugin base image (once per gateway version).** Plugin images
   build `FROM ghcr.io/pelegw/aab-plugin-base:<version>`, a private GHCR
   package of the gateway repository. Create a GitHub token with
   `read:packages` (classic; or fine-grained with Packages: read), then on
   the host:

   ```bash
   echo <token> | docker login ghcr.io -u <github user> --password-stdin
   docker pull ghcr.io/pelegw/aab-plugin-base:0.3.0     # the version the plugins name in FROM
   ```

   The pull matters: the installer drives the host's Docker daemon but has
   no registry credentials of its own (the login is stored in the deploying
   user's `~/.docker/config.json`, which the installer does not mount), so
   its builds use the base image from the daemon's image store. Pull again
   when a plugin moves to a newer base version.
2. **`.env` on the host** (`/opt/aab/.env`):

   ```bash
   INSTALLER_ENABLED=true
   INSTALLER_ALLOWED_SOURCES=github.com/pelegw/*     # env-only; empty refuses every install
   AAB_HOME=/opt/aab                                 # this checkout's path (REMOTE_DIR)
   ```

   `INSTALLER_TOKEN` is generated (`deploy/push.sh` appends it to an older
   `.env`, as it does `AAB_HOME`). The GitHub token for private plugin
   repositories is **not** a `.env` line: it is a console setting (step 4).
   An `INSTALLER_GIT_TOKEN=` line left in an older `.env` is ignored;
   delete it.
3. **Deploy**: `deploy/push.sh`. The compose file set now includes
   `docker-compose.installer.yml`: `aab-installer` starts, and the broker is
   recreated on `net_installer` with `INSTALLER_URL` and `INSTALLER_TOKEN`.
4. **Install**: console, Plugins, **+ Add plugin**. For a private
   repository, first paste a read-only GitHub token into "GitHub token for
   private plugin repositories" and choose Set: a fine-grained token whose
   repository access is only the plugin repositories, with Contents:
   read-only (or a classic token with `repo`, which reads everything the
   account can: prefer fine-grained). The broker stores it encrypted under
   `BROKER_SECRETS_KEY` and sends it to the installer only with inspect,
   install and upgrade; git gets it only through `GIT_ASKPASS`, for
   `github.com` only. Then `github.com/pelegw/aab-plugin-finance` and
   `v0.1.0`, Inspect, read the review, Install. The job panel follows the
   build; the broker keeps running (the installer connects it to the
   plugin's network); the card appears disabled; enable it. The next
   `deploy/push.sh` recreates the broker once into the same state.

Upgrade and remove are on the plugin's card (`docs/console.md`). Installed
plugins live in `/opt/aab/plugins.d/` (the installer's; `deploy/push.sh`
never syncs or deletes it) and each adds `PLUGIN_TOKEN_<SERVICE>` and
`PLUGIN_SECRETS_KEY_<SERVICE>` to `.env`. To turn the installer off, set
`INSTALLER_ENABLED=false` and redeploy: installed plugins keep running (their
overlays stay in the file set), only installing, upgrading and removing stop.

## Operations

- **Update**: re-run `deploy/push.sh`; it rebuilds and restarts in place.
- **Logs**: `ssh ... 'cd /opt/aab && docker compose $(scripts/compose-files.sh) logs -f broker'`
  (services: `edge`, `broker`, `plugin-whatsapp`, `whatsapp-sidecar`,
  `plugin-github`, `plugin-google`, and with the installer on `aab-installer`
  and each installed `plugin-<service>`). The WhatsApp pairing QR is shown in the
  console (Plugins > WhatsApp > Connect), printed in the `whatsapp-sidecar`
  log, and served as a PNG at `/v1/admin/plugins/whatsapp/connect/qr.png`.
  Add `--since 1h` to bound the output and `--no-log-prefix | grep <request-id>`
  to follow one request across services; `LOG_LEVEL` / `LOG_FORMAT` in `.env`
  set level and format, and Docker rotates each container's log at 5 x 10 MB
  (docs/logging.md).
- **Extra MCP hosts**: a console change to `mcp_allowed_hosts_extra`
  (Settings) takes effect at the next broker start:
  `docker compose $(scripts/compose-files.sh) restart broker`.
  Every other console setting applies on the next request.
- **Reboots**: `restart: unless-stopped` + `systemctl enable docker` (provision
  does this) bring the stack back automatically.
- **Backups**: back these up together to encrypted storage:

  | What | Holds | Needs, to be useful |
  |---|---|---|
  | `broker_data` volume | owner account, keys, grants, decision record, console settings, Telegram bot token and the installer's GitHub token (encrypted) | `DECISION_SIGNING_KEY` (old rows verify only under it), `BROKER_SECRETS_KEY` (else re-enter both tokens) |
  | `wa_session` volume | WhatsApp session (**plaintext**: a backup is the live account) | nothing: treat the backup itself as a credential |
  | `wa_data` volume | WhatsApp message archive | nothing (message content: keep it as private as the account) |
  | `whatsapp_secrets` volume | nothing today (plugin-whatsapp has no config to store) | `PLUGIN_SECRETS_KEY_WHATSAPP` |
  | `github_secrets` volume | GitHub plugin config (App id and slug, the key if pasted, the PAT if used) and the installation (encrypted) | `PLUGIN_SECRETS_KEY_GITHUB` |
  | `google_secrets` volume | Google OAuth client id and secret, refresh token (encrypted) | `PLUGIN_SECRETS_KEY_GOOGLE` |
  | `<service>_secrets` volume, per installed plugin | that plugin's own secret store (encrypted) | `PLUGIN_SECRETS_KEY_<SERVICE>` |
  | `<service>_*` volumes an installed plugin declares (e.g. `finance_data`) | that plugin's data (the finance database) | whatever the plugin documents; `finance_data` is plain SQLite |
  | `/opt/aab/plugins.d/` | each installed plugin's checkout at its pinned commit, rendered overlay and install record | nothing (rebuildable from the repository at the recorded commit, but restoring it avoids a reinstall); the pins themselves are in `broker_data` |
  | `/opt/aab/data/github-app/app.pem` | the GitHub App key, only if you use the file alternative | nothing: treat it as a credential |
  | `/opt/aab/.env` | every key above, plus tokens and the Cloudflare Access values | host-only, mode 0600 |

  SQLite files are only consistent when copied at rest, so stop the stack
  for the copy. For example, into a directory outside `/opt/aab` (which
  `deploy/push.sh` keeps in sync with `--delete`):

  ```bash
  cd /opt/aab
  C="docker compose $(scripts/compose-files.sh)"
  mkdir -p ~/aab-backup && chmod 700 ~/aab-backup
  $C stop
  # Every volume of the project: the fixed ones and each installed plugin's.
  for v in $(docker volume ls -q | grep '^aab_'); do
    docker run --rm -v $v:/v:ro -v ~/aab-backup:/b alpine tar czf /b/$v.tgz -C /v .
  done
  $C start
  cp .env ~/aab-backup/env && chmod 600 ~/aab-backup/env
  tar czf ~/aab-backup/plugins.d.tgz --exclude=plugins.d/_installer plugins.d   # installed plugins, if any
  ```

  Then encrypt `~/aab-backup` before it leaves the host: it holds the live
  WhatsApp session in plaintext and, in `env`, every key needed to read the
  rest. Restore the volumes together with the `.env` they were taken with.

  Docker prefixes the volume names with the project name (`aab_broker_data`,
  ...). Removing an installed plugin keeps its volumes and comments its two
  `.env` secrets out (`#aab-retired# ...`), so a reinstall finds its data;
  removing it with purge deletes both for good, so back up first. Losing a
  `PLUGIN_SECRETS_KEY_<SERVICE>` means that plugin's old
  store must be cleared, its secret config re-entered and the plugin
  reconnected (`docs/deployment.md` > Rotating secrets); nothing else is
  lost. Losing `BROKER_SECRETS_KEY` means re-entering the Telegram bot
  token and, if you use one, the installer's GitHub token. Losing
  `DECISION_SIGNING_KEY` means the existing decision record
  no longer verifies. **Never** commit `.env`, `data/` or
  `edge/certs/*` (already gitignored).
