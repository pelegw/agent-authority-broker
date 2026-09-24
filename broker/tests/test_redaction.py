"""The redaction backstop's pattern table: every row catches its secret
shape, and nothing the logs legitimately carry (uuids, hashes, request ids,
resource ids, timestamps, access lines) is touched.

The table is the one list of secret shapes (logging_setup.SECRET_PATTERNS);
a row added there without a sample here fails the first test."""

import hashlib
import uuid

import pytest

from broker.logging_setup import REDACTED, SECRET_PATTERNS, kv, redact

FERNET_KEY = "3q2-7wEXAMPLEexampleEXAMPLEexampleEXAMPLE0a="     # 43 chars + "="
PEM = ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
       "BKcwggSjAgEAAoIBAQC7\n-----END PRIVATE KEY-----")

# name -> (text containing the secret, the secret itself)
SAMPLES = {
    "pem_block": (f"loaded key {PEM} from config", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"),
    "bearer": ("header Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig", "eyJhbGciOiJ"),
    "admin_token": ("token aab_admin_" + "0123456789abcdef" * 3, "0123456789abcdef" * 3),
    "agent_key": ("key=aab_" + "fedcba9876543210" * 3 + " ok", "fedcba9876543210" * 3),
    "telegram_bot_token": ("POST /bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getMe",
                           "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"),
    "google_access_token": ("token ya29.a0AfB_byC-1234567890abcdef", "a0AfB_byC-1234567890"),
    "google_refresh_token": ("refresh 1//0gAbCdEfGhIjKlMnOpQrStUvWx", "0gAbCdEfGhIjKlMnOpQr"),
    "google_client_secret": ("secret GOCSPX-AbCdEfGhIjKlMnOpQrSt", "AbCdEfGhIjKlMnOpQrSt"),
    "github_token": ("installation ghs_16C7e42F292c6912E7710c838347Ae178B4a", "16C7e42F292c69"),
    "github_fine_grained_pat": ("pat github_pat_11ABCDEFG0abcdefghijklmnop", "11ABCDEFG0abc"),
    "fernet_key": (f"PLUGIN_SECRETS_KEY {FERNET_KEY} set", FERNET_KEY),
    "session_cookie": ("Cookie: aab_session=Zm9vYmFyYmF6cXV4; theme=dark", "Zm9vYmFyYmF6cXV4"),
    "secret_field": ("{'X-Plugin-Token': 'c0ffee0123456789c0ffee0123456789', "
                     "'password': 'correct horse'}", "c0ffee0123456789c0ffee0123456789"),
}


def test_every_pattern_has_a_sample():
    assert set(SAMPLES) == {name for name, _, _ in SECRET_PATTERNS}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_each_secret_shape_is_redacted(name):
    text, secret = SAMPLES[name]
    out = redact(text)
    assert secret not in out and REDACTED in out, out


def test_the_whole_pem_block_goes_and_the_text_around_it_stays():
    out = redact(SAMPLES["pem_block"][0])
    assert out == f"loaded key {REDACTED} from config"


def test_a_truncated_pem_is_redacted_to_the_end():
    out = redact("key -----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3")
    assert out == f"key {REDACTED}"


def test_named_fields_keep_their_names():
    out = redact("form client_secret=GOC-abc&refresh_token=xyz123 password: hunter2x")
    assert out == (f"form client_secret={REDACTED}&refresh_token={REDACTED} "
                   f"password: {REDACTED}")


def test_bearer_keeps_the_scheme():
    assert redact("Bearer abcdefghijkl") == f"Bearer {REDACTED}"


def test_redaction_is_idempotent():
    for text, _ in SAMPLES.values():
        once = redact(text)
        assert redact(once) == once


NEGATIVES = [
    str(uuid.uuid4()), uuid.uuid4().hex, hashlib.sha256(b"x").hexdigest(),
    hashlib.sha1(b"x").hexdigest(), "sched-" + uuid.uuid4().hex, "tg-" + uuid.uuid4().hex,
    "972501234567@s.whatsapp.net", "120363025246125244@g.us", "2026-09-24T12:00:00.123Z",
    "octo/hello-world", "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",   # a Drive id (44)
    "0B1234567890abcdefghijklmnopqrstu",                                   # a Drive id (33)
    "gAAAAABmExampleFernetCiphertextNotAKeyAtAllButLongerThanOne44CharsX==",
    kv(method="GET", path="/v1/targets/echo/actions/get", status=200, duration_ms=12,
       actor="key:agent-1", ip="172.18.0.1"),
    kv(plugin="github", fields=["app_id", "app_slug"],
       secret_fields=["private_key_pem", "pat", "client_secret"]),
    "the Bearer scheme", "password_change refused", "decision=allow reason=covered chain=2",
    "request_id=5f0c1e2d3c4b5a69788796a5b4c3d2e1 row=42", "setup token is now inert",
]


@pytest.mark.parametrize("text", NEGATIVES)
def test_no_false_positives(text):
    assert redact(text) == text
