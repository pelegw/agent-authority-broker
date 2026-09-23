# Origin certificates (git-ignored)

Place three files here before running the public overlay:

- `origin.pem` / `origin.key` — a **Cloudflare Origin Certificate**
  (Cloudflare dashboard → SSL/TLS → Origin Server → Create Certificate).
  These are trusted only between Cloudflare and your origin.
- `cloudflare-origin-pull-ca.pem` — the Cloudflare **Authenticated Origin Pull**
  CA, so the origin can require Cloudflare's client certificate. Download the
  current PEM from Cloudflare's docs (search "authenticated origin pull
  origin-pull-ca.pem") and save it here.

Then enable Authenticated Origin Pulls for the zone/hostname in the Cloudflare
dashboard (SSL/TLS → Origin Server → Authenticated Origin Pulls).
