# Deploying the Agent Authority Broker to EC2 behind Cloudflare

This puts the broker on a public EC2 instance, reachable only through
Cloudflare, with the admin/management plane gated by Cloudflare Access SSO.

> Status: v0.2.0 is under construction. This runbook covers the deploy
> mechanics, which are already final; the owner setup flow (step 8) lands in
> phase 1 and the WhatsApp plugin in phase 4.

**Threat model recap.** The origin is locked down three ways so nobody who
learns the EC2 IP can bypass Cloudflare: (1) the **security group** only accepts
:443 from Cloudflare's IP ranges, (2) **Caddy** requires Cloudflare's client
certificate (Authenticated Origin Pulls), and (3) the app rejects any request
missing the **`X-AAB-Origin` secret** that a Cloudflare Transform Rule injects.
Admin routes additionally require a **Cloudflare Access** identity. The broker
**refuses to boot** in public mode if Access isn't configured.

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
host**. That writes `SETUP_TOKEN`, `SIDECAR_TOKEN`, `ORIGIN_SECRET`,
`BROKER_SECRETS_KEY` and `DECISION_SIGNING_KEY` with mode 0600, plus empty,
labelled placeholders, prints a checklist, and stops. Secret values are never
printed and never leave the host.

To rotate one value later: `python3 scripts/init_secrets.py --out .env --rotate NAME`
(the script tells you what else to update).

## 5. Cloudflare: origin secret (Transform Rule)

Read the generated `ORIGIN_SECRET` on the host (`grep ^ORIGIN_SECRET= /opt/aab/.env`)
and add it as a request header on your hostname:
Rules > Transform Rules > **Modify Request Header** > *When incoming requests
match* `Hostname equals aab.example.com` > **Set static** `X-AAB-Origin` = `<the secret>`.

## 6. Cloudflare: Access on the admin plane, then finish `.env`

1. Zero Trust > Access > **Applications** > *Add a self-hosted application*.
2. Application domains: `aab.example.com/admin*`, `aab.example.com/auth*` **and**
   `aab.example.com/v1/admin*`.
3. Add a policy: *Allow* your email(s).
4. From the app's settings copy the **Application Audience (AUD) tag** and your
   **team domain** (`yourteam.cloudflareaccess.com`).
5. *(Optional, recommended)* Security > WAF > **Rate limiting rule** on the
   hostname as an edge first line of defense.

Then edit `/opt/aab/.env` on the host: set `SITE_DOMAIN`, `CF_ACCESS_ENABLED=true`,
`CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, and optionally `CF_ACCESS_ALLOWED_EMAILS`.
Leave `ALLOW_INSECURE_ADMIN=false`: the boot interlock is your safety net.

## 7. Deploy

From your laptop:
```bash
HOST=ec2-user@<host> SSH_KEY=key.pem deploy/push.sh
```
This syncs the code (never your secrets) and runs
`docker compose -f docker-compose.yml -f docker-compose.public.yml up -d --build`.
Only Caddy (:443) is exposed; the broker stays on the internal network. The
public overlay is also what turns on origin lockdown (`ORIGIN_SECRET`); the
base compose file alone is local-only.

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
- **Logs**: `ssh ... 'cd /opt/aab && docker compose -f docker-compose.yml -f docker-compose.public.yml logs -f broker'`.
- **Reboots**: `restart: unless-stopped` + `systemctl enable docker` (provision
  does this) bring the stack back automatically.
- **Backups**: the `wa_data` volume is your WhatsApp session + archive;
  `broker_data` holds the owner account, keys, grants and the decision record.
  Back both up to encrypted storage, together with `.env` (without
  `BROKER_SECRETS_KEY` the stored plugin credentials can't be decrypted).
  **Never** commit `.env` or `edge/certs/*` (already gitignored).
