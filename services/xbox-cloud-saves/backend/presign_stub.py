"""
Pre-signed URL service — LOCAL STUB.

!!! STUB — SIMULATES S3 PRE-SIGNED URLS !!!
Production (Phase 6 of the plan) uses boto3 generate_presigned_url()
against S3 with a 15-minute expiry. Locally there is no S3, so this
module mints an equivalent capability token:

    token = base64url(payload) "." base64url(HMAC-SHA256(payload, secret))

where payload = {"op": "get"|"put", "key": "users/{xuid}/savefiles/{name}",
                 "exp": <unix ts, now + 900>}

The token is embedded in a local URL:  {base}/transfer/{token}
The /transfer endpoint verifies signature, operation, and expiry before
touching storage — the same security properties the plan requires of
real pre-signed URLs (scoped to one object, one operation, short-lived,
cannot enumerate the bucket).
"""

import base64
import hashlib
import hmac
import json
import os
import time

EXPIRES_IN = 900  # 15 minutes, per Phase 6 of the plan

# Dev-only default. Production uses AWS SigV4 signing, not a shared secret.
_DEV_SECRET = os.environ.get("XSAVE_DEV_SECRET", "xsave-dev-only-secret")


class PresignError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str) -> str:
    mac = hmac.new(_DEV_SECRET.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256)
    return _b64encode(mac.digest())


def make_transfer_url(base_url: str, op: str, key: str) -> str:
    """Return a pre-signed-URL-style local URL for one operation on one key."""
    payload = {"op": op, "key": key, "exp": int(time.time()) + EXPIRES_IN}
    payload_b64 = _b64encode(json.dumps(payload).encode("utf-8"))
    token = payload_b64 + "." + _sign(payload_b64)
    return f"{base_url}/transfer/{token}"


def verify_transfer_token(token: str, expected_op: str) -> str:
    """Verify a transfer token and return the storage key it authorizes."""
    try:
        payload_b64, signature = token.split(".", 1)
    except ValueError:
        raise PresignError("malformed transfer token")
    if not hmac.compare_digest(signature, _sign(payload_b64)):
        raise PresignError("invalid transfer token signature")
    try:
        payload = json.loads(_b64decode(payload_b64))
    except Exception:
        raise PresignError("malformed transfer token")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise PresignError("transfer URL expired")
    if payload.get("op") != expected_op:
        raise PresignError("transfer token does not allow this operation")
    return payload["key"]
