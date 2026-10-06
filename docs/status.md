# Status and caveats

Version 0.3.0, single owner. This page lists the verified items, the items
not yet verified, and the limits an operator must know. The README links
here instead of holding them.

## Verified

- **External plugins and the installer.** The acceptance test in
  `docs/deployment.md` ran under Docker on a laptop and on the production
  server. From the console, it installed the echo fixture from a public
  repository and the finance plugin from a private one. Then it removed and
  purged them.
- **The images and the local stack.** The images build, and the stack runs
  as uid 10001 with only the broker published. `plugin-whatsapp` reads the
  sidecar's WAL-mode archive through its read-only mount. A stand-in writer
  exercised that mount before pairing. A paired phone runs on the production
  server. CI validates the compose files with `docker compose config`, builds
  the plugin base image, and builds no other image.
  `docs/deployment.md` lists the checks to run after `docker compose up`, with
  the expected outputs.

## Not verified

- **Google downscoped refresh against the real endpoint.** The plugin rejects
  any token wider than it asked for. If Google ignores the requested subset,
  the manifests' `scopes` narrowing moves to `proxy` and does not claim target
  enforcement.

## Limits and caveats

- **WhatsApp runs through whatsmeow, an unofficial client.** Meta's terms do
  not permit it, and Meta can ban accounts. The WhatsApp session is the one
  credential not encrypted at rest. It is whatsmeow's own store in
  `wa_session`, a volume only the sidecar mounts. A backup of that volume is
  the live account.
- **Single owner, no identity provider.** With no token exchange, the Google
  refresh token and the GitHub App key stay long-term inside their plugin
  containers, and only there. There is no 2FA or passkey support yet.
- **The decision record is tamper-evident, not tamper-proof.** Anyone with
  `DECISION_SIGNING_KEY` and write access to `broker.db` can rewrite it.
- **Budgets belong to grants.** Each approved expansion adds its own daily
  budget to a key.
- **Extra MCP hostnames apply at the next broker start.** Every other console
  setting applies on the next request.
- **The broker restarts on each plugin install, upgrade and remove.** The
  installer recreates the broker container because the plugin's network and
  the broker's environment are compose-level settings. The job panel polls
  through the restart. A restart-free design is a known follow-up.
