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
- **Install and remove without a broker restart.** Under Docker on a laptop,
  the echo fixture was installed and removed with the same broker container
  id and start time. The broker joined and left `aab_net_echo`. An agent
  call answered 200 after the install and 404 after the remove. A
  `docker restart` of the broker kept the network and found echo again. A
  later `up -d` recreated the broker once into the same state.
- **The images and the local stack.** The images build, and the stack runs
  as uid 10001 with only the broker published. `plugin-whatsapp` reads the
  sidecar's WAL-mode archive through its read-only mount. A stand-in writer
  exercised that mount before pairing. A paired phone runs on the production
  server. CI validates the compose files with `docker compose config`, builds
  the plugin base image, and builds no other image.
  `docs/deployment.md` lists the checks to run after `docker compose up`, with
  the expected outputs.

## Not verified

- **Install without a broker restart on the production server, and an
  upgrade end to end.** The public echo fixture has one tag, so the laptop
  run did no upgrade. The tests cover the upgrade with the real installer
  and a recording Docker.
- **Shipping to New Relic.** Nobody has sent data to a real New Relic
  account yet: it needs the owner's license key. These parts were checked
  locally under Docker:
  - The overlay, with `docker compose config`.
  - The shipper's configuration, with Fluent Bit's `--dry-run`.
  - Docker's `fluentd` driver feeding the shipper's pipeline, with a stdout
    output in place of New Relic.
  - A service that starts and keeps logging while the shipper is down.
  - The audit exporter in the broker image, reading a read-only volume while
    another container held the database open. The full stack did not run.
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
- **The next deploy recreates the broker once after a plugin install or
  remove.** An install, upgrade or remove never restarts the broker. The
  installer connects the running broker to the plugin's network, or
  disconnects it. The broker reads the plugin's URL and token from the
  installer. The overlay still declares both for the broker, so the next
  full `docker compose up -d` recreates the broker once into the same state.
  The merge rule: for a service that the installer lists, the installer's
  URL and token win over the broker's environment. A service that it
  stopped listing is dropped. Every other service comes from the
  environment.
