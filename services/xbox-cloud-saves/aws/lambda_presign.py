"""Xbox Cloud Save — presigned-URL API Lambda (REAL AWS build).

Implements the two save routes from Taylor's "Xbox AWS S3 1" design
behind API Gateway (HTTP API, payload format 2.0):

  POST /saves/{saveId}/upload-url    -> {"uploadUrl", "key", "expiresIn"}
  GET  /saves/{saveId}/download-url  -> {"downloadUrl", "key", "expiresIn"}

Flow (the document's Phases 2/3/5/6): validate the caller's Xbox Live
XSTS token, derive the XUID from the token's validated claims, build
the object key strictly as users/{xuid}/savefiles/{saveId}, and hand
back a SigV4 pre-signed S3 URL that expires in 900 seconds
(15 minutes). The client then talks to S3 directly; this function's
IAM role is the only thing that can mint URLs, and its policy allows
only s3:GetObject / s3:PutObject under this bucket's users/* prefix.

XSTS validation — honest status
-------------------------------
Validation is implemented against the REAL Xbox Live services:
the XBL3.0 header is parsed, the token's DisplayClaims are checked
(user hash binding: claims uhs must equal the header's user hash),
and the token is then presented to the Xbox Live profile service
(profile.xboxlive.com), which is Microsoft itself validating the
token — HTTP 200 means Microsoft accepted it for that XUID.

What this CANNOT do yet: Microsoft only issues XSTS tokens for a
relying party / audience that is registered with Microsoft (an Xbox
Live partner/title registration — ID@Xbox, the Xbox Live Creators
Program, or a full partner title), and only accepts tokens presented
within that registration's rules. Taylor has no such registration on
file, so until one exists, real Xbox clients cannot obtain tokens
this API will accept, and validation will correctly return 401 for
everything else. No Microsoft credential is faked or bypassed here.
See aws/README.md.

Test identity (verification only)
---------------------------------
If — and only if — the environment variable XSAVE_TEST_XUID is set,
a request carrying the header  X-Xsave-Test-Xuid: <same value>  is
treated as that XUID without Xbox validation. This exists solely so
the provisioning chain can be verified end to end before the
Microsoft registration exists. It is OFF by default (the provisioner
only sets it with an explicit --enable-test-identity flag), it is
loudly named, and it must be removed for any real use:
  aws lambda update-function-configuration --function-name
  xsave-presign-api --environment Variables="{XSAVE_BUCKET=...,XSAVE_TEST_XUID=}"
"""

import base64
import json
import os
import re
import urllib.error
import urllib.request

import boto3

BUCKET = os.environ.get("XSAVE_BUCKET", "")
TEST_XUID = os.environ.get("XSAVE_TEST_XUID", "")
EXPIRES_IN = 900  # 15 minutes, per the plan (Phase 6)
REGION = os.environ.get("AWS_REGION", "us-west-2")

SAVE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROFILE_URL = "https://profile.xboxlive.com/users/xuid({xuid})/profile/settings"

_s3 = boto3.client("s3", region_name=REGION)


# ------------------------------------------------------------------ http

def _respond(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _error(status, message):
    return _respond(status, {"error": message})


# ------------------------------------------------------------------ auth

class AuthError(Exception):
    pass


def _b64url_json(segment):
    pad = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + pad))


def parse_xbl_header(header):
    """Split 'XBL3.0 x=<user_hash>;<xsts_token>' (the plan's format)."""
    if not header or not header.startswith("XBL3.0 x="):
        raise AuthError("missing or malformed XBL3.0 authorization header")
    rest = header[len("XBL3.0 x="):]
    if ";" not in rest:
        raise AuthError("malformed XBL3.0 authorization header")
    user_hash, token = rest.split(";", 1)
    return user_hash.strip(), token.strip()


def validate_xsts(token, user_hash):
    """Validate an XSTS token against the real Xbox Live services.

    Returns the XUID from the token's DisplayClaims. Raises AuthError
    on any failure. The token's claims bind the user hash (uhs) to the
    header value; the live call to profile.xboxlive.com is Microsoft
    itself accepting or rejecting the token for that XUID.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("xsts token is not a JWT")
    try:
        claims = _b64url_json(parts[1])
    except Exception:
        raise AuthError("xsts token payload is not decodable")
    xui = (claims.get("xui") or [{}])[0]
    xuid, uhs = xui.get("xid"), xui.get("uhs")
    if not xuid:
        raise AuthError("xsts token carries no XUID (DisplayClaims.xui[0].xid)")
    if uhs != user_hash:
        raise AuthError("user hash does not match token claims (uhs binding)")

    url = PROFILE_URL.format(xuid=xuid) + "?settings=Gamertag"
    req = urllib.request.Request(url, headers={
        "Authorization": f"XBL3.0 x={user_hash};{token}",
        "x-xbl-contract-version": "2",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                raise AuthError(f"xbox live profile check returned {resp.status}")
    except urllib.error.HTTPError as exc:
        raise AuthError(f"xbox live rejected the token (HTTP {exc.code})")
    except urllib.error.URLError as exc:
        raise AuthError(f"xbox live validation unreachable: {exc.reason}")
    return str(xuid)


def authenticate(headers):
    """Return the caller's XUID, or raise AuthError.

    The test-identity path is taken only when XSAVE_TEST_XUID is set
    AND the caller presents it back in the X-Xsave-Test-Xuid header.
    """
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    if TEST_XUID and lowered.get("x-xsave-test-xuid") == TEST_XUID:
        return TEST_XUID
    user_hash, token = parse_xbl_header(lowered.get("authorization"))
    return validate_xsts(token, user_hash)


# ------------------------------------------------------------------ saves

def key_for(xuid, save_id):
    """Object keys are built ONLY from the validated XUID (zero-trust
    identity mapping, Phase 3) — never from client-supplied ids."""
    return f"users/{xuid}/savefiles/{save_id}"


def validate_save_id(save_id):
    if not SAVE_ID_RE.match(save_id or "") or ".." in save_id:
        raise ValueError(f"invalid saveId: {save_id!r}")


def upload_url(xuid, save_id):
    key = key_for(xuid, save_id)
    url = _s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=EXPIRES_IN,
    )
    return _respond(200, {"uploadUrl": url, "key": key, "expiresIn": EXPIRES_IN})


def download_url(xuid, save_id):
    key = key_for(xuid, save_id)
    try:
        _s3.head_object(Bucket=BUCKET, Key=key)
    except _s3.exceptions.ClientError:
        return _error(404, "save not found")
    url = _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=EXPIRES_IN,
    )
    return _respond(200, {"downloadUrl": url, "key": key, "expiresIn": EXPIRES_IN})


# ------------------------------------------------------------------ entry

def handler(event, _context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", "")
    save_id = (event.get("pathParameters") or {}).get("saveId", "")
    try:
        validate_save_id(save_id)
    except ValueError as exc:
        return _error(400, f"invalid request: {exc}")
    try:
        xuid = authenticate(event.get("headers"))
    except AuthError as exc:
        return _error(401, f"unauthorized: {exc}")
    if method == "POST":
        return upload_url(xuid, save_id)
    if method == "GET":
        return download_url(xuid, save_id)
    return _error(405, "method not allowed")
