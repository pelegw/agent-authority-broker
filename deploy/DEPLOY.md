# Deploying the Agent Authority Broker to EC2 behind Cloudflare

This puts the broker on a public EC2 instance, reachable only through
Cloudflare, with the admin/management plane gated by Cloudflare Access SSO.

> Status: v0.2.0 is under construction. This runbook covers the deploy
> mechanics, which are already final. The plugin containers
> (`plugin-whatsapp`, `plugin-github`, `plugin-google`) are placeholders that
> only idle until phases 4, 6 and 7 fill them in. The topology, env split and
> volumes are explained in `docs/deployment.md`.

**Threat model recap.** The origin is locked down three ways so nobody who
learns the EC2 IP can bypass Cloudflare: (1) the **security group** only accepts
:443 from Cloudflare's IP ranges, (2) **Caddy** requires Cloudflare's client
certificate (Authenticated Origin Pulls), and (3) the app rejects any request
missing the **`X-AAB-Origin` secret** that a Cloudflare Transform Rule injects.
Admin routes additionally require a **Cloudflare Access** identity. The broker
**refuses to boot** in public mode if Access isn't configured.

Inside the host, the edge (Caddy) sits on `edge_net` with the broker only; the
plugin containers sit on `broker_net` with the broker only; the WhatsApp
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
then empty, labelled placeholders; it prints a checklist and stops. Secret
values are never printed and never leave the host. Compose hands each
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

## 6b. Register the OAuth redirect URIs (only for the plugins you use)

The connect flows for Google and GitHub redirect the owner's browser back to
the broker's public URL, which relays the one-time code to the plugin
container. Register exactly these (substitute your `SITE_DOMAIN`):

| Provider | Where | Redirect / callback URL |
|---|---|---|
| Google | Cloud Console > APIs & Services > Credentials > your OAuth client (Web application) > Authorized redirect URIs | `https://aab.example.com/oauth/callback/google` |
| GitHub | Your GitHub App > General > Post installation > **Setup URL** (tick "Redirect on update"); GitHub appends `installation_id` to it | `https://aab.example.com/oauth/callback/github` |

Put `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` and `GITHUB_APP_ID`
in `/opt/aab/.env`. For the GitHub App private key, either upload it from the
console when connecting, or place it on the host as
`/opt/aab/data/github-app/app.pem` (readable by uid 10001 only:
`sudo install -o 10001 -g 10001 -m 0400 app.pem /opt/aab/data/github-app/`)
and set `GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github/app.pem`. Only
`plugin-github` mounts that directory.

## 7. Deploy

From your laptop:
```bash
HOST=ec2-user@<host> SSH_KEY=key.pem deploy/push.sh
```
This syncs the code (never your secrets) and runs
`docker compose -f docker-compose.yml -f docker-compose.public.yml up -d --build`.
Only Caddy (:443) is exposed; the broker is reachable only from Caddy
(`edge_net`), and no plugin or sidecar port is ever published. The public
overlay is also what turns on origin lockdown (`ORIGIN_SECRET`); the base
compose file alone is local-only.

## 8. Create the owner account

Open **`https://aab.example.com/admin`**. Cloudflare Access prompts for SSO,
then the setup page asks for the `SETUP_TOKEN` from `/opt/aab/.env` and your
username and password. The token is inert once the owner exists.

## 9. Verify

```bash
curl https://aab.example.com/v1/health          # {"status":"ok","version":"0.2.0"}
curl -s -o /dev/null -w '%{http_code}\n' https://<elastic-ip>/v1/health   # should FAIL/timeout: origin not directly reachable
```
`https://aab.example.com/v1/admin/*` should require Access; a request without the
Cloudflare secret header (i.e. straight to the origin) should get 403.

## Operations

- **Update**: re-run `deploy/push.sh`; it rebuilds and restarts in place.
- **Logs**: `ssh ... 'cd /opt/aab && docker compose -f docker-compose.yml -f docker-compose.public.yml logs -f broker'`
  (services: `edge`, `broker`, `plugin-whatsapp`, `whatsapp-sidecar`,
  `plugin-github`, `plugin-google`). The WhatsApp pairing QR is printed in the
  `whatsapp-sidecar` log and served as a PNG at
  `/v1/admin/plugins/whatsapp/connect/qr.png`.
- **Reboots**: `restart: unless-stopped` + `systemctl enable docker` (provision
  does this) bring the stack back automatically.
- **Backups**: back these up together to encrypted storage:

  | What | Holds | Needs, to be useful |
  |---|---|---|
  | `broker_data` volume | owner account, keys, grants, decision record | `DECISION_SIGNING_KEY` (old rows verify only under it) |
  | `wa_data` volume | WhatsApp session (**plaintext**: a backup is the live account) + message archive | nothing: treat the backup itself as a credential |
  | `whatsapp_secrets` volume | plugin-whatsapp's encrypted config | `PLUGIN_SECRETS_KEY_WHATSAPP` |
  | `github_secrets` volume | GitHub App key + installation (encrypted) | `PLUGIN_SECRETS_KEY_GITHUB` |
  | `google_secrets` volume | Google OAuth refresh token (encrypted) | `PLUGIN_SECRETS_KEY_GOOGLE` |
  | `/opt/aab/.env` | every key above, plus tokens and third-party values | host-only, mode 0600 |

  Docker prefixes the volume names with the project name (`aab_broker_data`,
  ...). Losing a `PLUGIN_SECRETS_KEY_<SERVICE>` means that plugin must be
  reconnected; nothing else is lost. `BROKER_SECRETS_KEY` protects nothing in
  0.2.0 but back it up anyway. **Never** commit `.env`, `data/` or
  `edge/certs/*` (already gitignored).
