# Xbox Cloud Save — REAL AWS build

Taylor's decision (2026-11-11): **"Make all real."** This directory
provisions the design from the "Xbox AWS S3 1" document as real AWS
resources — not the demo stubs — in Taylor's AWS **member account
589997396938 ("Index Zero")**, region **us-west-2**, Python throughout
(Taylor's standing rule for AWS/S3 work).

| File | What it is |
|---|---|
| `provision_aws.py` | Idempotent provisioner (boto3). Account-guarded: aborts before any change unless the caller is account 589997396938. `--destroy` tears everything down. |
| `lambda_presign.py` | The Lambda code (deployed as `lambda_function.py`): the two presigned-URL routes with XSTS validation. |
| `verify_aws.py` | End-to-end verification: upload URL → PUT a demo save → download URL → GET byte-identical → confirm the bucket key → remove the test object. |

## What gets created

| Resource | Name / shape |
|---|---|
| S3 bucket | `xbox-cloud-saves-589997396938-us-west-2` — SSE default encryption (AES256), all public access blocked, versioning on. Keys: `users/{xuid}/savefiles/{saveId}`. |
| IAM role | `xsave-lambda-exec` — Lambda trust only. Policies: `s3:GetObject` + `s3:PutObject` on `arn:aws:s3:::<bucket>/users/*` **only**, plus CloudWatch Logs write on the function's own log group. Nothing broader. |
| Lambda | `xsave-presign-api` — Python 3.12, 256 MB, 15 s. Routes: `POST /saves/{saveId}/upload-url`, `GET /saves/{saveId}/download-url`. Pre-signed URLs expire in **900 s (15 min)**. |
| API Gateway | HTTP API `xsave-api`, stage `$default` (auto-deploy), CloudWatch access logging to `/aws/apigateway/xsave-api`. |
| CloudWatch | Function logs (`/aws/lambda/xsave-presign-api`) + API access logs. |

Free-tier scale: no provisioned concurrency, no paid extras.

## Run it

The code needs an **authenticated AWS session for the member
account** — there are no credentials in this repo, by design. The
intended session is Taylor's **AWS CloudShell** (sign in to the
"Index Zero" member account first — the console defaults to the
organization management account 392349106458, which is the wrong
account; `provision_aws.py` hard-aborts if the caller is not
589997396938, so a mistake stops before anything is created).

```bash
# in CloudShell (account 589997396938), from a checkout of this repo:
cd services/xbox-cloud-saves/aws
python3 provision_aws.py --enable-test-identity   # first run
python3 verify_aws.py --endpoint <apiEndpoint from the summary> \
    --test-xuid 9000000000000001 --bucket xbox-cloud-saves-589997396938-us-west-2
python3 provision_aws.py --destroy                # full teardown, if ever wanted
```

## Status (2026-11-11)

- **Built and syntax-checked; NOT yet executed.** The environment
  that built this (the Muse VM) holds no AWS credentials for
  Taylor's accounts — checked directly: the AWS CLI reports
  "Unable to locate credentials" (earlier real AWS changes ran
  through Taylor's browser CloudShell session, which a background
  build cannot use). No bucket, role, function, or API exists until
  `provision_aws.py` is run from an authenticated session as above.
  The identifiers in the table are the deterministic names the
  provisioner will create.

## XSTS validation — the one real blocker

`lambda_presign.py` validates tokens against the **real** Xbox Live
services: it parses the `XBL3.0 x=<user_hash>;<token>` header,
checks the token's DisplayClaims (XUID from `xui[0].xid`, user-hash
binding `uhs == user_hash`), then presents the token to
`profile.xboxlive.com` — Microsoft itself accepting or rejecting it.

What is missing is **Microsoft-side**: XSTS tokens are only issued
for a relying party / audience registered with Microsoft (Xbox Live
partner registration — ID@Xbox, the Xbox Live Creators Program, or a
full partner title), and Taylor has no such registration or
credentials on file. Until one exists:

- no real Xbox client can obtain a token this API will accept, and
- validation correctly returns 401 for everything else.

Nothing about this is faked: there is no invented Microsoft
credential and no silent stub.

**Test identity (verification only).** So the rest of the chain can
be proven before the registration exists, the Lambda accepts a
clearly-labeled test identity **only** when the provisioner set it
with `--enable-test-identity`: requests with header
`X-Xsave-Test-Xuid: 9000000000000001` are treated as that XUID.
`verify_aws.py` uses exactly that. For any real use, remove it:

```bash
aws lambda update-function-configuration --function-name xsave-presign-api \
  --environment Variables="{XSAVE_BUCKET=xbox-cloud-saves-589997396938-us-west-2,XSAVE_TEST_XUID=}"
```

## Deliberately not provisioned

From the document's wider design, these need more than this pack —
mostly the Microsoft registration above, or separate decisions:
real Xbox client token issuance (Microsoft-side), the full Save API
surface beyond the two URL routes (list/delete/indexer), the S3
ObjectCreated metadata indexer Lambda, offline Bluetooth sync, the
Jira backlog seed (`jira_backlog_seed.py`), and SSE-KMS with a
customer-managed key (the bucket uses SSE-S3/AES256 — encryption on,
no key-management charges).
