"""
Xbox XSTS authentication — LOCAL STUB.

!!! STUB — NOT REAL XBOX AUTH !!!
This module simulates the Phase 2 "Xbox Authentication Module" from the
Xbox Cloud Save Platform plan. It implements the same function surface
(parse_xbl_token / validate_xsts_token / get_xuid) and the same rejection
rules (missing / expired / invalid / malformed token -> 401), but it does
NOT talk to Microsoft and does NOT validate real XSTS tokens.

How the stub works:
  * POST /auth/dev-login issues a fake token. The token is just a
    base64url-encoded JSON payload {"sub": <gamertag>, "exp": <unix ts>}
    with NO signature. Real XSTS tokens are signed JWTs validated against
    Microsoft's keys — that validation is a later increment and requires a
    registered Xbox relying-party configuration.
  * get_xuid() derives a stable, fake 16-digit XUID from the gamertag so
    the storage layout (users/{xuid}/...) matches production exactly.

The client header format is the real one:
    Authorization: XBL3.0 x=<user_hash>;<xsts_token>
"""

import base64
import hashlib
import json
import time

TOKEN_TTL_SECONDS = 3600  # stub tokens live 1 hour


class AuthError(Exception):
    """Raised for every authentication failure; the API maps it to 401."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_dev_token(gamertag: str) -> tuple[str, str, int]:
    """STUB login: mint a fake (user_hash, token, expires_at) for a gamertag."""
    exp = int(time.time()) + TOKEN_TTL_SECONDS
    payload = {"sub": gamertag, "exp": exp, "iss": "xsave-dev-stub"}
    token = _b64encode(json.dumps(payload).encode("utf-8"))
    user_hash = hashlib.sha256(("hash:" + gamertag).encode("utf-8")).hexdigest()[:16]
    return user_hash, token, exp


def parse_xbl_token(authorization: str | None) -> tuple[str, str]:
    """Parse 'Authorization: XBL3.0 x=<user_hash>;<xsts_token>'.

    Mirrors the spec's parse_xbl_token(). Raises AuthError on a missing or
    malformed header.
    """
    if not authorization:
        raise AuthError("missing token")
    if not authorization.startswith("XBL3.0 x="):
        raise AuthError("malformed token")
    parts = authorization[len("XBL3.0 x="):].split(";", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise AuthError("malformed token")
    return parts[0], parts[1]


def validate_xsts_token(token: str, _expected_user_hash: str | None = None) -> dict:
    """STUB validation of an XSTS token. (The optional user-hash
    parameter exists only so this stub shares auth_xsts.validate's
    signature; the stub has no user hash in its payload to check.)

    Real implementation: verify the JWT signature against Microsoft's
    keys and check audience/expiry. Stub: decode the unsigned payload and
    check shape + expiry, applying the spec's rejection rules.
    """
    try:
        payload = json.loads(_b64decode(token))
    except Exception:
        raise AuthError("invalid token")
    if not isinstance(payload, dict) or "sub" not in payload or "exp" not in payload:
        raise AuthError("invalid token")
    if int(payload["exp"]) < int(time.time()):
        raise AuthError("expired token")
    return payload


def get_xuid(payload: dict) -> str:
    """Derive the (fake, stable) XUID for an authenticated player.

    Real XUIDs come from the XSTS token claims. The stub derives a
    deterministic 16-digit value with the same shape (2535... prefix,
    as in the plan's example) from the gamertag, so storage paths are
    realistic and stable across logins.
    """
    digest = hashlib.sha256(("xuid:" + str(payload["sub"])).encode("utf-8")).hexdigest()
    return "2535" + str(int(digest[:12], 16) % 10**12).zfill(12)
