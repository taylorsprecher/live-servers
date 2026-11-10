"""Minimal AWS Signature Version 4 signing + credential providers.

Standard library only. Two consumers in this project:

* the host bootstrap (``aws-activation/bootstrap-certs.sh`` drives the
  copy in ``aws-activation/``) calling IoT ``CreateCertificateFromCsr``,
* the live S3 batch sink in ``aws_sinks.py`` (``PutObject``).

The implementation in ``backend/sigv4.py`` and in
``aws-activation/sigv4.py`` is byte-identical by design — if you change
one, change both (tests assert identical signatures from both copies).

Credential resolution order for ``default_credentials()``:

1. explicit ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` env vars
   (``AWS_SESSION_TOKEN`` optional) — mainly for off-host testing,
2. the EC2 instance metadata service (IMDSv2 token flow) using the
   instance profile attached to the host.

No credentials are ever logged or persisted by this module.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def derive_signing_key(secret: str, date_stamp: str, region: str,
                       service: str) -> bytes:
    k_date = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


class Credentials:
    def __init__(self, access_key: str, secret_key: str,
                 session_token: str = None):
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token

    def __repr__(self):  # never include the secret
        return f"Credentials(access_key={self.access_key!r}, token={'yes' if self.session_token else 'no'})"


def sign_request(method: str, host: str, region: str, service: str,
                 credentials: Credentials, path: str = "/",
                 query: str = "", headers: dict = None,
                 body: bytes = b"", when: datetime = None,
                 payload_hash: str = None) -> dict:
    """Sign one HTTP request; returns the full header dict to send.

    ``headers`` may carry extra headers to include in the signature
    (e.g. ``content-type``). ``host`` and ``x-amz-date`` (plus
    ``x-amz-security-token`` when a session token is present) are always
    signed. ``payload_hash`` overrides the computed body hash — pass
    UNSIGNED_PAYLOAD for services that accept it (S3 does).
    """
    when = when or datetime.now(timezone.utc)
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = when.strftime("%Y%m%d")

    hdrs = {}
    for k, v in (headers or {}).items():
        hdrs[k.strip().lower()] = " ".join(str(v).split())
    hdrs["host"] = host
    hdrs["x-amz-date"] = amz_date
    if credentials.session_token:
        hdrs["x-amz-security-token"] = credentials.session_token

    signed_headers = ";".join(sorted(hdrs))
    canonical_headers = "".join(f"{k}:{hdrs[k]}\n" for k in sorted(hdrs))
    body_hash = payload_hash if payload_hash is not None \
        else _sha256_hex(body)

    canonical_request = "\n".join([
        method.upper(), path, query, canonical_headers, signed_headers,
        body_hash,
    ])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        ALGORITHM, amz_date, scope, _sha256_hex(canonical_request.encode()),
    ])
    signing_key = derive_signing_key(credentials.secret_key, date_stamp,
                                     region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    out = dict(hdrs)
    out["authorization"] = (
        f"{ALGORITHM} Credential={credentials.access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}")
    return out


# ---------------------------------------------------------------------------
# Credential providers
# ---------------------------------------------------------------------------

def env_credentials() -> Credentials:
    ak = os.environ.get("AWS_ACCESS_KEY_ID")
    sk = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if ak and sk:
        return Credentials(ak, sk, os.environ.get("AWS_SESSION_TOKEN"))
    return None


_IMDS_BASE = "http://169.254.169.254"
_IMDS_TOKEN_TTL = "21600"


def _imds(path: str, token: str = None, timeout: float = 2.0) -> str:
    req = urllib.request.Request(_IMDS_BASE + path)
    if token:
        req.add_header("X-aws-ec2-metadata-token", token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def imds_credentials(timeout: float = 2.0) -> Credentials:
    """Fetch instance-profile credentials via the IMDSv2 token flow."""
    token_req = urllib.request.Request(
        _IMDS_BASE + "/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": _IMDS_TOKEN_TTL})
    with urllib.request.urlopen(token_req, timeout=timeout) as resp:
        token = resp.read().decode("utf-8")
    role = _imds("/latest/meta-data/iam/security-credentials/", token,
                 timeout).strip()
    if not role:
        raise RuntimeError("IMDS: no IAM role attached to this instance")
    doc = json.loads(_imds(
        f"/latest/meta-data/iam/security-credentials/{role}", token,
        timeout))
    return Credentials(doc["AccessKeyId"], doc["SecretAccessKey"],
                       doc.get("Token"))


def default_credentials() -> Credentials:
    """Env vars first (testing), then the instance metadata service."""
    creds = env_credentials()
    if creds is not None:
        return creds
    return imds_credentials()


class CredentialCache:
    """Caches instance credentials and refreshes them near expiry.

    IMDS documents carry an ``Expiration``; env credentials are treated
    as non-expiring. Refresh margin: 5 minutes.
    """

    def __init__(self, provider=default_credentials, margin_s: float = 300):
        self._provider = provider
        self._margin = margin_s
        self._creds = None
        self._expires_at = 0.0

    def get(self) -> Credentials:
        now = time.time()
        if self._creds is None or now >= self._expires_at - self._margin:
            self._creds = self._provider()
            # IMDS creds live ~6 h; env creds are static. A conservative
            # 1 h cache keeps refresh logic exercised without hammering
            # the metadata service.
            self._expires_at = now + 3600
        return self._creds
