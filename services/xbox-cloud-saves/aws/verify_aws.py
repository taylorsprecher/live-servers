#!/usr/bin/env python3
"""Verify the REAL Xbox Cloud Save API end to end.

Chain under test (the document's production flow):

  1. POST {endpoint}/saves/{saveId}/upload-url     -> pre-signed URL
  2. PUT  the demo save bytes to that URL          -> lands in the
     real bucket under users/{test-xuid}/savefiles/{saveId}
  3. GET  {endpoint}/saves/{saveId}/download-url   -> pre-signed URL
  4. GET  the bytes back                           -> byte-identical
  5. Confirm the object key in the bucket (boto3), then DELETE the
     test object and confirm the download URL now 404s.

Identity: real Xbox Live validation needs Microsoft-issued XSTS
tokens for a registered relying party, which does not exist yet, so
verification uses the Lambda's clearly-labeled TEST identity
(X-Xsave-Test-Xuid header; only works when the provisioner set
XSAVE_TEST_XUID with --enable-test-identity). The auth header used
here is obviously not an Xbox token — that is the point, and the
README documents removing the test identity for real use.

Requires: the API endpoint from provision_aws.py's output, and (for
step 5) a boto3 session for account 589997396938.

  python3 verify_aws.py --endpoint https://abc123.execute-api.us-west-2.amazonaws.com \
      --test-xuid 9000000000000001 --bucket xbox-cloud-saves-589997396938-us-west-2
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
    ap.add_argument("--endpoint", required=True,
                    help="API Gateway endpoint from provision_aws.py")
    ap.add_argument("--test-xuid", required=True,
                    help="the labeled test XUID enabled on the Lambda")
    ap.add_argument("--bucket", default="xbox-cloud-saves-589997396938-us-west-2")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--save-id", default="verify-check.dat")
    args = ap.parse_args()

    base = args.endpoint.rstrip("/")
    headers = {"X-Xsave-Test-Xuid": args.test_xuid}
    payload = (b"XSAVE-REAL-VERIFY\x00\x01\x02 demo save for the "
               b"real-AWS verification round-trip.")
    expected_key = f"users/{args.test_xuid}/savefiles/{args.save_id}"

    code, up = call("POST", f"{base}/saves/{args.save_id}/upload-url",
                    {}, headers=headers)
    step(code == 200 and "uploadUrl" in up, "upload-url",
         f"POST -> {code}")
    step(up.get("key") == expected_key, "key layout",
         f"key={up.get('key')}")
    step(up.get("expiresIn") == 900, "expiry", f"expiresIn={up.get('expiresIn')}")
    if FAILURES:
        return finish()

    code, _ = call("PUT", up["uploadUrl"], body=payload, raw=True)
    step(code == 200, "PUT bytes to pre-signed S3 URL", f"-> {code}")

    code, down = call("GET", f"{base}/saves/{args.save_id}/download-url",
                      headers=headers)
    step(code == 200 and "downloadUrl" in down, "download-url",
         f"GET -> {code}")
    if not FAILURES:
        code, got = call("GET", down["downloadUrl"], raw=True)
        step(code == 200 and got == payload, "GET bytes back",
             f"-> {code}, byte-identical: {got == payload}")

    # Confirm placement in the real bucket, then remove the test object.
    try:
        import boto3
        s3 = boto3.client("s3", region_name=args.region)
        head = s3.head_object(Bucket=args.bucket, Key=expected_key)
        step(head["ContentLength"] == len(payload), "object in bucket",
             f"{args.bucket}/{expected_key} ({head['ContentLength']} bytes)")
        s3.delete_object(Bucket=args.bucket, Key=expected_key)
        listing = s3.list_objects_v2(Bucket=args.bucket, Prefix=expected_key)
        step(listing.get("KeyCount", 0) == 0, "test object removed",
             f"remaining keys under test prefix: {listing.get('KeyCount', 0)}")
    except Exception as exc:  # noqa: BLE001 — report, don't mask
        step(False, "bucket confirmation", f"{type(exc).__name__}: {exc}")

    code, _ = call("GET", f"{base}/saves/{args.save_id}/download-url",
                   headers=headers)
    step(code == 404, "download-url 404s after removal", f"-> {code}")

    return finish()


def finish():
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} step(s) failed: {', '.join(FAILURES)})")
        return 1
    print("RESULT: PASS — real API Gateway -> Lambda -> S3 round-trip verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
