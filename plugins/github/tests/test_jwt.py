"""The App JWT: RS256, iat 60 s back, exp 10 minutes ahead, iss = App ID."""

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from aab_plugin_github.app_jwt import InvalidKey, app_jwt, load_private_key

from .conftest import APP_CONFIG, configure, install
from .fakes import APP_ID, PRIVATE_KEY_PEM, PUBLIC_KEY, START


def test_claims_and_signature():
    token = app_jwt(APP_ID, load_private_key(PRIVATE_KEY_PEM), START)
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iat": int(START) - 60, "exp": int(START) + 600, "iss": APP_ID}


def test_the_connection_sends_exactly_those_claims(client, gh, clock):
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM})
    install(client)
    assert gh.jwt_claims, "no App JWT reached GitHub"
    for claims in gh.jwt_claims:
        assert claims == {"iat": int(clock()) - 60, "exp": int(clock()) + 600, "iss": APP_ID}


def test_every_mint_signs_a_fresh_jwt_on_the_current_clock(app_mode, gh, clock, perform):
    clock.advance(3600)          # an hour later: an old JWT would be expired
    assert perform("get_file", {"repo": "octo/a", "path": "README.md"}).status_code == 200
    assert gh.jwt_claims[-1]["exp"] == int(clock()) + 600


@pytest.mark.parametrize("pem", ["", "not a key",
                                 "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----"])
def test_bad_pem_is_refused_without_echoing_it(pem):
    with pytest.raises(InvalidKey) as e:
        load_private_key(pem)
    assert "AAAA" not in str(e.value) and e.value.__cause__ is None


def test_non_rsa_keys_are_refused():
    ec_pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    with pytest.raises(InvalidKey, match="RSA"):
        load_private_key(ec_pem)
