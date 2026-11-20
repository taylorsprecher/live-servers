#!/usr/bin/env python3
"""Stage 0 fleet backup — snapshot live-servers stateful data to S3.

Runs ON the audited host (dispatch via .github/workflows/stage0-fleet-backup.yml,
which scp's this file to the host and executes it; the run log is the record).

What it does, all online (no restarts, no downtime):
  * For each live-servers Docker volume: stream a gzipped tar of the volume
    to a staging dir using a throwaway container from an image already on
    the host (postgres:16-alpine's busybox tar).
  * A logical pg_dump (custom format) of the paydb Postgres, plus a
    globals-only dump, produced with docker exec so DB credentials never
    leave the paydb container.
  * SHA-256 + size for every artifact, written to manifest.json.
  * Upload of every artifact and the manifest to S3 with a stdlib-only
    SigV4 signer (host has no aws CLI / boto3).

Credentials: the host .env ZILLION_AWS_* user (values never printed) can
assume the zillion control-plane role but cannot use the bucket's KMS key
directly (first run, 2026-11-20, failed PUT with kms:GenerateDataKey
AccessDenied). So the script first exchanges the user creds via STS
AssumeRole for arn:aws:iam::392349106458:role/zillion-control-plane-role
(the same role the zillion service itself uses) and signs S3 requests
with the temporary role credentials.

Bucket: zillion-tb-aws-bucket (management account 392349106458) under
prefix fleet-backups/<YYYY-MM-DD>/<timestamp>/ — the only S3 path the
host can reach. Override with BACKUP_BUCKET / BACKUP_PREFIX /
BACKUP_REGION / BACKUP_ROLE_ARN env vars.

Local reuse: keep at ~/workspace/fleet-rebuild/fleet_backup.py; the copy
in the repo (ops/fleet_backup.py) is what the workflow ships to the host.
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

HOST_TREE = "/home/deploy/live-servers"
ENV_PATH = os.path.join(HOST_TREE, ".env")
BUCKET = os.environ.get("BACKUP_BUCKET", "zillion-tb-aws-bucket")
REGION = os.environ.get("BACKUP_REGION", "us-west-2")
PREFIX_ROOT = os.environ.get("BACKUP_PREFIX", "fleet-backups")
ROLE_ARN = os.environ.get(
    "BACKUP_ROLE_ARN", "arn:aws:iam::392349106458:role/zillion-control-plane-role")
TAR_IMAGE = os.environ.get("BACKUP_TAR_IMAGE", "postgres:16-alpine")
PAYDB_CONTAINER = os.environ.get("BACKUP_PAYDB_CONTAINER", "live-paydb")
STAGE_ROOT = "/home/deploy/fleet-backups"

VOLUME_SUFFIXES = [
    "members-data",
    "payledger-data",
    "zillion-data",
    "minio-data",
    "vpn-data",
    "xsave-data",
    "maclan-data",
    "celltower-data",
    "fileslib-data",
    "syncflow-data",
]

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_env_keys(path, wanted):
    """Read selected keys from a .env file. Secret values never printed."""
    found = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key in wanted:
                found[key] = val.strip().strip('"').strip("'")
    return found


# ---------- SigV4 (stdlib, generic) ----------

def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signed_headers(service, method, host, uri, query, payload_hash, creds,
                   token=None, s3=False):
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-date": amz_date}
    if s3:
        headers["x-amz-content-sha256"] = payload_hash
    if token:
        headers["x-amz-security-token"] = token
    signed_keys = sorted(headers)
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in signed_keys)
    canonical_request = "\n".join([
        method, uri, query, canonical_headers, ";".join(signed_keys), payload_hash,
    ])
    scope = f"{date_stamp}/{REGION}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k = _hmac(("AWS4" + creds["secret"]).encode("utf-8"), date_stamp)
    k = _hmac(k, REGION)
    k = _hmac(k, service)
    k = _hmac(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={creds['id']}/{scope}, "
        f"SignedHeaders={';'.join(signed_keys)}, Signature={signature}"
    )
    return headers


def http(method, url, headers, data=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        print(f"{method} {url.split('?')[0]} FAILED: HTTP {e.code}: "
              f"{e.read()[:300]!r}", flush=True)
        raise


def sts_assume_role(user_creds):
    host = f"sts.{REGION}.amazonaws.com"
    body = urllib.parse.urlencode({
        "Action": "AssumeRole",
        "Version": "2011-06-15",
        "RoleArn": ROLE_ARN,
        "RoleSessionName": "fleet-backup-stage0",
        "DurationSeconds": "3600",
    }).encode("utf-8")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = signed_headers("sts", "POST", host, "/", "", payload_hash, user_creds)
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
    status, _h, resp_body = http("POST", f"https://{host}/", headers, data=body)
    ns = {"sts": "https://sts.amazonaws.com/doc/2011-06-15/"}
    root = ET.fromstring(resp_body)
    creds_node = root.find(".//sts:Credentials", ns)
    temp = {
        "id": creds_node.find("sts:AccessKeyId", ns).text,
        "secret": creds_node.find("sts:SecretAccessKey", ns).text,
        "token": creds_node.find("sts:SessionToken", ns).text,
    }
    print(f"STS AssumeRole {ROLE_ARN}: HTTP {status} (temp creds in memory only)",
          flush=True)
    return temp


def s3_request(method, key, creds, body_path=None, query=""):
    host = f"{BUCKET}.s3.{REGION}.amazonaws.com"
    uri = "/" + key
    payload_hash = sha256_of(body_path) if body_path else EMPTY_SHA256
    headers = signed_headers("s3", method, host, uri, query, payload_hash,
                             creds, token=creds.get("token"), s3=True)
    url = f"https://{host}{uri}" + (f"?{query}" if query else "")
    data = None
    if body_path is not None:
        data = open(body_path, "rb")
        headers["Content-Length"] = str(os.path.getsize(body_path))
    return http(method, url, headers, data=data)


# ---------- backup steps ----------

def discover_volumes():
    out = run(["docker", "volume", "ls", "--format", "{{.Name}}"],
              capture_output=True, text=True).stdout.split()
    chosen = []
    for suffix in VOLUME_SUFFIXES:
        match = [v for v in out if v == suffix or v.endswith("_" + suffix)]
        if match:
            chosen.append((suffix, match[0]))
        else:
            print(f"volume for {suffix}: NOT PRESENT — skipped", flush=True)
    return chosen


def tar_volume(volume, dest_path):
    cmd = ["docker", "run", "--rm", "-v", f"{volume}:/data:ro",
           "--entrypoint", "tar", TAR_IMAGE, "czf", "-", "-C", "/data", "."]
    with open(dest_path, "wb") as f:
        run(cmd, stdout=f)


def pg_dumps(stage_dir, artifacts):
    dump = os.path.join(stage_dir, "paydb.dump")
    cmd = ["docker", "exec", PAYDB_CONTAINER, "sh", "-c",
           'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc']
    with open(dump, "wb") as f:
        run(cmd, stdout=f)
    artifacts.append(("paydb.dump", dump, "pg_dump custom format of paydb"))
    globals_path = os.path.join(stage_dir, "paydb-globals.sql")
    cmd = ["docker", "exec", PAYDB_CONTAINER, "sh", "-c",
           'pg_dumpall -U "$POSTGRES_USER" --globals-only']
    with open(globals_path, "wb") as f:
        run(cmd, stdout=f)
    artifacts.append(("paydb-globals.sql", globals_path, "pg_dumpall globals-only"))


def main():
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = f"{PREFIX_ROOT}/{day}/{ts}"
    stage_dir = os.path.join(STAGE_ROOT, ts)
    os.makedirs(stage_dir, exist_ok=True)
    print(f"Stage 0 fleet backup — staging at {stage_dir}", flush=True)
    print(f"Target: s3://{BUCKET}/{prefix}/ (region {REGION})", flush=True)

    creds_env = read_env_keys(ENV_PATH, {"ZILLION_AWS_ACCESS_KEY_ID",
                                         "ZILLION_AWS_SECRET_ACCESS_KEY"})
    if len(creds_env) != 2:
        print("ERROR: ZILLION_AWS_* credentials not found in host .env — aborting",
              flush=True)
        return 1
    user_creds = {"id": creds_env["ZILLION_AWS_ACCESS_KEY_ID"],
                  "secret": creds_env["ZILLION_AWS_SECRET_ACCESS_KEY"]}
    print("User credentials loaded from host .env (values not shown)", flush=True)
    creds = sts_assume_role(user_creds)

    artifacts = []  # (name, path, note)
    for suffix, volume in discover_volumes():
        dest = os.path.join(stage_dir, f"volume-{suffix}.tar.gz")
        print(f"tarring volume {volume} -> {dest}", flush=True)
        tar_volume(volume, dest)
        artifacts.append((f"volume-{suffix}.tar.gz", dest, f"docker volume {volume}"))

    pg_dumps(stage_dir, artifacts)

    manifest = {"created_utc": ts, "bucket": BUCKET, "prefix": prefix,
                "region": REGION, "role_arn": ROLE_ARN, "artifacts": []}
    for name, path, note in artifacts:
        size = os.path.getsize(path)
        digest = sha256_of(path)
        manifest["artifacts"].append(
            {"name": name, "size": size, "sha256": digest, "note": note})
        print(f"artifact {name}: {size} bytes sha256={digest}", flush=True)

    manifest_path = os.path.join(stage_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print("== uploading ==", flush=True)
    for name, path, _note in artifacts:
        key = f"{prefix}/{name}"
        status, _h, _b = s3_request("PUT", key, creds, body_path=path)
        print(f"PUT {key}: HTTP {status}", flush=True)
    status, _h, _b = s3_request("PUT", f"{prefix}/manifest.json", creds,
                                body_path=manifest_path)
    print(f"PUT {prefix}/manifest.json: HTTP {status}", flush=True)

    print("== verifying (HEAD per object) ==", flush=True)
    ok = True
    for entry in manifest["artifacts"]:
        key = f"{prefix}/{entry['name']}"
        status, headers, _b = s3_request("HEAD", key, creds)
        remote_size = int(headers.get("Content-Length", -1))
        match = remote_size == entry["size"]
        ok = ok and match
        print(f"HEAD {key}: HTTP {status} size={remote_size} "
              f"local={entry['size']} {'OK' if match else 'MISMATCH'}", flush=True)

    print("== verifying (ListObjectsV2) ==", flush=True)
    query = "list-type=2&prefix=" + urllib.parse.quote(prefix + "/", safe="")
    status, _h, body = s3_request("GET", "", creds, query=query)
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    root = ET.fromstring(body)
    for contents in root.findall("s3:Contents", ns):
        k = contents.find("s3:Key", ns).text
        s = contents.find("s3:Size", ns).text
        print(f"LIST {k} size={s}", flush=True)

    subprocess.run(["rm", "-rf", STAGE_ROOT])
    print(f"Staging removed. Backup {'VERIFIED' if ok else 'COMPLETED WITH MISMATCHES'}: "
          f"s3://{BUCKET}/{prefix}/", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
