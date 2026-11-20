#!/usr/bin/env python3
"""Zillion Storage — AWS backend: SigV4 signing, STS AssumeRole and
the S3 operations + health probe. Moved verbatim from
backend/server.py."""
import datetime
import hashlib
import hmac
import http.client
import os
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from common import sha256_of_file, with_retries
from config import (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, CHUNK,
                    EMPTY_SHA256, S3_BUCKET, S3_HOST, S3_KMS_KEY_ID,
                    S3_REGION, S3_ROLE_ARN)

# ======================================================================
# AWS: SigV4 signing, STS AssumeRole, and the S3 backend (stdlib only)
# ======================================================================

class AwsError(RuntimeError):
    """An AWS call failed; the message carries the HTTP status and the
    AWS error detail so callers can surface it, never swallow it."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _hmac_sha256(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sigv4_derived_key(secret, date_stamp, region, service):
    k_date = _hmac_sha256(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def sigv4_headers(*, method, url, payload_hash, service, region,
                  access_key, secret_key, session_token=None,
                  extra_headers=None, when=None):
    """Sign one AWS request (Signature Version 4) and return the full
    header dict to send (Authorization, X-Amz-Date, the signed extras,
    and X-Amz-Security-Token when a session token is in play). The
    Host header is NOT returned — the HTTP transport adds it — but it
    IS part of the canonical request, taken from the URL's netloc."""
    when = when or datetime.datetime.now(datetime.timezone.utc)
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = when.strftime("%Y%m%d")
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    query_pairs = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    canonical_query = "&".join(
        f"{quote(str(k), safe='-_.~')}={quote(str(v), safe='-_.~')}"
        for k, v in query_pairs)
    canon = {}
    for k, v in (extra_headers or {}).items():
        canon[k.lower()] = " ".join(str(v).split())
    canon["host"] = host
    canon["x-amz-date"] = amz_date
    if session_token:
        canon["x-amz-security-token"] = session_token
    signed_names = sorted(canon)
    canonical_headers = "".join(f"{n}:{canon[n]}\n" for n in signed_names)
    signed_headers = ";".join(signed_names)
    canonical_request = "\n".join([
        method.upper(), path, canonical_query, canonical_headers,
        signed_headers, payload_hash,
    ])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signing_key = _sigv4_derived_key(secret_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    out = {k: v for k, v in (extra_headers or {}).items()}
    out["X-Amz-Date"] = amz_date
    if session_token:
        out["X-Amz-Security-Token"] = session_token
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}")
    return out


# --- STS AssumeRole, with the temporary credentials cached -----------

_aws_state = {"creds": None, "expires": 0.0, "error": None, "error_at": 0.0}
_aws_lock = threading.Lock()


def parse_assume_role_xml(xml_text):
    """Parse an STS AssumeRole response document into
    {access_key, secret_key, session_token, expires} (epoch seconds).
    Namespace-agnostic: element tags are matched by local name."""
    fields = {}
    for el in ET.fromstring(xml_text).iter():
        local = el.tag.split("}")[-1]
        if local in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration"):
            fields[local] = (el.text or "").strip()
    missing = [k for k in ("AccessKeyId", "SecretAccessKey", "SessionToken")
               if not fields.get(k)]
    if missing:
        raise AwsError(f"AssumeRole response missing fields: {', '.join(missing)}")
    expires = 0.0
    raw_exp = fields.get("Expiration", "")
    if raw_exp:
        try:
            expires = datetime.datetime.fromisoformat(
                raw_exp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            expires = 0.0
    return {"access_key": fields["AccessKeyId"],
            "secret_key": fields["SecretAccessKey"],
            "session_token": fields["SessionToken"],
            "expires": expires}


def _assume_role():
    body = urlencode({
        "Action": "AssumeRole",
        "Version": "2011-06-15",
        "RoleArn": S3_ROLE_ARN,
        "RoleSessionName": "zillion-storage",
        "DurationSeconds": "3600",
    }).encode("utf-8")
    url = f"https://sts.{S3_REGION}.amazonaws.com/"
    headers = sigv4_headers(
        method="POST", url=url,
        payload_hash=hashlib.sha256(body).hexdigest(),
        service="sts", region=S3_REGION,
        access_key=AWS_ACCESS_KEY_ID, secret_key=AWS_SECRET_ACCESS_KEY,
        extra_headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        detail = ex.read().decode("utf-8", "replace")[:300]
        raise AwsError(f"STS AssumeRole failed HTTP {ex.code}: {detail}",
                       status=ex.code) from ex
    except Exception as ex:  # noqa: BLE001 - surfaced to the caller as-is
        raise AwsError(f"STS AssumeRole failed: {ex}") from ex
    return parse_assume_role_xml(text)


def aws_temp_credentials():
    """Temporary credentials from AssumeRole, cached until 60s before
    expiration. A failure is cached for 20s so a broken AWS path does
    not stall every request; the error is re-raised, never hidden."""
    now = time.time()
    with _aws_lock:
        creds = _aws_state["creds"]
        if creds and now < creds["expires"] - 60:
            return creds
        if _aws_state["error"] and now - _aws_state["error_at"] < 20:
            raise _aws_state["error"]
        try:
            creds = _assume_role()
        except Exception as ex:  # noqa: BLE001
            _aws_state["error"] = ex
            _aws_state["error_at"] = now
            raise
        # A response without a parseable expiration is trusted for 5 min.
        if not creds["expires"]:
            creds["expires"] = now + 300
        _aws_state["creds"] = creds
        _aws_state["error"] = None
        return creds


def s3_configured():
    return bool(AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY)


# --- S3 operations -----------------------------------------------------

def _s3_open(method, url, payload_hash, extra_headers=None, body_len=0):
    """Open one SigV4-signed S3 request with the temporary credentials
    and return (connection, response) after headers are sent; the
    caller streams any body, reads the response, and closes."""
    creds = aws_temp_credentials()
    extras = {"X-Amz-Content-Sha256": payload_hash}
    extras.update(extra_headers or {})
    headers = sigv4_headers(
        method=method, url=url, payload_hash=payload_hash,
        service="s3", region=S3_REGION,
        access_key=creds["access_key"], secret_key=creds["secret_key"],
        session_token=creds["session_token"], extra_headers=extras)
    parsed = urlparse(url)
    conn = http.client.HTTPSConnection(parsed.netloc, timeout=30)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    conn.putrequest(method, target, skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.putheader("Content-Length", str(body_len))
    conn.endheaders()
    return conn


def _s3_error(method, key, status, body_bytes):
    detail = body_bytes.decode("utf-8", "replace")[:300]
    return AwsError(f"S3 {method} {key} failed HTTP {status}: {detail}",
                    status=status)


def s3_put_file(path, name):
    payload_hash = sha256_of_file(path)
    url = f"https://{S3_HOST}/{quote(name, safe='/-_.~')}"
    size = os.path.getsize(path)
    conn = _s3_open("PUT", url, payload_hash, extra_headers={
        "X-Amz-Server-Side-Encryption": "aws:kms",
        "X-Amz-Server-Side-Encryption-Aws-Kms-Key-Id": S3_KMS_KEY_ID,
    }, body_len=size)
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            conn.send(block)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    if resp.status != 200:
        raise _s3_error("PUT", name, resp.status, body)
    return {"ok": True}


def s3_download_hash(name):
    """Download the object and return its SHA-256 (verify phase)."""
    url = f"https://{S3_HOST}/{quote(name, safe='/-_.~')}"
    conn = _s3_open("GET", url, EMPTY_SHA256)
    resp = conn.getresponse()
    if resp.status != 200:
        body = resp.read()
        conn.close()
        raise _s3_error("GET", name, resp.status, body)
    h = hashlib.sha256()
    while True:
        block = resp.read(CHUNK)
        if not block:
            break
        h.update(block)
    conn.close()
    return h.hexdigest()


def s3_delete(name):
    url = f"https://{S3_HOST}/{quote(name, safe='/-_.~')}"
    conn = _s3_open("DELETE", url, EMPTY_SHA256)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    if resp.status not in (200, 204):
        raise _s3_error("DELETE", name, resp.status, body)
    return {"ok": True}


def s3_list_stats():
    """One ListObjectsV2 page (MaxKeys=1000): (object_count, bytes)."""
    url = f"https://{S3_HOST}/?list-type=2&max-keys=1000"
    conn = _s3_open("GET", url, EMPTY_SHA256)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    if resp.status != 200:
        raise _s3_error("LIST", S3_BUCKET, resp.status, body)
    count = 0
    total = 0
    for el in ET.fromstring(body).iter():
        if el.tag.split("}")[-1] == "Contents":
            count += 1
            for child in el:
                if child.tag.split("}")[-1] == "Size":
                    total += int(child.text or 0)
    return count, total


_s3_probe = {"at": 0.0, "entry": None}


def s3_health_entry():
    entry = {"name": "aws-s3", "type": "s3", "bucket": S3_BUCKET,
             "configured": s3_configured(), "ok": False}
    if not s3_configured():
        entry["note"] = ("Design backend from the document; activates when the "
                         "bucket and host credentials are provided.")
        return entry
    now = time.time()
    if _s3_probe["entry"] is not None and now - _s3_probe["at"] < 30:
        return dict(_s3_probe["entry"])
    try:
        count, total = with_retries(s3_list_stats, base_wait=1, cap=2)
        entry.update({"ok": True, "objects": count, "bytes_stored": total})
    except Exception as ex:  # noqa: BLE001 - reported, never swallowed
        entry["error"] = str(ex)[:200]
    _s3_probe["at"] = now
    _s3_probe["entry"] = entry
    return dict(entry)

