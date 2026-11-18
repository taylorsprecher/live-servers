#!/usr/bin/env python3
"""Zillion Storage — the runnable core of Taylor's "Python Cloud —
1 Zillion TB" design document (tmp3D_Python_Cloud_1Zillion_TB.docx,
filed 2026-11-03, installed 2026-11-18 at Taylor's direction).

What the document designs, and what runs here:
  - A Python control plane for object storage (Python stays the
    language for Taylor's AWS/S3 work — standing rule, 2026-11-10).
  - Streaming ingest: request bodies are written to disk in chunks
    while the SHA-256 is computed — payloads are never loaded whole
    into RAM (the document's memory-safety rule).
  - Concurrent replication: each object is copied to every configured
    backend in parallel (ThreadPoolExecutor), with per-backend fault
    isolation — one backend failing does not block the others.
  - Retry with exponential backoff on each backend write (the
    document's tenacity profile: 3 attempts; backoff is shorter here
    for local backends, the network profile is noted in the README).
  - SHA-256 verification phase: after upload, and on demand, every
    backend copy is re-read and re-hashed and compared bit-for-bit
    against the ingest hash (the document's Phase 2).

Backends that are real today: two local stores (primary + replica) on
the service's data volume, plus — when AWS base credentials are
provided in the environment — the document's AWS S3 backend (bucket
"zillion-tb-aws-bucket", us-west-2, SSE-KMS). The service host lives
in a different AWS account than the bucket, so the S3 backend assumes
the scoped IAM role zillion-control-plane-role via STS AssumeRole
(SigV4-signed, stdlib only) and signs S3 requests with the temporary
credentials. Without credentials the health endpoint reports the S3
backend honestly as not configured rather than pretending. MinIO,
Ceph, and GCS remain design-only. "1 Zillion TB" is the design's
name; the health endpoint reports the host's real capacity alongside
it.

Python stdlib only. Runs under BASE_PATH (default /zillion).
"""
import concurrent.futures
import datetime
import hashlib
import hmac
import http.client
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse

BASE = os.environ.get("BASE_PATH", "/zillion").rstrip("/")
PORT = int(os.environ.get("PORT", "8772"))
DATA = os.environ.get("ZILLION_DATA", "/data")
STARTED = time.time()

BACKENDS = [
    {"name": "primary", "type": "local", "path": os.path.join(DATA, "primary")},
    {"name": "replica", "type": "local", "path": os.path.join(DATA, "replica")},
]

# --- AWS S3 backend configuration (all optional; the backend is ------
# --- configured only when BOTH base credential variables are set) ----
S3_BUCKET = os.environ.get("ZILLION_S3_BUCKET", "zillion-tb-aws-bucket").strip() \
    or "zillion-tb-aws-bucket"
S3_REGION = os.environ.get("ZILLION_S3_REGION", "us-west-2").strip() or "us-west-2"
S3_ROLE_ARN = os.environ.get(
    "ZILLION_S3_ROLE_ARN",
    "arn:aws:iam::392349106458:role/zillion-control-plane-role").strip()
S3_KMS_KEY_ID = os.environ.get(
    "ZILLION_S3_KMS_KEY_ID", "19f314ab-afa4-4746-a2b5-39427104158c").strip()
AWS_ACCESS_KEY_ID = os.environ.get("ZILLION_AWS_ACCESS_KEY_ID", "").strip()
AWS_SECRET_ACCESS_KEY = os.environ.get("ZILLION_AWS_SECRET_ACCESS_KEY", "").strip()
S3_HOST = f"{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CHUNK = 1024 * 1024
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\- ]{0,199}$")
MANIFEST_PATH = os.path.join(DATA, "manifest.json")
_lock = threading.Lock()


def load_manifest():
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_manifest(manifest):
    os.makedirs(DATA, exist_ok=True)
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    os.replace(tmp, MANIFEST_PATH)


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def with_retries(fn, attempts=3, base_wait=0.5, cap=8):
    """Exponential backoff, tenacity-style: 3 attempts, waits grow
    from base_wait (local profile 0.5s -> 1s -> 2s; the document's
    network profile for cloud backends is 2-10s)."""
    wait = base_wait
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except Exception as ex:  # noqa: BLE001 - isolated per backend by design
            last = ex
            time.sleep(wait)
            wait = min(wait * 2, cap)
    raise last


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


def backend_health():
    out = []
    for b in BACKENDS:
        entry = {"name": b["name"], "type": b["type"], "configured": True}
        try:
            os.makedirs(b["path"], exist_ok=True)
            usage = shutil.disk_usage(b["path"])
            entry.update({"ok": True, "free_bytes": usage.free, "total_bytes": usage.total})
        except Exception as ex:  # noqa: BLE001
            entry.update({"ok": False, "error": str(ex)[:200]})
        out.append(entry)
    out.append(s3_health_entry())
    out.append({
        "name": "minio", "type": "minio", "configured": False, "ok": False,
        "note": "Design backend from the document; no MinIO cluster exists yet.",
    })
    out.append({
        "name": "ceph", "type": "ceph", "configured": False, "ok": False,
        "note": "Design backend from the document; no Ceph cluster exists yet.",
    })
    out.append({
        "name": "gcs", "type": "gcs", "configured": False, "ok": False,
        "note": "Design backend from the document; no Google Cloud credentials exist yet.",
    })
    return out


def replicate(tmp_path, name):
    """Copy the staged object to every backend, concurrently, each
    with retries; returns {backend_name: result dict}. The set is the
    two local stores plus, when credentials are configured, aws-s3.
    Fault isolation per the design: one backend failing never blocks
    or fails the others — its result carries the error."""
    def put_local(backend):
        dest = os.path.join(backend["path"], name)
        os.makedirs(backend["path"], exist_ok=True)
        staging = dest + ".part"
        shutil.copyfile(tmp_path, staging)
        os.replace(staging, dest)
        return {"ok": True}
    jobs = [(b["name"], lambda b=b: with_retries(lambda: put_local(b)))
            for b in BACKENDS]
    if s3_configured():
        jobs.append(("aws-s3", lambda: with_retries(
            lambda: s3_put_file(tmp_path, name), base_wait=2, cap=10)))
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(job): name_ for name_, job in jobs}
        for fut in concurrent.futures.as_completed(futures):
            bname = futures[fut]
            try:
                results[bname] = fut.result()
            except Exception as ex:  # noqa: BLE001 - fault isolation per the design
                results[bname] = {"ok": False, "error": str(ex)[:200]}
    return results


def verify(name, expected_hash):
    """Re-read and re-hash every backend copy (design Phase 2). The
    aws-s3 copy, when configured, is downloaded and hashed the same
    way; an AWS failure is reported as an ERROR entry with the error
    string, never hidden."""
    out = {}
    for b in BACKENDS:
        path = os.path.join(b["path"], name)
        if not os.path.exists(path):
            out[b["name"]] = {"ok": False, "result": "MISSING"}
            continue
        actual = sha256_of_file(path)
        out[b["name"]] = {"ok": actual == expected_hash,
                          "result": "PASSED" if actual == expected_hash else "FAILED",
                          "sha256": actual}
    if s3_configured():
        try:
            actual = with_retries(lambda: s3_download_hash(name),
                                  base_wait=2, cap=10)
            out["aws-s3"] = {"ok": actual == expected_hash,
                             "result": "PASSED" if actual == expected_hash else "FAILED",
                             "sha256": actual}
        except AwsError as ex:
            if ex.status == 404:
                out["aws-s3"] = {"ok": False, "result": "MISSING"}
            else:
                out["aws-s3"] = {"ok": False, "result": "ERROR",
                                 "error": str(ex)[:200]}
        except Exception as ex:  # noqa: BLE001 - reported, never swallowed
            out["aws-s3"] = {"ok": False, "result": "ERROR",
                             "error": str(ex)[:200]}
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep logs quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        p = urlparse(self.path).path
        if BASE and p.startswith(BASE):
            p = p[len(BASE):]
        return p or "/"

    def do_GET(self):
        p = self._path()
        if p in ("/", ""):
            index = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
            try:
                with open(index, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except Exception:
                self._send(200, {"service": "zillion-storage"})
            return
        if p == "/health":
            with _lock:
                manifest = load_manifest()
            self._send(200, {
                "status": "ok",
                "service": "zillion-storage",
                "design": "Python Cloud - 1 Zillion TB (design name; real capacity in backends)",
                "backends": backend_health(),
                "objects": len(manifest),
                "bytes_stored": sum(v.get("size", 0) for v in manifest.values()),
                "uptime_seconds": int(time.time() - STARTED),
            })
            return
        if p == "/objects":
            with _lock:
                manifest = load_manifest()
            items = [{"name": k, **{f: v[f] for f in ("size", "sha256", "stored_at") if f in v}}
                     for k, v in sorted(manifest.items())]
            self._send(200, {"objects": items})
            return
        if p.startswith("/objects/"):
            name = unquote(p[len("/objects/"):])
            if not NAME_OK.match(name):
                self._send(400, {"error": "bad object name"})
                return
            for b in BACKENDS:
                path = os.path.join(b["path"], name)
                if os.path.exists(path):
                    size = os.path.getsize(path)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    with open(path, "rb") as fh:
                        while True:
                            block = fh.read(CHUNK)
                            if not block:
                                break
                            self.wfile.write(block)
                    return
            self._send(404, {"error": "object not found"})
            return
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        p = self._path()
        if not p.startswith("/objects/"):
            self._send(404, {"error": "not found"})
            return
        name = unquote(p[len("/objects/"):])
        if not NAME_OK.match(name):
            self._send(400, {"error": "bad object name"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        os.makedirs(DATA, exist_ok=True)
        tmp_path = os.path.join(DATA, ".ingest-" + str(int(time.time() * 1000)))
        h = hashlib.sha256()
        written = 0
        with open(tmp_path, "wb") as fh:
            remaining = length
            while remaining > 0:
                block = self.rfile.read(min(CHUNK, remaining))
                if not block:
                    break
                fh.write(block)
                h.update(block)
                written += len(block)
                remaining -= len(block)
        digest = h.hexdigest()
        results = replicate(tmp_path, name)
        os.unlink(tmp_path)
        checks = verify(name, digest)
        with _lock:
            manifest = load_manifest()
            manifest[name] = {
                "size": written, "sha256": digest,
                "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "backends": sorted(k for k, v in results.items() if v.get("ok")),
            }
            save_manifest(manifest)
        self._send(200, {
            "name": name, "size": written, "sha256": digest,
            "backends": results, "verification": checks,
            "verified": all(v.get("result") == "PASSED" for v in checks.values()),
        })

    do_POST = do_PUT

    def do_DELETE(self):
        p = self._path()
        if p.startswith("/verify/"):
            self._send(405, {"error": "verify is POST"})
            return
        if not p.startswith("/objects/"):
            self._send(404, {"error": "not found"})
            return
        name = unquote(p[len("/objects/"):])
        if not NAME_OK.match(name):
            self._send(400, {"error": "bad object name"})
            return
        removed = []
        for b in BACKENDS:
            path = os.path.join(b["path"], name)
            if os.path.exists(path):
                os.unlink(path)
                removed.append(b["name"])
        delete_errors = {}
        if s3_configured():
            try:
                with_retries(lambda: s3_delete(name), base_wait=2, cap=10)
                removed.append("aws-s3")
            except Exception as ex:  # noqa: BLE001 - reported in the response
                delete_errors["aws-s3"] = str(ex)[:200]
        with _lock:
            manifest = load_manifest()
            manifest.pop(name, None)
            save_manifest(manifest)
        body = {"name": name, "removed_from": removed}
        if delete_errors:
            body["delete_errors"] = delete_errors
        self._send(200, body)


def main():
    # POST /verify/<name> is routed here by method override below.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"zillion-storage listening on :{PORT} base={BASE} data={DATA}", flush=True)
    server.serve_forever()


# Route POST /verify/<name> distinctly from PUT-style uploads.
_orig_post = Handler.do_POST


def _post(self):
    p = self._path()
    if p.startswith("/verify/"):
        name = unquote(p[len("/verify/"):])
        if not NAME_OK.match(name):
            self._send(400, {"error": "bad object name"})
            return
        with _lock:
            manifest = load_manifest()
        entry = manifest.get(name)
        if not entry:
            self._send(404, {"error": "object not found"})
            return
        checks = verify(name, entry["sha256"])
        self._send(200, {"name": name, "sha256": entry["sha256"], "verification": checks,
                         "verified": all(v.get("result") == "PASSED" for v in checks.values())})
        return
    _orig_post(self)


Handler.do_POST = _post

if __name__ == "__main__":
    main()
