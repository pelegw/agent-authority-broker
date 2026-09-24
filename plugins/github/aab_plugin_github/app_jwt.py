"""The GitHub App JWT: how the plugin proves it is the App.

An App authenticates as itself with a short RS256 JWT signed by its private
key, and uses that only to read its installation and to mint installation
tokens (`POST /app/installations/{id}/access_tokens`). Claims follow
GitHub's guidance: `iat` 60 s in the past (clock drift), `exp` 10 minutes
ahead (GitHub's maximum), `iss` the App ID.

The PEM is parsed here and nowhere else. Parse errors are reported without
the underlying exception text, which could quote key material.
"""

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 600
MAX_PEM_BYTES = 16 * 1024


class InvalidKey(ValueError):
    """The PEM is not an unencrypted RSA private key."""


def load_private_key(pem: str) -> RSAPrivateKey:
    if not isinstance(pem, str) or not pem.strip():
        raise InvalidKey("private key is empty")
    data = pem.strip().encode("utf-8", "replace")
    if len(data) > MAX_PEM_BYTES:
        raise InvalidKey("private key is too large to be a PEM")
    try:
        key = load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        # Deliberately not chained into the message: keep key bytes out.
        raise InvalidKey("private key is not a valid unencrypted PEM") from None
    if not isinstance(key, RSAPrivateKey):
        raise InvalidKey("private key must be an RSA key")
    return key


def app_jwt(app_id: str, key: RSAPrivateKey, now: float) -> str:
    """A fresh App JWT (valid for GitHub for at most 10 minutes)."""
    issued = int(now)
    claims = {"iat": issued - JWT_BACKDATE_SECONDS,
              "exp": issued + JWT_LIFETIME_SECONDS,
              "iss": str(app_id)}
    return jwt.encode(claims, key, algorithm="RS256")
