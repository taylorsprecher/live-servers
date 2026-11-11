#!/usr/bin/env python3
"""End-to-end verification of the Xbox Cloud Save demo service.

Exercises the document's presigned-URL flow exactly as a client would,
against a running demo service (local dev server or the public gateway
path):

  1. GET  {base}/health                          -> 200, status ok
  2. POST {base}/auth/dev-login {gamertag}        -> demo XBL3.0 token
  3. POST {base}/saves/{name}/upload-url          -> {uploadUrl, expiresIn}
     (the document's route: POST /saves/{saveId}/upload-url)
  4. PUT  uploadUrl with demo save bytes          -> stored
  5. GET  {base}/saves/{name}/download-url        -> {downloadUrl}
  6. GET  downloadUrl                             -> bytes, compared
     byte-for-byte with what was PUT
  7. GET  {base}/saves                            -> the save is listed
  8. DELETE {base}/saves/{name}                   -> cleanup

The check also asserts the handed-out URLs expire in 900 seconds
(15 minutes, per the plan). Exits 0 on PASS, 1 on any failure, with one
line per step. Standard library only — runs anywhere Python 3 does.

Usage:
  python3 verify_roundtrip.py [--base http://52.41.84.116:443/xbox]
                              [--gamertag InstallerCheck]
                              [--keep]   (do not delete the demo save)
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

FAILURES = []


def step(ok, label, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def call(method, url, body=None, headers=None, raw=False):
    data = body
    hdrs = dict(headers or {})
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload or b"{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if raw:
            return exc.code, payload
        try:
            return exc.code, json.loads(payload or b"{}")
        except ValueError:
            return exc.code, {"error": payload.decode("utf-8", "replace")}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://52.41.84.116:443/xbox",
                    help="service base URL including the /xbox prefix")
    ap.add_argument("--gamertag", default="InstallerCheck")
    ap.add_argument("--keep", action="store_true",
                    help="do not delete the demo save afterwards")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    code, health = call("GET", f"{base}/health")
    step(code == 200 and health.get("status") == "ok",
         "health", f"GET {base}/health -> {code}")

    code, login = call("POST", f"{base}/auth/dev-login",
                       {"gamertag": args.gamertag})
    step(code == 200 and "authorization" in login,
         "dev-login", f"gamertag {args.gamertag!r} -> xuid {login.get('xuid')}")
    if FAILURES:
        return finish()
    auth = {"Authorization": login["authorization"]}

    name = "install-check.dat"
    payload = (b"XSAVE-INSTALL-CHECK\x00\x01\x02 demo save bytes for the "
               b"automatic-installer verification round-trip.")

    code, up = call("POST", f"{base}/saves/{name}/upload-url",
                    {}, headers=auth)
    step(code == 200 and "uploadUrl" in up,
         "upload-url", f"POST /saves/{name}/upload-url -> {code}")
    step(up.get("expiresIn") == 900,
         "upload-url expiry", f"expiresIn={up.get('expiresIn')} (plan: 900)")
    if FAILURES:
        return finish()

    code, _ = call("PUT", up["uploadUrl"], body=payload, raw=True)
    step(code == 200, "PUT save bytes via upload URL", f"-> {code}")

    code, down = call("GET", f"{base}/saves/{name}/download-url",
                      headers=auth)
    step(code == 200 and "downloadUrl" in down,
         "download-url", f"GET /saves/{name}/download-url -> {code}")
    if not FAILURES:
        code, got = call("GET", down["downloadUrl"], raw=True)
        step(code == 200 and got == payload,
             "GET save bytes via download URL",
             f"-> {code}, {len(got)} bytes, byte-identical: {got == payload}")

    code, listing = call("GET", f"{base}/saves", headers=auth)
    names = [s.get("name") for s in listing.get("saves", [])]
    step(code == 200 and name in names, "list saves", f"saves={names}")

    if not args.keep:
        code, _ = call("DELETE", f"{base}/saves/{name}", headers=auth)
        step(code == 200, "cleanup: delete demo save", f"-> {code}")

    return finish()


def finish():
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} step(s) failed: {', '.join(FAILURES)})")
        return 1
    print("RESULT: PASS — presigned-URL save round-trip verified end to end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
