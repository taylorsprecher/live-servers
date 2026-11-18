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
the service's data volume. Backends the document also designs — AWS
S3 (bucket "zillion-tb-aws-bucket"), MinIO, Ceph, GCS — need their
clusters/credentials to exist first; the health endpoint reports them
honestly as not configured rather than pretending. "1 Zillion TB" is
the design's name; the health endpoint reports the host's real
capacity alongside it.

Python stdlib only. Runs under BASE_PATH (default /zillion).
"""
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

BASE = os.environ.get("BASE_PATH", "/zillion").rstrip("/")
PORT = int(os.environ.get("PORT", "8772"))
DATA = os.environ.get("ZILLION_DATA", "/data")
STARTED = time.time()

BACKENDS = [
    {"name": "primary", "type": "local", "path": os.path.join(DATA, "primary")},
    {"name": "replica", "type": "local", "path": os.path.join(DATA, "replica")},
]
S3_BUCKET = os.environ.get("ZILLION_S3_BUCKET", "").strip()
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


def with_retries(fn, attempts=3):
    """Exponential backoff, tenacity-style: 3 attempts, waits grow
    0.5s -> 1s -> 2s (local profile; the document's network profile
    is 2-10s for cloud backends)."""
    wait = 0.5
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except Exception as ex:  # noqa: BLE001 - isolated per backend by design
            last = ex
            time.sleep(wait)
            wait = min(wait * 2, 8)
    raise last


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
    out.append({
        "name": "aws-s3", "type": "s3", "bucket": S3_BUCKET or "zillion-tb-aws-bucket",
        "configured": bool(S3_BUCKET), "ok": False,
        "note": "Design backend from the document; activates when the bucket and host credentials are provided.",
    })
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
    """Copy the staged object to every local backend, concurrently,
    each with retries; returns {backend_name: result dict}."""
    def put(backend):
        dest = os.path.join(backend["path"], name)
        os.makedirs(backend["path"], exist_ok=True)
        staging = dest + ".part"
        shutil.copyfile(tmp_path, staging)
        os.replace(staging, dest)
        return {"ok": True}
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(BACKENDS)) as pool:
        futures = {pool.submit(with_retries, lambda b=b: put(b)): b for b in BACKENDS}
        for fut in concurrent.futures.as_completed(futures):
            b = futures[fut]
            try:
                results[b["name"]] = fut.result()
            except Exception as ex:  # noqa: BLE001 - fault isolation per the design
                results[b["name"]] = {"ok": False, "error": str(ex)[:200]}
    return results


def verify(name, expected_hash):
    """Re-read and re-hash every local backend copy (design Phase 2)."""
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
        with _lock:
            manifest = load_manifest()
            manifest.pop(name, None)
            save_manifest(manifest)
        self._send(200, {"name": name, "removed_from": removed})


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
