"""
Xbox XSTS authentication — REAL TOKEN STRUCTURE, LOCAL TEST KEY.

This module replaces the Increment 1 unsigned stub (auth_stub.py) with
the production XSTS validation *structure* from Phase 2 of the plan:

  * Tokens are JWTs (JWS compact serialization):
        base64url(header) "." base64url(payload) "." base64url(signature)
  * The payload carries the real XSTS claim layout:
        iss  = "https://xsts.auth.xboxlive.com"
        aud  = the relying party ("http://xboxlive.com" by default)
        exp / nbf / iat
        DisplayClaims = {"xui": [{"xid": <XUID>, "uhs": <user hash>}]}
  * get_xuid() reads the XUID from DisplayClaims.xui[0].xid — never from
    a client-supplied id.
  * validate_xsts_token() verifies, in order: structure, alg, signature,
    expiry / not-before, issuer, audience, DisplayClaims shape, and that
    the token's uhs matches the user hash in the Authorization header.

What is NOT production here: the signature. Microsoft signs XSTS tokens
asymmetrically and production validation resolves Microsoft's public
keys (JWKS) for the token's kid. Locally we sign and verify with HS256
against a single symmetric test key (env XSAVE_XSTS_SIGNING_KEY) so the
full parse/validate path runs offline. Swapping in real validation
means replacing _verify_signature() with JWKS resolution — the claim
checks around it do not change.

Function surface matches auth_stub.py (parse_xbl_token /
validate_xsts_token / get_xuid / issue_dev_token / AuthError) so the
API and Lambda handlers can use either implementation.

The client header format is the real one:
    Authorization: XBL3.0 x=<user_hash>;<xsts_token>
"""

import base64
import hashlib
import hmac
import json
import os
import time

TOKEN_TTL_SECONDS = 3600  # XSTS tokens are short-lived; 1 h locally

ISSUER = os.environ.get("XSAVE_XSTS_ISSUER", "https://xsts.auth.xboxlive.com")
AUDIENCE = os.environ.get("XSAVE_XSTS_AUDIENCE", "http://xboxlive.com")

# LOCAL TEST KEY ONLY. Production never has a shared secret — it
# validates Microsoft's signatures with Microsoft's public keys.
_SIGNING_KEY = os.environ.get(
    "XSAVE_XSTS_SIGNING_KEY", "xsave-local-test-key-NOT-FOR-PRODUCTION"
).encode("utf-8")

_ALG = "HS256"  # local stand-in for Microsoft's asymmetric signing


class AuthError(Exception):
    """Raised for every authentication failure; callers map it to 401."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- helpers

def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(signing_input: bytes) -> bytes:
    return hmac.new(_SIGNING_KEY, signing_input, hashlib.sha256).digest()


def sign_token(payload: dict) -> str:
    """Serialize + sign a payload as a JWT with the local test key.

    Used by the dev-login issuer and by tests. Production tokens are
    issued by Microsoft, never by this service.
    """
    header = {"typ": "JWT", "alg": _ALG}
    head_b64 = _b64encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    body_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{head_b64}.{body_b64}".encode("ascii")
    return f"{head_b64}.{body_b64}.{_b64encode(_sign(signing_input))}"


def _verify_signature(signing_input: bytes, signature: bytes) -> bool:
    """LOCAL: HMAC comparison against the test key.

    PRODUCTION REPLACEMENT POINT: resolve the token header's kid
    against Microsoft's XSTS JWKS and verify the asymmetric signature.
    Nothing else in this module changes.
    """
    return hmac.compare_digest(signature, _sign(signing_input))


def _derive_xuid(gamertag: str) -> str:
    """Deterministic fake XUID with the real shape (2535... prefix).

    Same derivation as the Increment 1 stub so a gamertag keeps the
    same XUID (and therefore the same storage prefix) across increments.
    Real XUIDs are assigned by Xbox Live and arrive inside the token.
    """
    digest = hashlib.sha256(("xuid:" + gamertag).encode("utf-8")).hexdigest()
    return "2535" + str(int(digest[:12], 16) % 10**12).zfill(12)


def _derive_user_hash(gamertag: str) -> str:
    digest = hashlib.sha256(("hash:" + gamertag).encode("utf-8")).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------- issuing

def issue_dev_token(gamertag: str) -> tuple[str, str, int]:
    """Dev-login issuer: mint a locally-signed, XSTS-shaped JWT.

    Returns (user_hash, token, expires_at) — the same tuple shape as
    auth_stub.issue_dev_token. The token is structurally a real XSTS
    JWT (DisplayClaims, iss, aud, exp) but signed with the local test
    key; Microsoft would never accept it, and that is the point of the
    "local" label.
    """
    now = int(time.time())
    exp = now + TOKEN_TTL_SECONDS
    user_hash = _derive_user_hash(gamertag)
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": exp,
        "DisplayClaims": {
            "xui": [
                {
                    "xid": _derive_xuid(gamertag),
                    "uhs": user_hash,
                    "gtg": gamertag,
                }
            ]
        },
    }
    return user_hash, sign_token(payload), exp


# ---------------------------------------------------------------- parsing

def parse_xbl_token(authorization: str | None) -> tuple[str, str]:
    """Parse 'Authorization: XBL3.0 x=<user_hash>;<xsts_token>'.

    Mirrors the spec's parse_xbl_token(). Raises AuthError on a missing
    or malformed header.
    """
    if not authorization:
        raise AuthError("missing token")
    if not authorization.startswith("XBL3.0 x="):
        raise AuthError("malformed token")
    parts = authorization[len("XBL3.0 x="):].split(";", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise AuthError("malformed token")
    return parts[0], parts[1]


def validate_xsts_token(token: str, expected_user_hash: str | None = None) -> dict:
    """Validate an XSTS JWT and return its payload claims.

    Rejection rules (spec Phase 2) -> AuthError reasons:
      malformed token  — not a 3-part JWT, undecodable, wrong alg,
                         missing/ill-shaped DisplayClaims
      invalid token    — signature failure, wrong issuer/audience,
                         user-hash mismatch with the header
      expired token    — exp in the past (or nbf in the future)
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("malformed token")
    head_b64, body_b64, sig_b64 = parts
    try:
        header = json.loads(_b64decode(head_b64))
        payload = json.loads(_b64decode(body_b64))
        signature = _b64decode(sig_b64)
    except Exception:
        raise AuthError("malformed token")
    if not isinstance(header, dict) or header.get("alg") != _ALG:
        raise AuthError("malformed token")
    if not isinstance(payload, dict):
        raise AuthError("malformed token")

    signing_input = f"{head_b64}.{body_b64}".encode("ascii")
    if not _verify_signature(signing_input, signature):
        raise AuthError("invalid token")

    now = int(time.time())
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise AuthError("malformed token")
    if int(exp) < now:
        raise AuthError("expired token")
    nbf = payload.get("nbf")
    if isinstance(nbf, (int, float)) and int(nbf) > now:
        raise AuthError("expired token")

    if payload.get("iss") != ISSUER:
        raise AuthError("invalid token")
    if payload.get("aud") != AUDIENCE:
        raise AuthError("invalid token")

    claims = _display_claims(payload)
    if expected_user_hash is not None and claims["uhs"] != expected_user_hash:
        raise AuthError("invalid token")
    return payload


def _display_claims(payload: dict) -> dict:
    """Extract DisplayClaims.xui[0], enforcing the XSTS claim shape."""
    try:
        xui = payload["DisplayClaims"]["xui"]
        first = xui[0]
        xid, uhs = first["xid"], first["uhs"]
    except (KeyError, IndexError, TypeError):
        raise AuthError("malformed token")
    if not isinstance(xid, str) or not xid or not isinstance(uhs, str) or not uhs:
        raise AuthError("malformed token")
    return first


def get_xuid(payload: dict) -> str:
    """Return the caller's XUID from validated token claims."""
    return _display_claims(payload)["xid"]


def get_user_hash(payload: dict) -> str:
    """Return the token's user hash (uhs) from validated claims."""
    return _display_claims(payload)["uhs"]
