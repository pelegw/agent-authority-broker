"""scripts/init_secrets.py: generates every broker-owned secret, never prints
one, refuses to clobber, rotates exactly one line, and matches .env.example.

The script is run as a subprocess (as an operator would run it), with the
interpreter running the tests.
"""

import base64
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "init_secrets.py"

GENERATED = ["SETUP_TOKEN", "SIDECAR_TOKEN", "ORIGIN_SECRET",
             "BROKER_SECRETS_KEY", "DECISION_SIGNING_KEY"]
PLACEHOLDERS = ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH", "GOOGLE_OAUTH_CLIENT_ID",
                "GOOGLE_OAUTH_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "CF_ACCESS_TEAM_DOMAIN",
                "CF_ACCESS_AUD", "SITE_DOMAIN"]


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def parse(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


@pytest.fixture()
def out(tmp_path) -> Path:
    return tmp_path / "x.env"


def test_fresh_run_creates_every_key(out):
    r = run("--out", out)
    assert r.returncode == 0, r.stderr
    values = parse(out)
    for name in GENERATED:
        assert len(values[name]) >= 40, name
    for name in PLACEHOLDERS:
        assert values[name] == "", name
    # Every value is distinct: no accidental reuse between secrets.
    assert len({values[n] for n in GENERATED}) == len(GENERATED)


def test_secret_shapes(out):
    run("--out", out)
    v = parse(out)
    assert len(base64.urlsafe_b64decode(v["BROKER_SECRETS_KEY"])) == 32
    from cryptography.fernet import Fernet
    Fernet(v["BROKER_SECRETS_KEY"])   # accepted as a Fernet key
    assert len(bytes.fromhex(v["DECISION_SIGNING_KEY"])) == 32
    assert len(bytes.fromhex(v["SIDECAR_TOKEN"])) == 32
    assert len(bytes.fromhex(v["ORIGIN_SECRET"])) == 32


def test_placeholders_are_labelled(out):
    run("--out", out)
    lines = out.read_text(encoding="utf-8").splitlines()
    for name in PLACEHOLDERS:
        i = lines.index(f"{name}=")
        assert lines[i - 1].startswith("# "), name


def test_stdout_never_contains_a_secret(out):
    r = run("--out", out)
    values = parse(out)
    for name in GENERATED:
        assert values[name] not in r.stdout and values[name] not in r.stderr
    assert str(out) in r.stdout
    for name in PLACEHOLDERS:   # the checklist names each third-party value
        assert name in r.stdout


def test_second_run_refuses_and_leaves_file_untouched(out):
    run("--out", out)
    before = out.read_bytes()
    r = run("--out", out)
    assert r.returncode != 0
    assert "--force" in r.stderr
    assert out.read_bytes() == before


def test_force_overwrites(out):
    run("--out", out)
    before = parse(out)
    r = run("--out", out, "--force")
    assert r.returncode == 0
    after = parse(out)
    assert all(before[n] != after[n] for n in GENERATED)


def test_rotate_changes_exactly_one_line(out):
    run("--out", out)
    before = out.read_bytes().splitlines(keepends=True)
    r = run("--out", out, "--rotate", "SETUP_TOKEN")
    assert r.returncode == 0, r.stderr
    after = out.read_bytes().splitlines(keepends=True)
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(changed) == 1
    assert after[changed[0]].startswith(b"SETUP_TOKEN=")
    new_value = after[changed[0]].strip().split(b"=", 1)[1].decode()
    assert new_value not in r.stdout


def test_rotate_preserves_hand_edits_byte_for_byte(out):
    run("--out", out)
    # An operator filled a placeholder and uses CRLF on one line: all kept.
    text = out.read_bytes().replace(b"SITE_DOMAIN=\n", b"SITE_DOMAIN=aab.example.com\r\n")
    out.write_bytes(text)
    run("--out", out, "--rotate", "ORIGIN_SECRET")
    after = out.read_bytes()
    assert b"SITE_DOMAIN=aab.example.com\r\n" in after
    old_lines = [l for l in text.splitlines(keepends=True) if not l.startswith(b"ORIGIN_SECRET=")]
    new_lines = [l for l in after.splitlines(keepends=True) if not l.startswith(b"ORIGIN_SECRET=")]
    assert old_lines == new_lines


def test_rotate_rejects_non_generated_names(out):
    run("--out", out)
    before = out.read_bytes()
    assert run("--out", out, "--rotate", "SITE_DOMAIN").returncode != 0
    assert run("--out", out, "--rotate", "NOPE").returncode != 0
    assert out.read_bytes() == before


def test_rotate_requires_an_existing_file(out):
    assert run("--out", out, "--rotate", "SETUP_TOKEN").returncode != 0
    assert not out.exists()


def test_rotate_appends_a_key_missing_from_an_older_file(out):
    out.write_bytes(b"SIDECAR_TOKEN=abc\n")
    assert run("--out", out, "--rotate", "DECISION_SIGNING_KEY").returncode == 0
    lines = out.read_bytes().splitlines(keepends=True)
    assert lines[0] == b"SIDECAR_TOKEN=abc\n"
    assert parse(out)["DECISION_SIGNING_KEY"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_file_mode_is_0600(out):
    run("--out", out)
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    run("--out", out, "--rotate", "SETUP_TOKEN")
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_env_example_matches_the_script(tmp_path):
    # .env.example is generated from the same key table; this keeps them in
    # lockstep. Regenerate with: python scripts/init_secrets.py --example --out .env.example
    generated = tmp_path / "example.env"
    assert run("--example", "--out", generated).returncode == 0
    committed = (REPO / ".env.example").read_bytes().replace(b"\r\n", b"\n")
    assert committed == generated.read_bytes()


def test_env_example_has_the_same_keys_as_a_real_env(out, tmp_path):
    run("--out", out)
    example = tmp_path / "example.env"
    run("--example", "--out", example)
    assert list(parse(out)) == list(parse(example))
    for name in GENERATED:
        assert parse(example)[name] == ""   # the template carries no secret
