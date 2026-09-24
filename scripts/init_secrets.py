#!/usr/bin/env python3
"""Generate every deployment secret into a .env file (stdlib only).

Run once per deployment, before the first `docker compose up`:

    python scripts/init_secrets.py                  # writes <repo>/.env
    python scripts/init_secrets.py --out /opt/aab/.env
    python scripts/init_secrets.py --rotate SETUP_TOKEN
    python scripts/init_secrets.py --example        # prints the .env.example template

Why a script instead of "openssl rand" instructions: every secret gets the
right shape (Fernet keys must be urlsafe base64 of 32 bytes), the file is
created 0600 from the start, and nothing secret is ever printed, so the
command is safe to run in a recorded terminal or CI log.

The generated values are the broker's own secrets plus, per plugin service
(whatsapp, github, google), the token the broker presents to it
(PLUGIN_TOKEN_<SERVICE>) and the key that encrypts that service's own secret
volume (PLUGIN_SECRETS_KEY_<SERVICE>). One .env holds them all, but
docker-compose.yml hands each container only its own values: no service gets
the whole file.

Third-party credentials (GitHub App, Google OAuth client, Telegram bot token)
are NOT in this file: the owner enters them in the console, which relays
plugin credentials to the plugin that owns them and encrypts the Telegram
token under BROKER_SECRETS_KEY (docs/configuration.md). The file keeps only
what must exist before the database is readable, plus the public-mode
exposure values (Cloudflare Access, SITE_DOMAIN), which are written as empty,
labelled placeholders; a checklist of where to obtain each is printed.

`.env.example` in the repo is this file's `--example` output; a test keeps the
two identical so the template can't drift from what the script writes.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Entry:
    name: str
    comment: str
    # Exactly one of: a generator (broker-owned secret), a checklist hint
    # (third-party placeholder), or neither (plain setting with a default).
    generate: Callable[[], str] | None = None
    obtain: str | None = None
    default: str = ""


def _hex32() -> str:
    return secrets.token_hex(32)


def _fernet_key() -> str:
    # Fernet wants urlsafe base64 of exactly 32 random bytes (with padding).
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _setup_token() -> str:
    return secrets.token_urlsafe(32)


# (section title, entries). Order here is the order in the file.
SECTIONS: list[tuple[str, list[Entry]]] = [
    ("Broker secrets (generated; rotate with --rotate NAME; broker container only)", [
        Entry("SETUP_TOKEN",
              "One-time token for creating the owner account at /admin. Inert once setup completes.",
              generate=_setup_token),
        Entry("ORIGIN_SECRET",
              "Edge secret a Cloudflare Transform Rule adds as X-AAB-Origin. Used only by the public overlay.",
              generate=_hex32),
        Entry("BROKER_SECRETS_KEY",
              "Fernet key for secrets entered in the console and kept by the broker (the Telegram bot token). Never target credentials.",
              generate=_fernet_key),
        Entry("DECISION_SIGNING_KEY",
              "HMAC key for the hash-chained decision record. Keep it stable: old rows verify under it.",
              generate=_hex32),
    ]),
    ("Plugin service secrets (generated; each only to the containers named)", [
        Entry("PLUGIN_TOKEN_WHATSAPP",
              "Broker <-> plugin-whatsapp token (X-Plugin-Token). Broker and plugin-whatsapp only.",
              generate=_hex32),
        Entry("PLUGIN_SECRETS_KEY_WHATSAPP",
              "Fernet key for plugin-whatsapp's own secret volume. plugin-whatsapp only.",
              generate=_fernet_key),
        Entry("SIDECAR_TOKEN",
              "plugin-whatsapp <-> whatsapp-sidecar token (X-Internal-Token, wa_internal network). Never the broker.",
              generate=_hex32),
        Entry("PLUGIN_TOKEN_GITHUB",
              "Broker <-> plugin-github token (X-Plugin-Token). Broker and plugin-github only.",
              generate=_hex32),
        Entry("PLUGIN_SECRETS_KEY_GITHUB",
              "Fernet key for plugin-github's own secret volume (App key, installation). plugin-github only.",
              generate=_fernet_key),
        Entry("PLUGIN_TOKEN_GOOGLE",
              "Broker <-> plugin-google token (X-Plugin-Token). Broker and plugin-google only.",
              generate=_hex32),
        Entry("PLUGIN_SECRETS_KEY_GOOGLE",
              "Fernet key for plugin-google's own secret volume (OAuth refresh token). plugin-google only.",
              generate=_fernet_key),
    ]),
    ("Public-mode values (fill in for an internet deploy; see the checklist the script prints)", [
        Entry("CF_ACCESS_TEAM_DOMAIN",
              "Cloudflare Access team domain, e.g. myteam.cloudflareaccess.com (public mode only).",
              obtain="Cloudflare Zero Trust dashboard > Settings > Custom Pages > Team domain."),
        Entry("CF_ACCESS_AUD",
              "Audience (AUD) tag of the Access application on the admin plane (public mode only).",
              obtain="Zero Trust > Access > Applications > your app > Overview > Application Audience (AUD) Tag."),
        Entry("SITE_DOMAIN",
              "Public hostname Cloudflare proxies to this host, e.g. aab.example.com (public mode; "
              "also the OAuth redirect host).",
              obtain="Your Cloudflare DNS: the proxied (orange-cloud) record pointing at this host."),
    ]),
    ("Settings (defaults are fine for a local run)", [
        Entry("BROKER_PORT", "Port the broker publishes on 127.0.0.1 (local run).", default="8080"),
        Entry("DEVICE_NAME", "Name shown under WhatsApp > Linked devices (applied at pairing).",
              default="AAB"),
        Entry("TZ", "Timezone for logs.", default="UTC"),
        Entry("GITHUB_APP_KEY_DIR",
              "Optional file-based alternative to pasting the GitHub App private key in the "
              "console: host directory bind-mounted read-only into plugin-github at "
              "/run/secrets/github (put app.pem there; git-ignored under data/).",
              default="./data/github-app"),
        Entry("MCP_ALLOWED_HOSTS",
              "Base Host headers the /mcp endpoint accepts (DNS-rebinding guard). The console "
              "can add hosts (Settings) but never remove these.",
              default="localhost:*,127.0.0.1:*"),
        Entry("CF_ACCESS_ENABLED",
              "Require a Cloudflare Access identity on the admin plane. Must be true in public mode.",
              default="false"),
        Entry("CF_ACCESS_ALLOWED_EMAILS", "Comma-separated Access identities allowed; blank = any.",
              default=""),
        Entry("ALLOW_INSECURE_ADMIN",
              "Escape hatch: boot in public mode without Cloudflare Access. Leave false.",
              default="false"),
    ]),
]

ENTRIES = {e.name: e for _, entries in SECTIONS for e in entries}

# Third-party credentials that deliberately have no .env entry: the owner
# enters each in the console. (what, where in the console, where to get it)
CONSOLE_ENTERED = [
    ("Telegram bot token", "Channels > Telegram",
     "message @BotFather on Telegram, send /newbot, copy the token"),
    ("GitHub App id and private key", "Plugins > GitHub",
     "GitHub > Settings > Developer settings > GitHub Apps > New GitHub App; "
     "General page > Private keys > Generate a private key (or put app.pem in "
     "GITHUB_APP_KEY_DIR instead of pasting it)"),
    ("Google OAuth client id and secret", "Plugins > Google",
     "Google Cloud Console > APIs & Services > Credentials > Create credentials > "
     "OAuth client ID (Web application)"),
]
GENERATED = [e.name for e in ENTRIES.values() if e.generate]

HEADER_ENV = [
    "# Agent Authority Broker environment, generated by scripts/init_secrets.py.",
    "# NEVER commit this file. Regenerate one value: python scripts/init_secrets.py --rotate NAME",
]
HEADER_EXAMPLE = [
    "# Agent Authority Broker environment template. Do not fill this in by hand:",
    "# run  python scripts/init_secrets.py  to write a real .env with fresh secrets.",
    "# (This file is that script's --example output; a test keeps them identical.)",
]

# What to do after rotating each generated secret.
ROTATE_HINTS = {
    "SETUP_TOKEN": "Only matters before the owner account exists.",
    "ORIGIN_SECRET": "Update the Cloudflare Transform Rule header value, then restart: "
                     "docker compose -f docker-compose.yml -f docker-compose.public.yml up -d.",
    "BROKER_SECRETS_KEY": "Restart the broker: docker compose up -d broker. Secrets entered in "
                          "the console no longer decrypt: re-enter the Telegram bot token "
                          "(Channels > Telegram).",
    "DECISION_SIGNING_KEY": "Existing decision rows will no longer verify under the new key.",
    "PLUGIN_TOKEN_WHATSAPP": "Restart both ends: docker compose up -d broker plugin-whatsapp.",
    "PLUGIN_SECRETS_KEY_WHATSAPP": "Restart plugin-whatsapp; its stored config no longer "
                                   "decrypts, so re-save the WhatsApp plugin settings.",
    "SIDECAR_TOKEN": "Restart both ends: docker compose up -d plugin-whatsapp whatsapp-sidecar.",
    "PLUGIN_TOKEN_GITHUB": "Restart both ends: docker compose up -d broker plugin-github.",
    "PLUGIN_SECRETS_KEY_GITHUB": "Restart plugin-github, then reconnect GitHub from the console "
                                 "(its stored App key and installation no longer decrypt).",
    "PLUGIN_TOKEN_GOOGLE": "Restart both ends: docker compose up -d broker plugin-google.",
    "PLUGIN_SECRETS_KEY_GOOGLE": "Restart plugin-google, then reconnect Google from the console "
                                 "(its stored refresh token no longer decrypts).",
}


def render(values: dict[str, str], header: list[str]) -> str:
    """The whole file. Every line ends in LF (compose on Linux reads it)."""
    lines = list(header)
    for title, entries in SECTIONS:
        lines += ["", f"# --- {title} ---"]
        for e in entries:
            lines += [f"# {e.comment}", f"{e.name}={values.get(e.name, '')}"]
    return "\n".join(lines) + "\n"


def example_text() -> str:
    values = {e.name: e.default for e in ENTRIES.values()}
    return render(values, HEADER_EXAMPLE)


def _write_private(path: Path, data: bytes) -> None:
    """Atomically write `data` with mode 0600 (best effort on Windows).

    The temp file is created 0600 by mkstemp, so the secret is never briefly
    world-readable; os.replace then swaps it in over any existing file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.chmod(path, 0o600)
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode != 0o600:
            sys.exit(f"error: {path} has mode {oct(mode)}, expected 0o600")


def _checklist() -> list[str]:
    out = ["For a public (internet) deploy, fill in these values in the file:"]
    for e in ENTRIES.values():
        if e.obtain:
            out.append(f"  [ ] {e.name}: {e.obtain}")
    out.append("  and set CF_ACCESS_ENABLED=true (see deploy/DEPLOY.md).")
    out.append("")
    out.append("Entered in the console (/admin), never in this file, when you enable the feature:")
    for what, where, obtain in CONSOLE_ENTERED:
        out.append(f"  [ ] {what}: {where}. How to get it: {obtain}.")
    return out


def cmd_create(path: Path, force: bool) -> int:
    if path.exists() and not force:
        print(f"error: {path} already exists; pass --force to overwrite it "
              f"or --rotate NAME to regenerate one value", file=sys.stderr)
        return 1
    values = {e.name: e.default for e in ENTRIES.values()}
    for name in GENERATED:
        values[name] = ENTRIES[name].generate()
    _write_private(path, render(values, HEADER_ENV).encode("utf-8"))
    print(f"wrote {path} (mode 0600) with {len(GENERATED)} generated secrets:")
    print("  " + ", ".join(GENERATED))
    print()
    print("\n".join(_checklist()))
    return 0


def cmd_rotate(path: Path, name: str) -> int:
    if name not in GENERATED:
        print(f"error: {name} is not a generated secret; choose one of: "
              f"{', '.join(GENERATED)}", file=sys.stderr)
        return 2
    if not path.exists():
        print(f"error: {path} does not exist; run without --rotate first", file=sys.stderr)
        return 1
    data = path.read_bytes()
    # Split keeping line endings so every untouched line stays byte-identical.
    lines = data.splitlines(keepends=True)
    prefix = f"{name}=".encode("ascii")
    hits = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    if len(hits) > 1:
        print(f"error: {name} appears {len(hits)} times in {path}; fix it by hand first",
              file=sys.stderr)
        return 1
    new_value = ENTRIES[name].generate().encode("ascii")
    if hits:
        i = hits[0]
        old = lines[i]
        ending = old[len(old.rstrip(b"\r\n")):]
        lines[i] = prefix + new_value + ending
    else:
        # Older file without this key: append it (with its comment) at the end.
        if lines and not lines[-1].endswith(b"\n"):
            lines[-1] += b"\n"
        lines += [f"# {ENTRIES[name].comment}\n".encode("utf-8"), prefix + new_value + b"\n"]
    _write_private(path, b"".join(lines))
    print(f"rotated {name} in {path}")
    print(f"next: {ROTATE_HINTS[name]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate the broker's secrets into a .env file.")
    p.add_argument("--out", type=Path, default=None,
                   help="output path (default: <repo>/.env; with --example: stdout)")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.add_argument("--rotate", metavar="NAME", help="regenerate one secret in an existing file")
    p.add_argument("--example", action="store_true",
                   help="emit the .env.example template (no secrets)")
    args = p.parse_args(argv)

    if args.example:
        text = example_text()
        if args.out is None:
            sys.stdout.write(text)
        else:
            args.out.write_bytes(text.encode("utf-8"))
        return 0
    path = args.out or (REPO_ROOT / ".env")
    if args.rotate:
        return cmd_rotate(path, args.rotate)
    return cmd_create(path, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
